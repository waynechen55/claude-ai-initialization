"""smelt-example-net could not start within Claude Code's 30 s on the machine its .mcp.json was copied to.

Its entry carried ``NODE_OPTIONS=--network-family-autoselection-attempt-timeout=30000``
from an old entry, never re-judged: 5 s on the machine that ran the program,
53 s or more here. A runtime-tuning setting fits the machine it was written on
and travels with the file, so every one must now earn its place: the candidate
is launched once without, and it is dropped when the server does as well. The
final file check flags a setting holding a wait at or past Claude Code's limit.
Restoring a lost folder also nested it in itself again -- one placement rule
now serves every mover.
"""

from __future__ import annotations

import sys
from pathlib import Path

from initialize_claude_ai.core.paths_home import server_destination
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.verifier import RUNTIME_OPTION_VARIABLES, CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

#: Answers MCP; sleeps first while NODE_OPTIONS is set, as a network wait would.
SLOW_WITH_OPTIONS = """
import json, os, sys, time
if os.environ.get("NODE_OPTIONS"):
    time.sleep(float(os.environ.get("SLEEP", "4")))
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    if rid is None:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "slow", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "describe", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""

#: Starts only with its runtime option set.
NEEDS_OPTIONS = SLOW_WITH_OPTIONS.replace(
    'if os.environ.get("NODE_OPTIONS"):\n    time.sleep(float(os.environ.get("SLEEP", "4")))',
    'if not os.environ.get("NODE_OPTIONS"):\n    sys.exit(3)',
)


def _candidate(tmp_path: Path, source: str) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(source)
    return Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=[sys.executable, str(script)],
        env={"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000", "SLEEP": "4"},
    )


def test_a_runtime_setting_the_server_does_not_need_is_dropped(ctx, tmp_path) -> None:
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.verification.runtime_tuning.unknown_job_keeps", False)
    candidate = CandidateVerifier(ctx).verify(_candidate(tmp_path, SLOW_WITH_OPTIONS))
    assert "NODE_OPTIONS" not in candidate.env
    assert candidate.handshake is not None and candidate.handshake.ok
    assert any("NODE_OPTIONS dropped" in note for note in candidate.notes)


def test_a_runtime_setting_the_server_needs_is_kept(ctx, tmp_path) -> None:
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    candidate = CandidateVerifier(ctx).verify(_candidate(tmp_path, NEEDS_OPTIONS))
    assert "NODE_OPTIONS" in candidate.env


def test_switching_it_off_keeps_every_setting(ctx, tmp_path) -> None:
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.verification.runtime_tuning.enabled", False)
    candidate = CandidateVerifier(ctx).verify(_candidate(tmp_path, SLOW_WITH_OPTIONS))
    assert "NODE_OPTIONS" in candidate.env


def test_the_final_file_check_flags_a_wait_past_the_start_up_limit(ctx) -> None:
    entry = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"},
    }
    _fixed, problems = McpWriter(ctx).check_as_run(entry)
    assert problems and "start-up wait" in problems[0][0] and problems[0][1] is False
    short = {"command": "npx", "env": {"NODE_OPTIONS": "--max-old-space-size=4096"}}
    assert McpWriter(ctx).check_as_run(short) == (None, [])


def test_every_mover_places_a_server_s_folder_without_nesting(tmp_path) -> None:
    home = tmp_path / "mcp-servers"
    assert server_destination(home, "smelt-example-test", Path("/elsewhere/mcp-servers/smelt-example-test")) == (
        home / "smelt-example-test"
    )
    assert (
        server_destination(home, "smelt-example-test", Path("/scratch/ca.pem"))
        == home / "smelt-example-test" / "ca.pem"
    )
    nested = home / "smelt-example-test" / "smelt-example-test" / "ca.pem"
    assert server_destination(home, "smelt-example-test", nested) == home / "smelt-example-test" / "ca.pem"


def test_packaged_settings_equal_the_code(settings) -> None:
    section = settings.section("mcp.verification.runtime_tuning")
    assert section.bool_("enabled") is True and section.bool_("flag_start_hazards") is True
    assert list(section.str_list("runtime_option_variables", [])) == list(RUNTIME_OPTION_VARIABLES)
    assert section.float_("start_slack_seconds") == 2.0
    assert section.bool_("unknown_job_keeps") is True and section.bool_("per_machine") is True
    from initialize_claude_ai.mcp_auto_discovery.writer import MACHINE_VARIABLE_TEMPLATE

    assert section.str_("machine_variable_template") == MACHINE_VARIABLE_TEMPLATE
    assert settings.bool_("mcp.verification.prefer_https") is True
    from initialize_claude_ai.mcp_auto_discovery import probes

    assert settings.int_("mcp.verification.launcher_retries") == probes.LAUNCHER_RETRIES
    assert list(settings.list_("mcp.verification.launcher_transient_patterns")) == list(
        probes.LAUNCHER_TRANSIENT_PATTERNS
    )


def test_a_variant_with_other_runtime_tuning_keeps_its_own_start_record() -> None:
    """The old entry's 30 s network wait set the rewritten entry's "slowest start" and raised MCP_TIMEOUT."""
    from initialize_claude_ai.mcp_auto_discovery.probes import start_key

    argv = ["/usr/bin/npx", "-y", "mcp-graphql@2.0.4"]
    with_tuning = start_key(
        argv,
        {
            "NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000",
            "ENDPOINT": "https://smelt.example.test/graphql/",
        },
    )
    without = start_key(argv, {"ENDPOINT": "https://smelt.example.test/graphql/"})
    assert with_tuning != without and without == start_key(argv)


# -- "as well" is the job; a needed setting is this machine's ---------------------------------------


def _shake(passed: bool) -> Handshake:
    return Handshake(
        ok=True,
        tools=["introspect-schema"],
        start_seconds=1.0,
        contract=[{"task": "describe the API", "tool": "introspect-schema", "passed": passed}],
    )


def test_a_setting_the_job_needs_is_kept_although_the_handshake_works_without(ctx, monkeypatch) -> None:
    """smelt-example-net: started fine without NODE_OPTIONS; every real call failed ("fetch failed")."""
    verifier = CandidateVerifier(ctx)
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"},
    )
    candidate.handshake = _shake(True)
    monkeypatch.setattr(verifier, "_verify_stdio", lambda c: _shake(False))
    assert verifier._without_runtime_tuning(candidate) == [] and "NODE_OPTIONS" in candidate.env
    monkeypatch.setattr(verifier, "_verify_stdio", lambda c: _shake(True))
    assert verifier._without_runtime_tuning(candidate) == ["NODE_OPTIONS"], "the job done as well: dropped"


def test_an_unknown_job_keeps_the_setting(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    candidate = Candidate(kind="stdio", name="x", command=["node", "s.js"], env={"NODE_OPTIONS": "--x"})
    candidate.handshake = Handshake(ok=True, tools=["t"], start_seconds=1.0)
    monkeypatch.setattr(verifier, "_verify_stdio", lambda c: Handshake(ok=True, tools=["t"], start_seconds=1.0))
    assert verifier._without_runtime_tuning(candidate) == []


def test_a_needed_runtime_setting_is_written_per_machine(ctx) -> None:
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={
            "ENDPOINT": "https://smelt.example.test/graphql/",
            "NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000",
        },
    )
    entry = McpWriter(ctx).entry_for(candidate, "smelt-example-test")
    assert entry["env"]["NODE_OPTIONS"] == "${ICA_MCP_SMELT_EXAMPLE_TEST_NODE_OPTIONS:-}"
    assert candidate.machine_values == {
        "ICA_MCP_SMELT_EXAMPLE_TEST_NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"
    }
    assert "network-family" not in str(entry), "never a literal in the file that travels"
    assert candidate.launch_values["ICA_MCP_SMELT_EXAMPLE_TEST_NODE_OPTIONS"], "the start check launches with it"


def test_an_http_endpoint_moves_to_https_when_it_answers_there(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    candidate = Candidate(kind="url", name="openqa-example-test", url="http://openqa.example.test/mcp?protocol=mcp")
    monkeypatch.setattr(verifier, "_verify_url", lambda c: Handshake(ok=False, requires_auth=c.url.startswith("https")))
    assert verifier._prefer_https(candidate) and candidate.url == "https://openqa.example.test/mcp?protocol=mcp"
    stays = Candidate(kind="url", name="x", url="http://x.example.test/mcp")
    monkeypatch.setattr(verifier, "_verify_url", lambda c: Handshake(ok=False, error="no answer"))
    assert not verifier._prefer_https(stays) and stays.url.startswith("http://")


def test_a_launcher_s_momentary_failure_is_launched_again(ctx, monkeypatch) -> None:
    """Two npx launches racing on a fresh cache: "npm error enoent ... _npx/.../package.json"."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    prober = McpProber(ctx)
    ctx.settings.set("mcp.verification.launcher_retry_pause_seconds", 0.0)
    answers = [
        Handshake(ok=False, error="process exited with rc=254: npm error enoent This is related to npm"),
        Handshake(ok=True, tools=["t"]),
    ]
    monkeypatch.setattr(prober, "_probe_stdio_once", lambda command, **kw: answers.pop(0))
    assert prober.probe_stdio(["npx", "-y", "mcp-graphql@2.0.4"]).ok
    broken = [Handshake(ok=False, error="process exited with rc=1: TypeError: x is not a function")]
    monkeypatch.setattr(prober, "_probe_stdio_once", lambda command, **kw: broken.pop(0))
    assert not prober.probe_stdio(["npx", "-y", "x"]).ok, "a real failure is not retried"
