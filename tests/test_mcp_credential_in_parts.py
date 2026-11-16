"""openSUSE's own server for build.opensuse.org was found four times and never written.

The run's agents proposed ``osc-mcp`` (``go run github.com/openSUSE/osc-mcp``),
the right answer, four times. Two defects threw it away every time:

* **A search path was read as a file.** An entry setting
  ``PATH=/usr/bin:/bin`` was refused as "the entry names /usr/bin:/bin, which
  is not there" -- before it was ever tried.
* **A credential in two parts was tried as one.** osc-mcp needs a user and a
  password. Without the password it said "password not found"; with a stand-in
  password it said "user not set for apiurl api.opensuse.org". The stand-in had
  been *used*, the failure had moved on -- and the run concluded "the
  credential is not the missing piece".

The pattern, for every server: a gate must read an input the way its consumer
does (a search path is a list of places to look), and evidence that a stand-in
was consumed is evidence for the credential, not against it.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery.portability import (
    DISPOSABLE,
    absolute_paths,
    assess,
    search_path,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://build.example.org"

#: Behaves like osc-mcp: wants OBS_TOOL_PASSWORD, then OBS_TOOL_USER, then starts.
SERVER = """
import json, os, sys
if not os.environ.get("OBS_TOOL_PASSWORD"):
    sys.stderr.write('level=ERROR msg="failed to get credentials" error="password not found in and '
                     'keyring access failed: could not find credentials for api.example.org"\\n')
    raise SystemExit(1)
if not os.environ.get("OBS_TOOL_USER"):
    sys.stderr.write('level=ERROR msg="failed to get credentials" error="user not set for apiurl '
                     'api.example.org in .oscrc"\\n')
    raise SystemExit(1)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "obs-tool", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "search_packages", "description": "",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, monkeypatch):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.secrets.resolve = lambda *a, **k: None  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.packages.credential_probe.timeout_seconds", 20)
    monkeypatch.delenv("OBS_TOOL_PASSWORD", raising=False)
    monkeypatch.delenv("OBS_TOOL_USER", raising=False)
    return ctx


def obs_tool(tmp_path, **env) -> Candidate:
    script = tmp_path / "obs_tool.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="build-example-org",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"OBS_TOOL_API": "https://api.example.org", **env},
    )
    candidate.secret_env = {"OBS_TOOL_PASSWORD": "env:OBS_TOOL_PASSWORD"}
    return candidate


# -- A. a search path is not a file ---------------------------------------------------


def test_a_search_path_is_read_as_its_directories() -> None:
    assert search_path("/usr/bin:/bin") == ["/usr/bin", "/bin"]
    assert search_path("/usr/bin") is None
    assert search_path("https://example.org:8443") is None, "a URL is not a search path"


def test_an_entry_with_a_search_path_is_not_refused_for_a_missing_directory(tmp_path) -> None:
    candidate = Candidate(
        kind="stdio",
        name="obs",
        command=["go", "run", "example.org/tool@v1"],
        env={"PATH": f"/usr/bin:{tmp_path}/no-such-dir", "OBS_TOOL_API": "https://api.example.org"},
    )
    assert f"{tmp_path}/no-such-dir" in absolute_paths(candidate)
    verdict = assess(candidate)
    assert verdict.kind != DISPOSABLE, verdict.reason


def test_a_real_missing_file_is_still_refused(tmp_path) -> None:
    candidate = Candidate(kind="stdio", name="x", command=[f"{tmp_path}/gone.py"])
    assert assess(candidate).kind == DISPOSABLE


def test_a_search_path_under_a_root_moves_with_it(ctx) -> None:
    writer = McpWriter(ctx)
    roots = [("ICA_MCP_ARTIFACTS", "/srv/artifacts")]
    assert writer.portable("/srv/artifacts/bin:/usr/bin", roots, key="PATH") == (
        "${ICA_MCP_ARTIFACTS:-/srv/artifacts}/bin:/usr/bin"
    )


# -- B. a credential in parts ------------------------------------------------------------


def test_the_rest_of_a_credential_in_parts_is_found_and_declared(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(obs_tool(tmp_path))

    assert candidate.needs_credential, "waiting for its credential, not discarded as broken"
    assert not candidate.verified
    assert candidate.handshake.credential_pending == ["OBS_TOOL_PASSWORD", "OBS_TOOL_USER"]
    assert "OBS_TOOL_USER" in candidate.secret_env, "the other part gets a slot of its own"
    assert "in parts" in candidate.handshake.credential_test


def test_the_written_entry_names_both_parts(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(obs_tool(tmp_path))
    entry = McpWriter(live).entry_for(candidate, "build-example-org")
    assert {"OBS_TOOL_PASSWORD", "OBS_TOOL_USER"} <= set(entry["env"])


def test_the_entry_starts_as_written_with_stand_ins_for_both(live, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor

    candidate = CandidateVerifier(live).verify(obs_tool(tmp_path))
    writer = McpWriter(live)
    healing = EntryDoctor(live, writer, writer.verifier).examine(candidate, "build-example-org", announce=False)
    assert healing.ok, healing.error


def test_companions_can_be_switched_off(live, tmp_path) -> None:
    live.settings.set("mcp.packages.credential_probe.max_companion_trials", 0)
    candidate = CandidateVerifier(live).verify(obs_tool(tmp_path))
    # The older verdict, on the server's words alone: waiting for the one part declared.
    assert candidate.handshake.credential_pending == ["OBS_TOOL_PASSWORD"]
    assert "OBS_TOOL_USER" not in candidate.secret_env


def test_a_failure_that_does_not_move_on_is_not_a_credential_in_parts(live, tmp_path) -> None:
    """The stand-in changed nothing: no companion is guessed at."""
    script = tmp_path / "broken.py"
    script.write_text(
        "import sys; sys.stderr.write('failed to get credentials: config unreadable\\n'); raise SystemExit(1)"
    )
    candidate = Candidate(
        kind="stdio", name="b", command=[sys.executable, str(script)], env={"OBS_TOOL_API": "https://api.example.org"}
    )
    candidate.secret_env = {"OBS_TOOL_PASSWORD": "env:OBS_TOOL_PASSWORD"}
    verdict = CandidateVerifier(live).verify(candidate)
    assert "OBS_TOOL_USER" not in verdict.secret_env
