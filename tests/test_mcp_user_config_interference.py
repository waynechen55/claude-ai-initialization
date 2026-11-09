"""A server that reads the operator's own configuration, and the evidence a credential matters.

build.opensuse.org finished with nothing in ``.mcp.json``. osc-mcp takes its API
address from the operator's oscrc before its entry's ``OSC_MCP_API`` and refuses
any example.net address by design; on a machine set up for build.example.net every
launch died with "failed to get credentials" -- with a stand-in credential just
as without one -- and was filed as a credential wall, then discarded by the
start gate. The existing bridge entry was dismissed as "``${ICA_MCP_PYTHON}`` is
not installed", the best lead printed was a URL that answered 404, and one agent
proposal named ``/abs/path/osc-mcp`` copied from a skill's example.

The fake server is a real process speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

#: Reads $HOME/.fakerc; refuses a "forbidden" API whatever credential it is
#: given, like osc-mcp's example.net guard; otherwise needs FAKE_PASSWORD.
SERVER = """
import json, os, sys, time
rc = os.path.join(os.environ.get("HOME", ""), ".fakerc")
api = open(rc).read().strip() if os.path.exists(rc) else os.environ.get("FAKE_API", "")
stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
if "forbidden" in api:
    sys.stderr.write(f"time={stamp} level=ERROR msg=\\"failed to get credentials\\" error=\\"refusing {api}\\"\\n")
    raise SystemExit(1)
if not os.environ.get("FAKE_PASSWORD"):
    sys.stderr.write(f"time={stamp} level=ERROR msg=\\"failed to get credentials\\" error=\\"FAKE_PASSWORD not set\\"\\n")
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
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-osc", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "list_requests", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, tmp_path, monkeypatch):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.secrets.resolve = lambda *a, **k: None  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.healing.isolation.directory", str(tmp_path / "isolated" / "{slug}"))
    operator = tmp_path / "operator-home"
    operator.mkdir()
    (operator / ".fakerc").write_text("https://api.forbidden.example\n")
    monkeypatch.setenv("HOME", str(operator))
    ctx.runner.base_env["HOME"] = str(operator)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("FAKE_PASSWORD", raising=False)
    return ctx


def fake(tmp_path, *, home: Path | None = None) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    env = {"FAKE_API": "https://api.example.org"}
    if home is not None:
        env["HOME"] = str(home)
    candidate = Candidate(
        kind="stdio",
        name="build-example-org",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env=env,
    )
    candidate.secret_env = {"FAKE_PASSWORD": "env:FAKE_PASSWORD"}
    return candidate


# -- the operator's configuration ---------------------------------------------------------


def test_a_server_broken_by_the_operator_s_configuration_gets_a_scope_of_its_own(live, tmp_path, monkeypatch) -> None:
    verifier = CandidateVerifier(live)
    probe_stdio = verifier.prober.probe_stdio
    probe_homes: list[str] = []

    def record_probe_home(command, **kwargs):
        probe_homes.append(str(kwargs.get("env", {}).get("HOME", "")))
        return probe_stdio(command, **kwargs)

    monkeypatch.setattr(verifier.prober, "probe_stdio", record_probe_home)
    candidate = verifier.verify(fake(tmp_path, home=tmp_path / "operator-home"))
    shake = candidate.handshake

    assert shake.credential_pending == ["FAKE_PASSWORD"], "with its own scope it asks for its credential"
    assert candidate.needs_credential and not candidate.verified
    assert probe_homes[0] == str(tmp_path / "operator-home")
    assert any(home.startswith(str(tmp_path / "isolated")) for home in probe_homes)
    assert set(shake.config_isolated) == {"HOME", "XDG_CONFIG_HOME"}, (
        f"error={shake.error!r}; credential_test={shake.credential_test!r}; probe_homes={probe_homes!r}"
    )
    assert "operator's own configuration" in shake.config_interference
    assert str(tmp_path / "isolated") in candidate.env["HOME"], "the candidate keeps the scope"


def test_the_entry_carries_the_scope_relocatably(live, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    live.settings.set("mcp.registration.path_variables", {"ICA_TEST_ROOT": str(tmp_path / "isolated")})
    candidate = CandidateVerifier(live).verify(fake(tmp_path, home=tmp_path / "operator-home"))
    entry = McpWriter(live).entry_for(candidate, "build-example-org")
    assert entry["env"]["HOME"].startswith("${ICA_TEST_ROOT:-"), entry["env"]["HOME"]


def test_isolation_can_be_turned_off(live, tmp_path) -> None:
    live.settings.set("mcp.healing.isolation.enabled", False)
    candidate = CandidateVerifier(live).verify(fake(tmp_path, home=tmp_path / "operator-home"))
    assert not candidate.handshake.config_isolated
    assert not candidate.needs_credential


def test_a_failure_the_isolation_does_not_change_is_left_as_it_was(live, tmp_path) -> None:
    home = tmp_path / "operator-home"
    candidate = fake(tmp_path, home=home)
    candidate.env["FAKE_API"] = "https://api.forbidden.example"  # the entry itself is the problem
    candidate = CandidateVerifier(live).verify(candidate)
    assert not candidate.handshake.config_isolated
    assert candidate.env["HOME"] == str(home), "nothing is kept that did not help"


# -- a credential wall needs evidence -------------------------------------------------------


def test_the_same_failure_with_a_stand_in_is_not_a_credential_wall(live, tmp_path) -> None:
    live.settings.set("mcp.healing.isolation.enabled", False)
    candidate = CandidateVerifier(live).verify(fake(tmp_path, home=tmp_path / "operator-home"))
    shake = candidate.handshake
    assert not shake.credential_pending, (
        "words alone are not evidence when the stand-in changed nothing; "
        f"error={shake.error!r}; credential_test={shake.credential_test!r}"
    )
    assert "whatever credential it is given" in shake.credential_test


def test_timestamps_do_not_make_two_failures_different(live) -> None:
    verifier = CandidateVerifier(live)
    one = Handshake(error='time=2026-10-13T23:37:07.131-04:00 msg="failed" pid=12 at 0xdeadbeef')
    other = Handshake(error='time=2026-10-13T23:37:08.002-04:00 msg="failed" pid=99 at 0xfeedface')
    assert verifier.same_failure(one, other)
    assert not verifier.same_failure(one, Handshake(error='time=x msg="password not found"'))


# -- the program's own variables in an existing entry ----------------------------------------


@pytest.fixture
def reuse(live, sandbox, tmp_path):
    bridge = tmp_path / "artifacts" / "bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("")
    live.settings.set("mcp.registration.path_variables", {"ICA_MCP_ARTIFACTS": str(tmp_path / "artifacts")})
    return live


def existing(reuse, entry: dict):
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration

    return ExistingRegistrations(reuse).candidate(Registration("build-example-org", entry, PROJECT_FILE), "bridge")


def test_reuse_resolves_this_program_s_own_variables(reuse, tmp_path) -> None:
    candidate = existing(reuse, {"command": "${ICA_MCP_PYTHON}", "args": ["${ICA_MCP_ARTIFACTS}/bridge.py"]})
    assert "${" not in candidate.command[0], "no longer 'ICA_MCP_PYTHON is not installed'"
    assert candidate.args == [str(tmp_path / "artifacts" / "bridge.py")]


def test_an_entry_from_another_machine_is_said_to_be(reuse) -> None:
    reuse.settings.set("mcp.registration.path_variables", {"ICA_MCP_ARTIFACTS": "/nonexistent/elsewhere"})
    candidate = existing(reuse, {"command": "python3", "args": ["${ICA_MCP_ARTIFACTS}/bridge.py"]})
    assert any("copied from another" in note for note in candidate.notes)


def test_restore_path_defaults_gives_the_variables_their_defaults(reuse, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    healer = EntryDoctor(reuse, McpWriter(reuse), CandidateVerifier(reuse))
    entry = {"command": "python3", "args": ["${ICA_MCP_ARTIFACTS}/bridge.py"]}
    revised = healer._restore_path_defaults(Candidate(name="x"), entry)
    assert revised["args"] == [f"${{ICA_MCP_ARTIFACTS:-{tmp_path / 'artifacts'}}}/bridge.py"]


# -- reporting --------------------------------------------------------------------------------


def test_a_server_that_ran_outranks_a_404_as_the_best_lead() -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import _lead_rank

    absent = Candidate(
        kind="url", url="https://x.example/mcp", confidence=0.9, handshake=Handshake(status=404, error="HTTP 404")
    )
    ran = Candidate(
        kind="stdio", command=["osc-mcp"], confidence=0.5, handshake=Handshake(error="refusing the operator's apiurl")
    )
    ran.start_failure = "its entry does not start"
    assert max([absent, ran], key=_lead_rank) is ran


def test_a_schema_refusal_names_the_fix() -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import describe_schema_problems

    assert "newer or patched build" in describe_schema_problems([{"tool": "t", "key": "a b"}])


def test_the_unstartable_are_kept_for_the_report() -> None:
    assert TargetOutcome(target="https://x.example").unstartable == []


# -- agent placeholders ------------------------------------------------------------------------


@pytest.mark.parametrize("command", [["/abs/path/osc-mcp"], ["<ABSOLUTE PATH OF THE BRIDGE SCRIPT>"]])
def test_a_copied_placeholder_is_refused_and_named(ctx, command) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    candidate, why = AgenticPipeline(ctx)._candidate_from({"kind": "stdio", "command": command}, origin="package")
    assert candidate is None
    assert "placeholder" in why and command[0][:9] in why


def test_no_skill_example_carries_a_path_an_agent_could_copy() -> None:
    from pathlib import Path

    skills = Path(__file__).resolve().parent.parent / "initialize_claude_ai" / "skills"
    for skill in skills.glob("*/SKILL.md"):
        assert "/abs/path/to" not in skill.read_text(encoding="utf-8"), skill


def test_the_entry_json_stays_valid(live, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = CandidateVerifier(live).verify(fake(tmp_path))
    json.dumps(McpWriter(live).entry_for(candidate, "build-example-org"))


def test_the_healing_repair_gives_an_entry_its_own_scope(live, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    doctor = EntryDoctor(live, McpWriter(live), CandidateVerifier(live))
    entry = {"command": sys.executable, "args": ["server.py"], "env": {"FAKE_API": "https://api.example.org"}}
    revised = doctor._isolate_user_config(Candidate(name="build-example-org"), entry)
    assert set(revised["env"]) == {"FAKE_API", "HOME", "XDG_CONFIG_HOME"}
    assert "operator-home" not in json.dumps(revised), "never the operator's own home"
    assert doctor._isolate_user_config(Candidate(name="build-example-org"), revised) is None, "already scoped"
