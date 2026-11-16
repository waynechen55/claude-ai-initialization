"""The launch that ships is checked as patiently as the one that was trialled -- and written as it ran.

gitlab.example.net's graphql server was proved by its trial (304 tools, public
reads) and then never written:

* the entry said ``"command": "uvx"`` while the trial had run
  ``/root/.local/bin/uvx`` -- found through this program's extra binary
  directories, not the ``PATH`` a client passes on -- so the entry as written
  died with "No such file or directory";
* the repair that wrote the absolute path got the server running, and it was
  still introspecting a large schema (7 to 68 s, measured) when the start
  check's single 90 s try ran out: "did not start";
* refused, the only proven server left the address with no entry at all;
* its CA settings read ``${ICA_STATE_DIR:-...}/trust/ca-bundle.pem``, a file
  only the run machine had;
* and it printed a banner on stdout before its handshake.

Fake servers are real processes; nothing here is a real service.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

#: Answers initialize after DELAY seconds (0 = at once), never (DELAY < 0), and
#: prints BANNER on stdout first when it is set.
SERVER = """
import json, os, sys, time
delay = float(os.environ.get("DELAY", "0"))
if os.environ.get("BANNER"):
    print(os.environ["BANNER"], flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") is None:
        continue
    if msg["method"] == "initialize":
        if delay < 0:
            time.sleep(3600)
        time.sleep(delay)
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "slow", "version": "1"}}
    elif msg["method"] == "tools/list":
        result = {"tools": [{"name": "ping", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {"content": [{"type": "text", "text": "pong"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 2)
    ctx.settings.set("mcp.packages.slow_start_timeout_factor", 4)
    ctx.settings.set("mcp.packages.slow_start_retries", 1)
    return ctx


def entry(tmp_path, **env) -> dict:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "env": {k: str(v) for k, v in env.items()},
    }


# -- 2. the start check is as patient as a trial --------------------------------------------


def test_a_start_slower_than_one_deadline_is_asked_again(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify_as_written(entry(tmp_path, DELAY=3), "slow")
    assert shake.ok, "2 s ran out, the retry's 8 s did not"
    assert not shake.start_unconfirmed


def test_a_server_still_running_at_every_deadline_is_unconfirmed_not_failed(live, tmp_path) -> None:
    live.settings.set("mcp.packages.slow_start_timeout_factor", 1.5)
    shake = CandidateVerifier(live).verify_as_written(entry(tmp_path, DELAY=-1), "stuck")
    assert not shake.ok
    assert "slow start, not a failed one" in shake.start_unconfirmed


def test_a_server_that_cannot_launch_is_still_a_failure(live, tmp_path) -> None:
    broken = {"type": "stdio", "command": str(tmp_path / "no-such-launcher"), "args": []}
    shake = CandidateVerifier(live).verify_as_written(broken, "missing")
    assert not shake.ok and not shake.start_unconfirmed, "ENOENT answers at once: nothing slow about it"


# -- 3. an unconfirmed start is neither repaired against nor refused --------------------------


def test_the_doctor_keeps_an_unconfirmed_start_usable(live, tmp_path) -> None:
    live.settings.set("mcp.packages.slow_start_timeout_factor", 1.5)
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio", name="slow", transport=Transport.STDIO, command=[sys.executable, str(script)], env={"DELAY": "-1"}
    )
    writer = McpWriter(live)
    healing = EntryDoctor(live, writer, CandidateVerifier(live)).examine(candidate, "slow", announce=False)
    assert healing.usable and not healing.ok and healing.unconfirmed
    assert healing.applied == [], "nothing in the entry is shown wrong, so nothing is repaired"
    assert healing.to_dict()["starts"] is None


def test_the_verification_row_says_not_confirmed_rather_than_no(ctx) -> None:
    registration = Registration(name="slow", candidate=Candidate(kind="stdio", name="slow"), entry={})
    registration.start_check = Handshake(
        ok=False, transport=Transport.STDIO, start_unconfirmed="no handshake within 180s"
    )
    row = McpWriter(ctx).start_check_row(registration)
    assert row.ok is None and "not confirmed" in row.detail
    assert registration.to_dict()["starts_as_written"] is None


# -- 1. the command is written as it ran ------------------------------------------------------


def test_a_bare_command_the_client_cannot_find_is_written_as_the_path_that_ran(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/opt/tools/bin/{name}")
    writer = McpWriter(ctx)
    assert writer.launched_command("uvx") == "/opt/tools/bin/uvx"
    candidate = Candidate(kind="stdio", name="g", transport=Transport.STDIO, command=["uvx", "pkg==1.0"])
    written = writer.entry_for(candidate, "gitlab-example-net")
    assert written["command"].startswith("${ICA_MCP_") and written["command"].endswith(":-/opt/tools/bin/uvx}")


def test_a_bare_command_the_client_finds_stays_bare(ctx, tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "uvx"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/opt/tools/bin/{name}")
    assert McpWriter(ctx).launched_command("uvx") == "uvx", "the launcher form is kept where it works"


def test_the_switch_keeps_the_name_as_written(ctx, tmp_path, monkeypatch) -> None:
    ctx.settings.set("mcp.registration.resolve_unfound_commands", False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/opt/tools/bin/{name}")
    assert McpWriter(ctx).launched_command("uvx") == "uvx"


# -- 4. a CA bundle is a fact about the machine ------------------------------------------------


def test_the_run_s_trust_store_bundle_is_read_through_the_per_machine_name(ctx, tmp_path) -> None:
    store = tmp_path / "trust"
    store.mkdir()
    ctx.settings.set("mcp.trust.store_dir", str(store))
    bundle = str(store / "ca-bundle.pem")
    writer = McpWriter(ctx)
    assert writer.ca_bundle_reference("SSL_CERT_FILE", bundle) == "${ICA_MCP_CA_BUNDLE:-%s}" % bundle
    candidate = Candidate(
        kind="stdio",
        name="g",
        transport=Transport.STDIO,
        command=[sys.executable, "-V"],
        env={"SSL_CERT_FILE": bundle, "REQUESTS_CA_BUNDLE": bundle},
    )
    written = writer.entry_for(candidate, "gitlab-example-net")
    assert written["env"]["SSL_CERT_FILE"] == "${ICA_MCP_CA_BUNDLE:-%s}" % bundle
    assert "ICA_STATE_DIR" not in str(written)


def test_only_trust_variables_and_known_bundles_are_renamed(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.trust.store_dir", str(tmp_path / "trust"))
    writer = McpWriter(ctx)
    assert writer.ca_bundle_reference("SOME_FILE", str(tmp_path / "trust" / "ca-bundle.pem")) == ""
    assert writer.ca_bundle_reference("SSL_CERT_FILE", "/srv/elsewhere/chain.pem") == ""
    assert writer.ca_bundle_reference("SSL_CERT_FILE", "${ICA_MCP_CA_BUNDLE}") == ""


# -- 5. a banner on stdout is said ---------------------------------------------------------------


def test_a_banner_before_the_handshake_is_recorded_and_said(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify_as_written(
        entry(tmp_path, BANNER="* endpoint: https://example.invalid"), "b"
    )
    assert shake.ok, "tolerated: the protocol answer still came"
    assert shake.stdout_noise.startswith("* endpoint")
    assert any("writes to stdout before its handshake" in h for h in shake.protocol_hints)


def test_a_quiet_server_has_nothing_to_say(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify_as_written(entry(tmp_path), "q")
    assert shake.ok and shake.stdout_noise == ""
