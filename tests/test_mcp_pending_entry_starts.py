"""A server waiting for a credential must also produce an entry that starts.

confluence.example.com had a working registration before the run and none after
it. Both of its candidates -- the existing `mcp-atlassian` entry and an npm
package an agent proposed -- were *waiting for a credential*: each listed its
tools only once a stand-in token was set. The stage check that launches an
entry exactly as written (``heal_or_discard``) only looked at *verified*
candidates, so neither was launched as written until publish. There the one
chosen died with ``FileNotFoundError: [Errno 2] No such file or directory | ) |
^`` -- the line naming the missing file had been dropped -- the lower tiers were
already closed, the other candidate was never tried, and nothing said the
machine still held a registration that had answered that very run.

The pattern, for every server: the launch that was tested has to be the launch
that ships, and a late refusal has to hand over to the next proven option,
never to nothing. These tests hold each half of that.
"""

from __future__ import annotations

import sys

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
from initialize_claude_ai.mcp_auto_discovery.probes import _failure_lines
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
    waits_for_credential,
)
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://confluence.example.com"

#: Starts only with TOKEN set to a value -- a literal "${...}" is no value.
SERVER = """
import json, os, sys
token = os.environ.get("TOKEN", "")
if not token or token.startswith("${"):
    sys.stderr.write("TOKEN is required\\n"); raise SystemExit(1)
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
                  "serverInfo": {"name": "waiting", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "search", "description": "",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""

#: Dies the way confluence.example.com's entry did.
BROKEN = """
import ssl
ssl.create_default_context(cafile="/nonexistent/initialize-claude-ai-test-bundle.pem")
"""


def live(ctx):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: f"/usr/bin/{name}"  # type: ignore[assignment]
    return ctx


def waiting(tmp_path, name: str, source: str = SERVER, confidence: float = 0.5) -> Candidate:
    """A local candidate the credential test showed is waiting for TOKEN."""
    script = tmp_path / f"{name}.py"
    script.write_text(source)
    candidate = Candidate(
        kind="stdio",
        name=name,
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"BASE_URL": TARGET},
        confidence=confidence,
    )
    candidate.secret_env = {"TOKEN": "env:TOKEN"}
    candidate.handshake = Handshake(
        ok=False, transport=Transport.STDIO, credential_pending=["TOKEN"], error="TOKEN is required"
    )
    return candidate


def doctor(ctx):
    return EntryDoctor(ctx, McpWriter(ctx), CandidateVerifier(ctx))


# -- the error names the call that failed --------------------------------------


def test_the_frame_that_raised_survives_and_layout_lines_do_not() -> None:
    traceback = [
        "Traceback (most recent call last):",
        'File "/srv/app.py", line 10, in main',
        "run()",
        'File "/usr/lib/python3/ssl.py", line 77, in create_default_context',
        "context.load_verify_locations(cafile, capath, cadata)",
        "~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^",
        "FileNotFoundError: [Errno 2] No such file or directory",
        ")",
        "^",
    ]
    lines = _failure_lines(traceback)
    assert lines[0] == "FileNotFoundError: [Errno 2] No such file or directory"
    assert "load_verify_locations" in " ".join(lines), "the call that failed is the useful part"
    assert ")" not in lines and "^" not in lines


def test_output_that_is_not_a_traceback_is_kept_as_it_was() -> None:
    assert _failure_lines(["listening on stdio", "ready"]) == ["listening on stdio", "ready"]


# -- the entry is launched the way the trial proved it -------------------------


def test_a_slot_the_trial_filled_is_filled_again_and_never_echoed(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    candidate = waiting(tmp_path, "atlassian")
    examined = doctor(live(ctx)).examine(candidate, "confluence-example-com", announce=False)
    assert examined.ok, examined.error
    placeholder = ctx.settings.str_("mcp.packages.credential_probe.placeholder")
    assert placeholder not in str(examined.to_dict())


def test_a_server_proven_to_start_without_its_credential_gets_no_stand_in(tmp_path) -> None:
    candidate = waiting(tmp_path, "optional")
    candidate.handshake = Handshake(ok=True, credential_pending=["TOKEN"], tools=["t"], round_trip=True)
    assert EntryDoctor.standins(candidate, {"env": {"TOKEN": "${TOKEN:-}"}}) == []


def test_only_the_credential_slots_are_stand_ins(tmp_path) -> None:
    candidate = waiting(tmp_path, "atlassian")
    entry = {"env": {"TOKEN": "${CONFLUENCE_TOKEN}", "BASE_URL": "${OTHER}"}}
    assert EntryDoctor.standins(candidate, entry) == ["CONFLUENCE_TOKEN"]


# -- a failure says how the two launches differ --------------------------------


def test_a_failure_names_what_the_proven_launch_had_and_the_entry_lacks(ctx, tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("not a certificate")
    monkeypatch.setenv("ICA_TEST_BUNDLE_FILE", str(bundle))
    candidate = waiting(tmp_path, "broken", BROKEN)
    candidate.env["TRIAL_ONLY"] = "yes"
    lines = doctor(live(ctx)).launch_difference(candidate, {"command": sys.executable, "env": {}})
    text = " ".join(lines)
    assert "TRIAL_ONLY" in text
    assert "ICA_TEST_BUNDLE_FILE" in text and str(bundle) not in text, "names, never values"


def test_differences_can_be_switched_off(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.healing.report_differences", False)
    candidate = waiting(tmp_path, "broken", BROKEN)
    candidate.env["TRIAL_ONLY"] = "yes"
    assert doctor(live(ctx)).launch_difference(candidate, {"command": sys.executable}) == []


# -- the stage examines servers waiting for a credential -----------------------


def stage_of(*candidates: Candidate) -> StageOutcome:
    for candidate in candidates:
        candidate.origin = Stage.PACKAGE
    return StageOutcome(stage=Stage.PACKAGE, status=Status.PARTIAL, candidates=list(candidates))


def test_one_that_will_not_start_hands_its_place_to_the_next(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    broken = waiting(tmp_path, "existing", BROKEN, confidence=0.9)
    working = waiting(tmp_path, "proposed", confidence=0.6)
    stage = stage_of(broken, working)
    pipeline = TraditionalPipeline(live(ctx))
    monkeypatch.setattr(pipeline, "registrable", lambda c, o: True)

    pipeline.heal_or_discard(stage, TargetOutcome(target=TARGET), provisional_limit=1)

    assert broken not in stage.candidates
    assert [c.name for c in stage.provisional] == ["proposed"]
    assert "proposed" in stage.detail and "will not start" in stage.detail
    assert any("FileNotFoundError" in r for r in stage.findings["unhealable"])


def test_when_none_starts_the_stage_does_not_settle(ctx, tmp_path, monkeypatch) -> None:
    stage = stage_of(waiting(tmp_path, "existing", BROKEN))
    pipeline = TraditionalPipeline(live(ctx))
    target = TargetOutcome(target=TARGET)
    pipeline.heal_or_discard(stage, target, provisional_limit=1)
    target.absorb(stage, 1)
    assert target.provisional == [], "a server that cannot start must not close the lower tiers"
    assert stage.status == Status.PARTIAL


def test_examining_waiting_servers_can_be_switched_off(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.healing.examine_pending", False)
    broken = waiting(tmp_path, "existing", BROKEN)
    stage = stage_of(broken)
    TraditionalPipeline(live(ctx)).heal_or_discard(stage, TargetOutcome(target=TARGET))
    assert broken in stage.candidates


# -- publish tries the next proven candidate -----------------------------------


def test_publish_falls_back_to_the_next_proven_candidate(ctx, tmp_path, monkeypatch, sandbox) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    ctx = live(ctx)
    monkeypatch.setattr(McpWriter, "_register", lambda self, r: None)
    broken = waiting(tmp_path, "existing", BROKEN, confidence=0.9)
    working = waiting(tmp_path, "proposed", confidence=0.6)
    outcome = TargetOutcome(target=TARGET, provisional=[broken])
    outcome.stages.append(stage_of(broken, working))

    registrations = McpWriter(ctx).publish(outcome)

    assert registrations[0].refused and registrations[0].candidate is broken
    assert "proposed was tried in its place" in registrations[0].detail
    assert registrations[1].candidate is working and not registrations[1].refused
    assert registrations[1].name == registrations[0].name
    assert outcome.registered == [registrations[1].name]


def test_fallbacks_can_be_switched_off(ctx, tmp_path, monkeypatch) -> None:
    ctx = live(ctx)
    ctx.settings.set("mcp.registration.start_fallbacks", 0)
    monkeypatch.setattr(McpWriter, "_register", lambda self, r: None)
    broken = waiting(tmp_path, "existing", BROKEN)
    outcome = TargetOutcome(target=TARGET, provisional=[broken])
    outcome.stages.append(stage_of(broken, waiting(tmp_path, "proposed")))
    assert len(McpWriter(ctx).publish(outcome)) == 1


# -- an address is never reported empty while the machine still serves it ------


def test_an_existing_registration_that_answered_is_kept_and_said(ctx, tmp_path, monkeypatch) -> None:
    ctx = live(ctx)
    ctx.settings.set("mcp.registration.start_fallbacks", 0)
    broken = waiting(tmp_path, "existing", BROKEN)
    outcome = TargetOutcome(target=TARGET, provisional=[broken])
    outcome.answered_existing["confluence-example-com"] = "claude_cli"

    McpWriter(ctx).publish(outcome)

    [kept] = outcome.kept_existing
    assert kept["name"] == "confluence-example-com"
    assert "claude mcp get confluence-example-com" in kept["remediation"]
    assert outcome.to_dict()["kept_existing"][0]["source"] == "claude_cli"


def test_the_verdict_never_counts_a_refused_entry_as_waiting(ctx, tmp_path) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ctx = live(ctx)
    ctx.settings.set("mcp.registration.start_fallbacks", 0)
    writer = McpWriter(ctx)
    outcome = TargetOutcome(target=TARGET, provisional=[waiting(tmp_path, "existing", BROKEN)])
    outcome.answered_existing["confluence-example-com"] = "claude_cli"
    writer.publish(outcome)

    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._report_verdict(result, writer, [outcome])
    detail = result.steps[-1].detail
    assert "EMPTY credential" not in detail
    assert "Kept as they were: confluence-example-com" in detail
    assert result.data["mcp_kept_existing"] == ["confluence-example-com"]


# -- one wording for what a waiting server is ----------------------------------


def test_a_local_server_is_never_called_an_endpoint_that_refused() -> None:
    local = Candidate(kind="stdio", name="pkg", command=["npx", "pkg"])
    remote = Candidate(kind="url", url="https://mcp.example.com/mcp")
    assert "refused an anonymous client" not in waits_for_credential(local)
    assert "refused an anonymous client" in waits_for_credential(remote)


# -- the agent's repair is actually asked for ----------------------------------


def test_the_agent_repair_returns_its_candidate(ctx, tmp_path, monkeypatch) -> None:
    """It used to raise TypeError after the turn was paid for, every time."""
    from types import SimpleNamespace

    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.runtime import AgentResult

    pipeline = AgenticPipeline(ctx)
    spec = SimpleNamespace(name="mcp-strategist", turn_budget=lambda *_a, **_k: 10)
    monkeypatch.setattr(pipeline, "available", lambda: (True, ""))
    monkeypatch.setattr(pipeline, "_agent_for", lambda *_a, **_k: spec)
    monkeypatch.setattr(pipeline, "prepare_workspace", lambda: tmp_path)
    revised_launch = {"kind": "stdio", "name": "fixed", "command": [sys.executable, "-V"], "env": {"BASE_URL": TARGET}}
    pipeline.runtime = SimpleNamespace(
        run=lambda *_a, **_k: AgentResult(ok=True, payload={"candidates": [revised_launch]}, turns=3)
    )
    original = waiting(tmp_path, "broken", BROKEN)

    revised = pipeline._agent_repair(original, "confluence-example-com", "FileNotFoundError")

    assert revised is not None and revised.name == "fixed"
    assert revised.handshake is original.handshake, "the proof is carried, the launch re-proved"


# -- what was written is what counts -------------------------------------------


def test_a_refused_candidate_never_counts_as_working(ctx, tmp_path) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ctx = live(ctx)
    ctx.settings.set("mcp.registration.start_fallbacks", 0)
    broken = waiting(tmp_path, "existing", BROKEN)
    broken.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)
    broken.secret_env = {}
    outcome = TargetOutcome(target=TARGET, verified=[broken])
    registrations = McpWriter(ctx).publish(outcome)
    assert registrations[0].refused

    McpPhase._settle_found(outcome, registrations)

    assert not outcome.succeeded, "nothing was written, so nothing works"
    assert outcome.refused == [broken]
    assert outcome.to_dict()["refused"][0]["name"] == "existing"
    assert McpPhase._verdict_on(outcome) != Status.FAILED, "found and refused is still partial"


def test_publish_does_not_relaunch_what_the_stage_proved(ctx, tmp_path, monkeypatch) -> None:
    ctx = live(ctx)
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setattr(McpWriter, "_register", lambda self, r: None)
    candidate = waiting(tmp_path, "proposed")
    stage = stage_of(candidate)
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "registrable", lambda c, o: True)
    target = TargetOutcome(target=TARGET)
    pipeline.heal_or_discard(stage, target, provisional_limit=1)
    target.absorb(stage, 1)

    writer = McpWriter(ctx)
    launches: list[str] = []
    real = writer.verifier.verify_as_written

    def counted(entry, name="", standins=(), kept=None):
        launches.append(name)
        return real(entry, name, standins=standins, kept=kept)

    writer.verifier.verify_as_written = counted  # type: ignore[assignment]
    [registration] = writer.publish(target)
    assert not registration.refused
    assert launches == [], "the stage already launched exactly this entry"


def test_a_backup_is_never_a_copy_of_what_was_chosen(ctx, tmp_path) -> None:
    chosen = waiting(tmp_path, "proposed")
    copy = waiting(tmp_path, "proposed")
    outcome = TargetOutcome(target=TARGET, provisional=[chosen])
    outcome.stages.append(stage_of(chosen, copy))
    assert McpWriter(live(ctx)).start_backups(outcome, [chosen]) == []


def test_no_waiting_backup_while_a_working_server_was_found(ctx, tmp_path) -> None:
    working = waiting(tmp_path, "working")
    working.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)
    other = waiting(tmp_path, "other")
    outcome = TargetOutcome(target=TARGET, verified=[working])
    outcome.stages.append(stage_of(working, other))
    assert McpWriter(live(ctx)).start_backups(outcome, [working]) == []


def test_an_agent_repair_becomes_the_candidates_launch(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    broken = waiting(tmp_path, "existing", BROKEN)
    script = tmp_path / "fixed.py"
    script.write_text(SERVER)

    def repair(candidate, name, failure):
        fixed = waiting(tmp_path, "existing")
        fixed.command = [sys.executable, str(script)]
        return fixed

    healing = doctor(live(ctx)).examine(broken, "confluence-example-com", agent_repair=repair, announce=False)
    assert healing.ok and healing.applied == ["agent"]
    assert broken.command == [sys.executable, str(script)], "publish builds from the repaired launch"
