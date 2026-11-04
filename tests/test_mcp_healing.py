"""An entry that will not start is repaired, or the target keeps looking.

Where this happens is the whole point of the refactor.

The check used to run at publish, after every stage had finished. That put it
too late for the two things that would actually help:

* the **agent** could not be asked, because its turn was over; and
* the **tier descent** could not respond, because it had already happened -- so
  a candidate whose entry would never start still occupied its target's answer,
  and tiers 3 and 4 were never tried.

It now runs inside the stage loop, beside ``discard_unregistrable``, where "we
found a server" and "we can write this server down" were already asked together.
A candidate that cannot be repaired is dropped there; the stage does not settle;
the lower tiers run. Nothing had to be arranged for that -- it falls out of
asking the question at the right moment.

The order of remedies mirrors the tier policy: the agent first, because it
can change more than any one rule; then the deterministic repairs,
which need no model and run under ``--no-agent``; then neither, and the
candidate is discarded with everything that was tried recorded.
"""

from __future__ import annotations

import json
import sys

from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://bugzilla.opensuse.org"

#: A server that negotiates, lists a tool and answers again -- the three things
#: the prober requires. Written to disk by the fixtures below.
SERVER = """
import json, os, sys
if not os.environ.get("NEEDED", "") and "REQUIRE" in os.environ.get("MODE", ""):
    sys.stderr.write("NEEDED is not set\\n"); raise SystemExit(1)
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
                  "serverInfo": {"name": "honest", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ping", "description": "",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def live(ctx):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: f"/usr/bin/{name}"  # type: ignore[assignment]
    return ctx


def server_script(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return script


def candidate_needing(tmp_path, **env) -> Candidate:
    """A candidate whose server refuses to start without NEEDED."""
    candidate = Candidate(
        kind="stdio",
        name="needs-env",
        transport=Transport.STDIO,
        command=[sys.executable, str(server_script(tmp_path))],
        env={"MODE": "REQUIRE", "BASE_URL": TARGET, **env},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, server_name="x", tools=["t"], round_trip=True)
    return candidate


def doctor(ctx):
    return EntryDoctor(ctx, McpWriter(ctx), CandidateVerifier(ctx))


# --------------------------------------------------------------------------- #
# the deterministic half -- which is also what --no-agent gets
# --------------------------------------------------------------------------- #


def test_an_entry_that_starts_is_left_alone(ctx, tmp_path) -> None:
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    healing = doctor(live(ctx)).examine(candidate, "bugzilla-opensuse-org")
    assert healing.ok and not healing.applied, "nothing was wrong, so nothing was changed"


def test_a_missing_variable_is_declared_and_re_proved(ctx, tmp_path) -> None:
    """The commonest repair, and the mechanism behind `starts` vs `claude`.

    Our trial inherits the operator's environment; a client passes only what the
    entry declares. So the value is known -- the candidate has it -- and simply
    never reached the entry.
    """
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    doc = doctor(live(ctx))
    original = doc.writer.entry_for
    doc.writer.entry_for = lambda c, n="": {  # type: ignore[assignment]
        **original(c, n),
        "env": {"MODE": "REQUIRE"},  # NEEDED dropped on the way out
    }

    healing = doc.examine(candidate, "bugzilla-opensuse-org")

    assert healing.ok
    assert healing.applied == ["declare_inherited_env"]
    assert healing.entry["env"]["NEEDED"] == "yes"


def test_a_repair_that_does_not_help_is_recorded_and_discarded(ctx) -> None:
    candidate = Candidate(
        kind="stdio",
        name="dies",
        transport=Transport.STDIO,
        command=["/bin/false"],
        env={"BASE_URL": TARGET},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)

    healing = doctor(live(ctx)).examine(candidate, "bugzilla-opensuse-org")

    assert not healing.ok
    assert healing.applied == [], "nothing worked, so nothing is claimed"
    assert healing.attempts, "but what was tried is on the record"
    assert all(":" in line for line in healing.attempts)


def test_nothing_launchable_is_not_a_verdict(ctx) -> None:
    """A remote entry starts no process; a dry run starts none either.

    Neither is a failure, and treating "nobody asked" as "it does not work"
    would refuse every URL entry in the file.
    """
    remote = Candidate(
        kind="url",
        name="r",
        url="https://mcp.example.test/mcp",
        transport=Transport.STREAMABLE_HTTP,
    )
    healing = doctor(live(ctx)).examine(remote, "r")
    assert healing.ok and healing.tested is False


def test_healing_can_be_switched_off(ctx) -> None:
    ctx.settings.set("mcp.healing.enabled", False)
    candidate = Candidate(kind="stdio", name="d", transport=Transport.STDIO, command=["/bin/false"])
    healing = doctor(live(ctx)).examine(candidate, "d")
    # The repairs are off; the question is not. An entry that does not start
    # is still judged, and nothing is changed to make it start.
    assert healing.tested and not healing.ok
    assert healing.applied == []
    assert any("mcp.healing.enabled" in a for a in healing.attempts)


# --------------------------------------------------------------------------- #
# the agent half, and the fallback to the rules
# --------------------------------------------------------------------------- #


def test_the_agent_is_asked_first_and_its_fix_is_re_proved(ctx, tmp_path) -> None:
    """It can change more than any one rule, so it goes first.

    What it may not do is decide whether the fix worked: the revised candidate
    is launched like any other, and kept only if it starts.
    """
    broken = candidate_needing(tmp_path)  # no NEEDED anywhere
    fixed = candidate_needing(tmp_path, NEEDED="yes")
    seen: list[str] = []

    def repair(candidate, name, failure):
        seen.append(failure)
        return fixed

    healing = doctor(live(ctx)).examine(broken, "bugzilla-opensuse-org", agent_repair=repair)

    assert healing.ok and healing.applied == ["agent"]
    assert seen and "NEEDED is not set" in seen[0], "the agent is told what went wrong"


def test_an_agent_fix_that_does_not_work_falls_back_to_the_rules(ctx, tmp_path) -> None:
    """The fallback, and the reason the agent is not trusted on its own word."""
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    doc = doctor(live(ctx))
    original = doc.writer.entry_for
    doc.writer.entry_for = lambda c, n="": {  # type: ignore[assignment]
        **original(c, n),
        "env": {"MODE": "REQUIRE"},
    }

    healing = doc.examine(candidate, "bugzilla-opensuse-org", agent_repair=lambda c, n, f: None)

    assert healing.ok
    assert healing.applied == ["declare_inherited_env"], "the rules finished what the agent could not"
    assert any(line.startswith("agent:") for line in healing.attempts), "and the agent's turn is recorded"


def test_an_agent_that_raises_does_not_kill_the_stage(ctx, tmp_path) -> None:
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    doc = doctor(live(ctx))
    original = doc.writer.entry_for
    doc.writer.entry_for = lambda c, n="": {**original(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    def explode(candidate, name, failure):
        raise RuntimeError("boom")

    healing = doc.examine(candidate, "bugzilla-opensuse-org", agent_repair=explode)
    assert healing.ok, "the rules still ran"
    assert any("RuntimeError" in line for line in healing.attempts)


def test_no_agent_means_the_rules_alone(ctx, tmp_path) -> None:
    """--no-agent is not a degraded mode here: the same repairs run."""
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    doc = doctor(live(ctx))
    original = doc.writer.entry_for
    doc.writer.entry_for = lambda c, n="": {**original(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    healing = doc.examine(candidate, "bugzilla-opensuse-org", agent_repair=None)
    assert healing.ok and healing.applied == ["declare_inherited_env"]


def test_the_order_is_configuration(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.healing.agent_first", False)
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    doc = doctor(live(ctx))
    original = doc.writer.entry_for
    doc.writer.entry_for = lambda c, n="": {**original(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    asked: list[str] = []
    healing = doc.examine(candidate, "x", agent_repair=lambda c, n, f: asked.append(n) or None)
    assert healing.applied == ["declare_inherited_env"]
    assert asked == [], "the rules solved it, so the agent was never needed"


# --------------------------------------------------------------------------- #
# and the consequence the move was for: the lower tiers get their turn
# --------------------------------------------------------------------------- #


def stage_with(candidate) -> StageOutcome:
    stage = StageOutcome(stage="package")
    stage.verified = [candidate]
    stage.candidates = [candidate]
    return stage


def test_a_candidate_that_cannot_start_does_not_settle_its_stage(ctx) -> None:
    """The whole reason this moved out of publish.

    At publish the descent was over, so an entry that would never start still
    occupied the target's answer. Here the stage simply does not settle, and
    whatever comes next runs.
    """
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    candidate = Candidate(
        kind="stdio",
        name="dies",
        transport=Transport.STDIO,
        command=["/bin/false"],
        env={"BASE_URL": TARGET},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)
    stage = stage_with(candidate)
    outcome = TargetOutcome(target=TARGET)

    TraditionalPipeline(live(ctx)).heal_or_discard(stage, outcome)

    assert stage.verified == [], "it cannot start, so it is not an answer"
    assert stage.findings["unhealable"], "and the reason is recorded"
    assert "tried:" in stage.findings["unhealable"][0]


def test_a_repaired_candidate_settles_its_stage_normally(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    candidate = candidate_needing(tmp_path, NEEDED="yes")
    stage = stage_with(candidate)
    TraditionalPipeline(live(ctx)).heal_or_discard(stage, TargetOutcome(target=TARGET))

    assert stage.verified == [candidate]
    assert candidate.healing.ok


def test_what_healing_did_reaches_the_machine_readable_report(ctx, tmp_path) -> None:
    """Collected and not reported is the failure this file's history is full of.

    The screen says it and the log says it; the JSON report is what tooling
    reads, and it was the one place these never reached.
    """
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    writer = McpWriter(live(ctx))
    original = writer.entry_for
    writer.entry_for = lambda c, n="": {**original(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    registration = writer.publish(TargetOutcome(target=TARGET, verified=[candidate]))[0]
    reported = registration.to_dict()

    assert reported["repairs_applied"] == ["declare_inherited_env"]
    assert reported["repairs_attempted"], "what was tried, not only what worked"
    assert reported["left_an_earlier_entry_in_place"] is False


def test_the_candidate_carries_its_own_verdict_into_the_report(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    candidate = candidate_needing(tmp_path, NEEDED="yes")
    TraditionalPipeline(live(ctx)).heal_or_discard(stage_with(candidate), TargetOutcome(target=TARGET))
    assert candidate.to_dict()["healing"]["starts"] is True


def test_a_candidate_nobody_healed_reports_nothing_rather_than_false(ctx) -> None:
    """`None` is "not asked". Reporting False would invent a failure."""
    candidate = Candidate(kind="stdio", name="x", transport=Transport.STDIO, command=["x"])
    assert candidate.to_dict()["healing"] is None


def test_publishing_does_not_launch_the_entry_more_than_it_has_to(ctx, tmp_path) -> None:
    """Every launch is a subprocess and a handshake against somebody's software.

    Publishing used to examine the entry, repair it -- launching once per
    attempt -- and then launch the winner *again* just to obtain a verdict it
    already had. The doctor knows the answer it arrived at; asking it a second
    time is a process nobody needed.
    """
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    writer = McpWriter(live(ctx))
    original_entry = writer.entry_for
    writer.entry_for = lambda c, n="": {**original_entry(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    launches: list[str] = []
    real = writer.verifier.verify_as_written

    def counted(entry, name="", standins=(), kept=None):
        launches.append(name)
        return real(entry, name, standins=standins, kept=kept)

    writer.verifier.verify_as_written = counted  # type: ignore[assignment]
    registration = writer.publish(TargetOutcome(target=TARGET, verified=[candidate]))[0]

    assert registration.repairs == ["declare_inherited_env"]
    # Once to find it broken, once to prove the repair. Not three.
    assert len(launches) == 2, launches


# --------------------------------------------------------------------------- #
# when nothing can be repaired, say which files it depends on and how old they are
# --------------------------------------------------------------------------- #


def test_a_stale_half_of_a_generated_pair_is_named(ctx, tmp_path) -> None:
    """The fact that settles it, which nobody sees without running `ls -la`.

    A bridge is a script plus the data it reads, and the commonest way one
    breaks is that something regenerated half of it. openqa.example.net broke that
    way twice -- first as `AttributeError: 'list' object has no attribute
    'items'`, then, once the pair carried a stamp, as an honest refusal -- and
    both times the deciding fact was two timestamps a day apart.
    """
    import os
    import time

    script = tmp_path / "mcp_bridge_x.py"
    manifest = tmp_path / "x.operations.json"
    script.write_text("#")
    manifest.write_text("{}")
    # Comfortably clear of the day boundary: `old` is sampled after the script
    # is written, so an exact multiple would round down and the test would fail
    # on arithmetic rather than on behaviour.
    old = time.time() - 86400 * 2.5
    os.utime(manifest, (old, old))

    doc = doctor(live(ctx))
    lines = doc.file_evidence(
        {"command": "/usr/bin/python3", "args": [str(script)]},
        failure=f"cannot read {manifest}",
    )
    flagged = [line for line in lines if "apart" in line]
    assert flagged, lines
    assert "mcp_bridge_x.py" in flagged[0] and "x.operations.json" in flagged[0]
    assert "2 days" in flagged[0]


def test_files_that_merely_coexist_on_a_system_are_not_flagged(ctx, tmp_path) -> None:
    """An interpreter from last year beside a CA bundle from last month is an
    ordinary machine, not a stale pair. Only files that live *together* are
    compared -- sharing a directory is the closest this can get to "generated
    as a set" without being told."""
    script = tmp_path / "only.py"
    script.write_text("#")
    lines = doctor(live(ctx)).file_evidence(
        {"command": "/usr/bin/python3", "args": [str(script)], "env": {"CA": "/etc/hosts"}}
    )
    assert not any("apart" in line for line in lines), lines


def test_a_path_only_the_failure_mentions_is_still_examined(ctx, tmp_path) -> None:
    """A bridge finds its own manifest, so the entry never names it.

    The complaint is the only place it appears -- and it is exactly the file
    whose age matters.
    """
    manifest = tmp_path / "ops.json"
    manifest.write_text("{}")
    lines = doctor(live(ctx)).file_evidence({"command": "/usr/bin/python3"}, failure=f"could not use {manifest}.")
    assert any("ops.json" in line and "bytes" in line for line in lines), lines


def test_a_missing_file_says_missing(ctx, tmp_path) -> None:
    lines = doctor(live(ctx)).file_evidence({"command": "/usr/bin/python3", "args": [str(tmp_path / "gone.py")]})
    assert any("MISSING" in line for line in lines)


def test_the_evidence_is_only_gathered_when_it_is_needed(ctx, tmp_path) -> None:
    """A working entry gets no forensics -- there is nothing to explain."""
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    healing = doctor(live(ctx)).examine(candidate, "x")
    assert healing.ok and healing.files == []


def test_it_reaches_the_report(ctx) -> None:
    candidate = Candidate(
        kind="stdio",
        name="dies",
        transport=Transport.STDIO,
        command=["/bin/false"],
        env={"BASE_URL": TARGET},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)
    healing = doctor(live(ctx)).examine(candidate, "dies")
    assert healing.to_dict()["files"] == healing.files


def test_it_can_be_switched_off(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.healing.report_files", False)
    assert doctor(live(ctx)).file_evidence({"command": "/usr/bin/python3"}) == []


def test_both_pipelines_use_the_same_healer(ctx) -> None:
    """Not "the same idea" -- the same bound method, as with every shared gate."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    agentic = AgenticPipeline(ctx)
    assert agentic.fallback.heal_or_discard.__func__ is TraditionalPipeline.heal_or_discard


def test_the_writer_reaches_the_same_answer_at_publish(ctx, tmp_path) -> None:
    """The last word, and it must not contradict the stage that already asked."""
    candidate = candidate_needing(tmp_path, NEEDED="yes")
    writer = McpWriter(live(ctx))
    original = writer.entry_for
    writer.entry_for = lambda c, n="": {**original(c, n), "env": {"MODE": "REQUIRE"}}  # type: ignore[assignment]

    registration = writer.publish(TargetOutcome(target=TARGET, verified=[candidate]))[0]

    assert not registration.refused
    assert registration.repairs == ["declare_inherited_env"]
    assert json.loads(writer.json_path.read_text())["mcpServers"][registration.name]["env"]["NEEDED"] == "yes", (
        "the repaired entry is what was written"
    )
