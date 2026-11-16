"""A value is a credential by evidence, and "without a credential" means without it.

Two runs on 2026-10-22 went wrong the same way: a judgement was made from
something that only *looked* like evidence.

* gitlab.example.net -- the generated bridge's User-Agent, ``mcp-bridge-gitlab-example-net/1``,
  scored as random enough to be a token. The writer's last guard turned it into a
  bare ``${GITLAB_EXAMPLE_NET_MCP_BRIDGE_USER_AGENT}`` nothing set, made it a credential
  slot, and the report asked the operator to export it "with a valid token".
  Claude Code warned "Missing environment variables" for the entry.
* github.com -- the trial "without a credential" inherited the token from the
  run's environment, ``get_me`` answered, and the run concluded the token was
  optional; the stand-in trial then got 401 and the two were read backwards.

Learned, for every server and both modes:

* one rule decides whether a value is a credential (``McpWriter.credential_evidence``):
  proven by name/inline/in-use, configuration when it equals the server's own
  default, *suspect* when it merely looks random -- and a suspect value is kept
  out of the file *and kept working*, never left as a reference nothing sets and
  never reported as a credential to supply;
* the launch the start check tests carries the kept values, so it is the launch
  that ships;
* a launch that claims to lack a credential withholds it from what it inherits,
  and an answer given with an inherited credential is never reused for it;
* a credential goes to a settings file nobody shares.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery import claude_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
from initialize_claude_ai.provision.settings_writer import env_settings_paths, secret_settings_path, settings_path

#: Random enough for looks_opaque, under a name no credential word matches.
RANDOM = "Qm9vZ2llV29vZ2llMTIzNDU2Nzg5MGFiY2RlZg8x"

BRIDGE = """\
import os
agent = (
    os.environ.get("MCP_BRIDGE_USER_AGENT")
    or "mcp-bridge-example-test/1"
)
timeout = os.environ.get("MCP_BRIDGE_TIMEOUT") or "30"
"""


def bridge(tmp_path: Path, **env: str) -> Candidate:
    script = tmp_path / "mcp_bridge_example_test.py"
    script.write_text(BRIDGE)
    return Candidate(kind="stdio", name="example-test", command=[sys.executable, "-I", str(script)], env=dict(env))


# -- one rule for "is this a credential" ----------------------------------------------------------------


def test_a_value_equal_to_the_servers_own_default_is_configuration(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path)
    assert writer.credential_evidence("MCP_BRIDGE_USER_AGENT", "mcp-bridge-example-test/1", candidate) == ""


def test_a_value_that_only_looks_random_is_suspect_and_a_named_one_proven(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path)
    assert writer.credential_evidence("MCP_BRIDGE_USER_AGENT", RANDOM, candidate) == writer.SUSPECT
    assert writer.credential_evidence("EXAMPLE_API_TOKEN", "hunter2-but-longer", candidate) == writer.PROVEN
    assert writer.credential_evidence("MCP_BRIDGE_TIMEOUT", "30", candidate) == ""


def test_the_bridge_user_agent_is_written_as_it_was_tested(ctx, tmp_path) -> None:
    """The gitlab.example.net case: no bare reference, no credential slot, nothing to export."""
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, MCP_BRIDGE_USER_AGENT="mcp-bridge-example-test/1")
    entry = writer.entry_for(candidate, "example-test")

    assert entry["env"]["MCP_BRIDGE_USER_AGENT"] == "mcp-bridge-example-test/1"
    assert "MCP_BRIDGE_USER_AGENT" not in candidate.secret_env
    assert candidate.kept_out == {}
    assert writer.exported_variables(candidate) == []
    assert claude_check.missing_variables(entry, {}) == [], "Claude Code would warn about a missing variable"


def test_a_suspect_literal_is_kept_out_of_the_file_and_kept_working(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, MCP_BRIDGE_USER_AGENT=RANDOM)
    entry = writer.entry_for(candidate, "example-test")

    text = json.dumps(entry)
    assert RANDOM not in text, "the value never reaches the file"
    reference = entry["env"]["MCP_BRIDGE_USER_AGENT"]
    assert reference.startswith("${") and reference.endswith("}")
    variable = reference[2:-1]
    assert candidate.kept_out[variable].reveal() == RANDOM, "...and is kept for the launch and for settings"
    assert "MCP_BRIDGE_USER_AGENT" not in candidate.secret_env, "never a credential slot"
    assert writer.exported_variables(candidate) == [], "never something the operator is asked to export"


def test_a_suspect_default_is_never_left_as_a_bare_reference_without_its_value(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, MCP_BRIDGE_USER_AGENT="${EXAMPLE_AGENT:-" + RANDOM + "}")
    entry = writer.entry_for(candidate, "example-test")

    assert RANDOM not in json.dumps(entry)
    assert entry["env"]["MCP_BRIDGE_USER_AGENT"] == "${EXAMPLE_AGENT}"
    assert candidate.kept_out["EXAMPLE_AGENT"].reveal() == RANDOM


def test_a_proven_credential_default_is_still_removed_and_asked_for(ctx, tmp_path) -> None:
    """The existing guarantee stands: a credential in a default never reaches the file."""
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, EXAMPLE_API_TOKEN="${EXAMPLE_API_TOKEN:-hunter2-but-longer}")
    entry = writer.entry_for(candidate, "example-test")

    assert entry["env"]["EXAMPLE_API_TOKEN"] == "${EXAMPLE_API_TOKEN}"
    assert candidate.kept_out == {}, "a proven credential is the operator's to supply"


def test_kept_values_go_where_claude_code_reads_them_when_written(ctx, tmp_path) -> None:
    ctx.dry_run = False
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, MCP_BRIDGE_USER_AGENT=RANDOM)
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True)
    entry = writer.entry_for(candidate, "example-test")
    variable = entry["env"]["MCP_BRIDGE_USER_AGENT"][2:-1]

    from initialize_claude_ai.mcp_auto_discovery.writer import Registration

    writer._keep_held_credentials(
        Registration(name="example-test", target="https://example.test", candidate=candidate, entry=entry)
    )
    path = secret_settings_path(ctx)
    held = json.loads(path.read_text())["env"]
    assert held[variable] == RANDOM
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert claude_check.missing_variables(entry, claude_check.fresh_environ(ctx)) == [], (
        "a Claude Code started in a new terminal resolves it"
    )


SERVER_NEEDS_VALUE = """
import json, os, sys
from pathlib import Path
if os.environ.get("MCP_BRIDGE_USER_AGENT") != Path(sys.argv[1]).read_text():
    sys.stderr.write("wrong user agent\\n")
    sys.exit(3)
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    if rid is None:
        continue
    method = msg.get("method")
    result = ({"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "x", "version": "1"}} if method == "initialize"
              else {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]} if method == "tools/list" else {})
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_the_start_check_launches_with_the_kept_values(ctx, tmp_path) -> None:
    """The launch tested is the launch shipped: with the kept value, as Claude Code will expand it."""
    ctx.dry_run = False
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    script = tmp_path / "server.py"
    script.write_text(SERVER_NEEDS_VALUE)
    expected = tmp_path / "expected"
    expected.write_text(RANDOM)
    entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script), str(expected)],
        "env": {"MCP_BRIDGE_USER_AGENT": "${EXAMPLE_AGENT}"},
    }
    verifier = CandidateVerifier(ctx)
    without = verifier.verify_as_written(entry, "example-test")
    with_kept = verifier.verify_as_written(entry, "example-test", kept={"EXAMPLE_AGENT": RANDOM})
    assert without is not None and not without.ok
    assert with_kept is not None and with_kept.ok, with_kept.error
    assert RANDOM not in json.dumps(with_kept.to_dict() if hasattr(with_kept, "to_dict") else str(with_kept))


# -- a credential goes to a file nobody shares -------------------------------------------------------------


def test_a_credential_never_goes_to_the_shared_project_settings(ctx, sandbox) -> None:
    assert secret_settings_path(ctx) == settings_path(ctx), "user scope: the user's own settings.json"
    ctx.settings.set("provision.scope", "project")
    assert settings_path(ctx) == sandbox / ".claude" / "settings.json"
    assert secret_settings_path(ctx) == sandbox / ".claude" / "settings.local.json"
    assert env_settings_paths(ctx) == [settings_path(ctx), secret_settings_path(ctx)]


def test_the_fresh_terminal_reads_the_private_file_too(ctx, sandbox) -> None:
    ctx.settings.set("provision.scope", "project")
    private = secret_settings_path(ctx)
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text(json.dumps({"env": {"EXAMPLE_KEPT": "v"}}))
    assert claude_check.fresh_environ(ctx)["EXAMPLE_KEPT"] == "v"


# -- "without a credential" means without it ---------------------------------------------------------------

SERVER_REPORTS_TOKEN = """
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    if rid is None:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "x", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "get_me", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        token = os.environ.get("EXAMPLE_PERSONAL_ACCESS_TOKEN")
        result = ({"content": [{"type": "text", "text": json.dumps({"login": "alice"})}]} if token else
                  {"isError": True, "content": [{"type": "text", "text": "401 Requires authentication"}]})
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_withheld_credential_does_not_reach_the_child_from_the_environment(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.dry_run = False
    script = tmp_path / "server.py"
    script.write_text(SERVER_REPORTS_TOKEN)
    monkeypatch.setenv("EXAMPLE_PERSONAL_ACCESS_TOKEN", "inherited-from-the-shell")
    prober = McpProber(ctx)
    argv = [sys.executable, str(script)]

    inherited = prober.probe_stdio(argv, timeout=20, login=True)
    withheld = prober.probe_stdio(argv, timeout=20, login=True, withhold=["EXAMPLE_PERSONAL_ACCESS_TOKEN"])
    assert inherited.login == login_check.ACCEPTED, "the leak this guards against is real"
    assert withheld.login == login_check.REJECTED, (
        "launched without the credential, it answers as anybody does -- never with the inherited token, "
        "and never with the answer memoised for the launch that had it"
    )


def test_a_name_set_on_purpose_is_never_withheld(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.dry_run = False
    script = tmp_path / "server.py"
    script.write_text(SERVER_REPORTS_TOKEN)
    monkeypatch.delenv("EXAMPLE_PERSONAL_ACCESS_TOKEN", raising=False)
    shake = McpProber(ctx).probe_stdio(
        [sys.executable, str(script)],
        timeout=20,
        login=True,
        env={"EXAMPLE_PERSONAL_ACCESS_TOKEN": "a-stand-in"},
        withhold=["EXAMPLE_PERSONAL_ACCESS_TOKEN"],
    )
    assert shake.login == login_check.ACCEPTED


def test_a_trial_withholds_every_credential_it_is_not_given(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    script = tmp_path / "bridge.py"
    script.write_text('import os\nkey = os.environ.get("SECOND_NAME_TOKEN")\nmode = os.environ.get("MODE")\n')
    candidate = Candidate(kind="stdio", name="x", command=[sys.executable, str(script)])
    candidate.secret_env = {"EXAMPLE_TOKEN": "env:EXAMPLE_TOKEN", "GIVEN_TOKEN": "env:GIVEN_TOKEN"}
    withheld = CandidateVerifier(ctx)._withheld(
        candidate,
        candidate.command,
        env={"MODE": "x"},
        secret_env={"GIVEN_TOKEN": "real"},
        pending=["EXAMPLE_PAT"],
        templates={"EXTRA_HEADERS": "Authorization: Bearer ${EXAMPLE_PAT}"},
    )
    assert withheld == ["EXAMPLE_PAT", "EXAMPLE_TOKEN", "EXTRA_HEADERS", "SECOND_NAME_TOKEN"]


def test_withholding_is_configurable_and_on_by_default(settings) -> None:
    assert settings.bool_("mcp.verification.withhold_unsupplied") is True
    assert os.environ.get("EXAMPLE_PERSONAL_ACCESS_TOKEN") is None


def test_a_withheld_launch_is_part_of_both_modes(ctx) -> None:
    """The trial launch is built once (CandidateVerifier) and both pipelines verify through it."""
    from initialize_claude_ai.mcp_auto_discovery import agentic, traditional, verifier

    for module in (agentic, traditional):
        assert "CandidateVerifier" in Path(module.__file__).read_text(), module.__name__
    assert '"withhold": self._withheld(' in Path(verifier.__file__).read_text()


def test_publishing_a_target_keeps_nothing_it_was_not_given(ctx, tmp_path) -> None:
    """End to end through publish: the entry written for a bridge reads no variable nothing sets."""
    writer = McpWriter(ctx)
    candidate = bridge(tmp_path, MCP_BRIDGE_USER_AGENT="mcp-bridge-example-test/1", MCP_BRIDGE_TIMEOUT="30")
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True)
    [registration] = writer.publish(TargetOutcome(target="https://example.test", verified=[candidate]))
    assert claude_check.missing_variables(registration.entry, {}) == []
    assert registration.needs_env == []


def test_every_copy_of_a_file_that_may_hold_a_credential_is_ignored_by_git() -> None:
    """The project directory is published; a backup holds what the file held when it was taken."""
    import fnmatch

    root = Path(__file__).resolve().parents[1]
    patterns = [
        line.strip()
        for line in (root / ".gitignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    def ignored(name: str) -> bool:
        return any(fnmatch.fnmatch(name, p.lstrip("/")) for p in patterns if not p.startswith("!"))

    for name in (
        ".mcp.json",
        ".mcp.json.ok",
        ".mcp.json.bak-20261017214558",
        "settings.json.20261023-101500.bak",
        ".claude/settings.local.json",
        ".claude/settings.local.json.20261023-101500.bak",
    ):
        assert ignored(name), name
    assert not ignored("README.md")


# -- a URL entry is probed with the header Claude Code will send ---------------------------------------------


def test_a_url_entrys_header_is_resolved_as_claude_code_resolves_it(ctx, monkeypatch) -> None:
    from initialize_claude_ai.core import redact
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("EXAMPLE_HOST_AUTHORIZATION", "alice:0123456789abcdef:fedcba9876543210")
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier, "_settings_value", lambda name: "from-settings" if name == "IN_SETTINGS" else "")
    out = verifier.resolved_headers(
        {
            "Authorization": "Bearer ${EXAMPLE_HOST_AUTHORIZATION}",
            "X-From-Settings": "${IN_SETTINGS}",
            "X-Default": "${NOT_SET:-fallback}",
            "X-Unresolved": "Token ${NOBODY_SETS_THIS}",
        }
    )
    assert out["Authorization"] == "Bearer alice:0123456789abcdef:fedcba9876543210"
    assert out["X-From-Settings"] == "from-settings", "Claude Code expands from its settings env too"
    assert out["X-Default"] == "fallback"
    assert out["X-Unresolved"] == "Token ${NOBODY_SETS_THIS}", "unresolved stays as written: the probe stays anonymous"
    assert "0123456789abcdef" not in redact.redact("Bearer alice:0123456789abcdef:fedcba9876543210")


def test_the_url_probe_sends_the_resolved_credential(ctx, monkeypatch) -> None:
    """openqa-example-net stayed "waiting" because its header went out unexpanded."""
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("EXAMPLE_HOST_AUTHORIZATION", "alice:0123456789abcdef:fedcba9876543210")
    verifier = CandidateVerifier(ctx)
    seen: dict = {}

    def probe_http(url, headers=None, login=False, **_kw):
        seen.update(headers=dict(headers or {}), login=login)
        return Handshake(ok=True, tools=["t"], round_trip=True)

    monkeypatch.setattr(verifier.prober, "probe_http", probe_http)
    candidate = Candidate(
        kind="url",
        name="x",
        url="https://openqa.example.test/mcp",
        headers={"Authorization": "Bearer ${EXAMPLE_HOST_AUTHORIZATION}"},
    )
    verifier._verify_url(candidate)
    assert seen["headers"]["Authorization"].endswith("fedcba9876543210")
    assert seen["login"] is True
