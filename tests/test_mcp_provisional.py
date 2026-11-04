"""An MCP endpoint that answers 401 is a find, not a dead end.

The tier rules already treat an authentication wall as proof that the target
runs its own MCP server -- it is what closes tiers 2 and 3. What used to be
missing is the other half: the endpoint was recognised, and then nothing was
written, so an operator who went and got a token had to re-run the whole search
before they could use it.

These tests pin the route such an endpoint now travels: picked out of the
probe's own results, carried to the end of the run beside (and apart from) the
servers that really answered, written into ``.mcp.json`` with the credential
left empty, and never once counted as working.

The second half of the file pins the same route through the *agentic* pipeline,
which used to have a different one. A 401 there read as "no handshake", so the
stage spent its remaining attempts re-finding the same URL and the wall was
never written into the tier-1 record -- which left the package and bridge
stages open to go looking for a replacement for a server already found.
"""

from __future__ import annotations

import json

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline, _best_of
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
from initialize_claude_ai.mcp_auto_discovery.runtime import AgentResult
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase


def walled(url: str = "https://mcp.example.test/mcp", confidence: float = 0.5) -> Candidate:
    """An endpoint that answered by demanding a credential."""
    return Candidate(
        kind="url",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        confidence=confidence,
        handshake=Handshake(
            ok=False,
            status=401,
            requires_auth=True,
            auth_hint="Bearer",
            # Without this the candidate is a login page, not an endpoint --
            # see "a 401 is only a find when it looked like one" below.
            auth_evidence=["it answered application/json, not a web page"],
        ),
    )


def working(url: str = "https://mcp.example.test/sse") -> Candidate:
    return Candidate(
        kind="url",
        url=url,
        transport=Transport.SSE,
        handshake=Handshake(ok=True, server_name="Example", tools=["a"], round_trip=True),
    )


def missing(url: str = "https://mcp.example.test/none") -> Candidate:
    return Candidate(kind="url", url=url, handshake=Handshake(ok=False, status=404))


# -- picking them out ----------------------------------------------------------


def test_a_candidate_that_was_refused_is_told_apart_from_one_that_failed() -> None:
    assert walled().needs_credential is True
    assert missing().needs_credential is False
    assert working().needs_credential is False
    assert Candidate(kind="url", url="x").needs_credential is False, "never probed is not a find"


def test_the_scheme_is_read_off_the_challenge_rather_than_assumed() -> None:
    assert Handshake(auth_hint='Basic realm="x"').auth_scheme == "Basic"
    assert Handshake(auth_hint="Bearer").auth_scheme == "Bearer"
    assert Handshake(auth_hint="").auth_scheme == ""
    assert Handshake(auth_hint="401").auth_scheme == "", "a status code is not a scheme"


def test_only_the_best_ranked_wall_is_kept_by_default() -> None:
    """A host entirely behind a login answers 401 on every path probed."""
    stage = StageOutcome(
        stage=Stage.ENDPOINT,
        candidates=[
            walled("https://mcp.example.test/a", confidence=0.2),
            walled("https://mcp.example.test/b", confidence=0.9),
            missing(),
        ],
    )
    assert [c.url for c in stage.note_provisional(limit=1)] == ["https://mcp.example.test/b"]


def test_exhaustive_keeps_every_one_of_them() -> None:
    stage = StageOutcome(
        stage=Stage.ENDPOINT,
        candidates=[walled("https://mcp.example.test/a"), walled("https://mcp.example.test/b")],
    )
    assert len(stage.note_provisional(limit=0)) == 2


def test_an_endpoint_that_verified_is_not_also_waiting_on_anything() -> None:
    candidate = walled()
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[candidate], verified=[candidate])
    assert stage.note_provisional() == []


def test_the_limit_is_configuration_and_exhaustive_overrides_it(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    assert pipeline.provisional_limit(exhaustive=False) == 1
    assert pipeline.provisional_limit(exhaustive=True) == 0
    ctx.settings.set("mcp.discovery.auth.max_provisional", "3")
    assert TraditionalPipeline(ctx).provisional_limit(exhaustive=False) == 3


def test_a_target_absorbs_both_kinds_of_find_without_mixing_them() -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[walled()], verified=[working()])

    outcome.absorb(stage)

    assert [c.url for c in outcome.verified] == ["https://mcp.example.test/sse"]
    assert [c.url for c in outcome.provisional] == ["https://mcp.example.test/mcp"]
    assert outcome.succeeded is True, "the working one is what makes the address a success"


def test_absorbing_the_same_stage_twice_adds_nothing() -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[walled()])
    outcome.absorb(stage)
    outcome.absorb(stage)
    assert len(outcome.provisional) == 1


# -- what the phase does with them ---------------------------------------------


def finalise(ctx, outcome: TargetOutcome) -> tuple[PhaseResult, McpWriter]:
    ctx.settings.set("mcp.verification.final_recheck", "false")
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    writer = McpWriter(ctx)
    McpPhase(ctx)._finalise(result, writer, outcome)
    return result, writer


def test_an_address_with_only_a_walled_endpoint_is_still_registered(ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test", provisional=[walled()])

    result, writer = finalise(ctx, outcome)

    assert [r.name for r in writer.published] == ["mcp-example-test"]
    assert writer.published[0].provisional is True
    assert outcome.status == Status.PARTIAL, "found and written is not the same as working"
    assert "empty credential" in outcome.detail
    assert "MCP_EXAMPLE_TEST_TOKEN" in outcome.detail
    assert any("register" in step.name for step in result.steps)


def test_a_working_server_beats_one_that_would_need_a_token(ctx) -> None:
    """The finding stays in the report; a dormant entry does not join a live one."""
    outcome = TargetOutcome(target="https://mcp.example.test", verified=[working()], provisional=[walled()])

    _result, writer = finalise(ctx, outcome)

    assert [r.name for r in writer.published] == ["mcp-example-test"]
    assert writer.published[0].provisional is False
    assert outcome.provisional == []
    assert outcome.status == Status.OK


def test_exhaustive_registers_both(ctx) -> None:
    ctx.settings.set("mcp.exhaustive", "true")
    outcome = TargetOutcome(target="https://mcp.example.test", verified=[working()], provisional=[walled()])

    _result, writer = finalise(ctx, outcome)

    assert [r.provisional for r in writer.published] == [False, True]
    assert len({r.name for r in writer.published}) == 2


def test_nothing_at_all_still_reports_nothing_at_all(ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")

    _result, writer = finalise(ctx, outcome)

    assert writer.published == []
    assert outcome.detail.startswith("nothing registered -- ")
    assert "no working MCP server was found" in outcome.detail


# -- what the agent's stage does with them --------------------------------------
#
# The endpoint an agent proposes takes the same route as one a probe found, and
# that has to include the route a 401 travels. It did not: the agentic stage
# read "no handshake" and spent its remaining attempts -- five to eight minutes
# each -- re-finding the same URL, then handed the stage to the deterministic
# pipeline to probe a hundred and sixty paths for it a fourth time. Every one of
# them ended with the entry the first attempt already had.


def agent_says(*candidates: dict, summary: str = "") -> AgentResult:
    payload: dict = {"candidates": list(candidates)}
    if summary:
        payload["summary"] = summary
    return AgentResult(ok=True, runtime="test", payload=payload)


def ingest(ctx, result: AgentResult, verdicts: dict[str, Handshake]):
    """Run one agent payload through the stage, with the probe stubbed out."""
    pipeline = AgenticPipeline(ctx)
    pipeline.verifier.verify = lambda c: (  # type: ignore[assignment]
        setattr(c, "handshake", verdicts.get(c.url or c.label, Handshake(ok=False, status=404))) or c
    )
    stage = StageOutcome(stage=Stage.ENDPOINT, mode="agentic")
    target = TargetOutcome(target="https://example.test")
    pipeline._ingest(stage, result, target)
    return pipeline, stage, target


REFUSED = Handshake(
    ok=False,
    status=401,
    requires_auth=True,
    error="authentication required (HTTP 401)",
    auth_hint="Bearer",
    auth_evidence=["it answered application/json, not a web page"],
)


def test_an_endpoint_that_asked_for_a_credential_settles_the_stage(ctx) -> None:
    pipeline, stage, target = ingest(
        ctx,
        agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
        {"https://mcp.example.test/mcp": REFUSED},
    )

    assert pipeline._stage_is_conclusive(stage) is True, "no second search for the same 401"
    assert stage.status == Status.PARTIAL, "settled is not the same as working"
    assert stage.verified == []
    assert [c.url for c in stage.provisional] == ["https://mcp.example.test/mcp"]


def test_the_stage_says_what_answered_rather_than_what_did_not(ctx) -> None:
    _pipeline, stage, _target = ingest(
        ctx,
        agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
        {"https://mcp.example.test/mcp": REFUSED},
    )

    assert "HTTP 401" in stage.detail
    assert "refused an anonymous client" in stage.detail
    assert "none completed a handshake" not in stage.detail
    assert any("credential slot empty" in line for line in stage.remediation)


def test_the_wall_is_written_into_the_checklist_so_the_lower_tiers_stay_shut(ctx) -> None:
    """The omission that let the package agent go looking for a replacement."""
    pipeline, _stage, target = ingest(
        ctx,
        agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
        {"https://mcp.example.test/mcp": REFUSED},
    )

    assert target.due_diligence.auth_walls == ["https://mcp.example.test/mcp"]
    assert target.due_diligence.native_exists is True
    allowed, why = pipeline.policy.gate(Stage.PACKAGE, target.due_diligence)
    assert allowed is False
    assert "behind authentication" in why


def test_an_operator_can_ask_for_the_search_to_go_on_anyway(ctx) -> None:
    ctx.settings.set("mcp.agent.credential_wall_settles_stage", "false")
    pipeline, stage, _target = ingest(
        ctx,
        agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
        {"https://mcp.example.test/mcp": REFUSED},
    )

    assert pipeline._stage_is_conclusive(stage) is False
    assert [c.url for c in stage.provisional] == ["https://mcp.example.test/mcp"], (
        "the finding is still kept; only the retrying changes"
    )


def test_nothing_answering_still_retries_and_now_says_why(ctx) -> None:
    _pipeline, stage, target = ingest(
        ctx,
        agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
        {"https://mcp.example.test/mcp": Handshake(ok=False, status=404, error="HTTP 404")},
    )

    assert stage.provisional == []
    assert "none completed a handshake" in stage.detail
    assert "HTTP 404" in stage.detail, "the verifier's reason, not just the count"
    assert target.due_diligence is None


def test_an_agent_that_claims_success_and_proposes_nothing_is_quoted_as_such(ctx) -> None:
    """The log read 'did not settle it (a real initialize handshake completed)'."""
    _pipeline, stage, _target = ingest(
        ctx,
        AgentResult(
            ok=True,
            runtime="test",
            payload={"summary": "Native MCP endpoint confirmed. A real handshake completed."},
        ),
        {},
    )

    assert stage.detail.startswith("the agent proposed nothing to verify; it reported:")
    assert "A real handshake completed." in stage.detail


def test_a_walled_endpoint_outranks_an_attempt_that_found_more_of_nothing() -> None:
    """Retrying must never lose the one candidate that actually answered."""
    found_it = StageOutcome(stage=Stage.ENDPOINT, candidates=[walled()])
    found_noise = StageOutcome(stage=Stage.ENDPOINT, candidates=[missing(), missing(), missing()])

    assert _best_of(found_it, found_noise) is found_it
    assert _best_of(found_noise, found_it) is found_it


def test_the_deterministic_fallback_does_not_erase_what_the_agent_found(ctx) -> None:
    """Both pipelines write the same record; the second must not assign over it."""
    target = TargetOutcome(target="https://mcp.example.test")
    target.due_diligence = DueDiligence(target=target.target, source="agent")
    target.due_diligence.auth_walls.append("https://mcp.example.test/mcp")

    TraditionalPipeline(ctx).stage_endpoint(target)

    assert "https://mcp.example.test/mcp" in target.due_diligence.auth_walls


def test_the_whole_address_finishes_on_the_first_attempt(ctx) -> None:
    """The payoff: one agent turn, not three plus a re-sweep plus a package hunt."""
    pipeline = AgenticPipeline(ctx)
    pipeline.available = lambda: (True, "")  # type: ignore[assignment]
    pipeline.verifier.verify = lambda c: (  # type: ignore[assignment]
        setattr(c, "handshake", REFUSED) or c
    )
    turns: list[str] = []

    def one_turn(name, target, *, attempt, attempts, plan):
        turns.append(f"{name}:{attempt}")
        stage = StageOutcome(stage=name, mode="agentic")
        pipeline._ingest(
            stage,
            agent_says({"kind": "url", "url": "https://mcp.example.test/mcp"}),
            target,
        )
        return stage

    pipeline._agent_stage = one_turn  # type: ignore[assignment]
    outcome = pipeline.run("https://mcp.example.test", [Stage.ENDPOINT, Stage.PACKAGE, Stage.BRIDGE])

    assert turns == ["endpoint:1"], "no second search, and no lower tier to search in"
    assert [c.url for c in outcome.provisional] == ["https://mcp.example.test/mcp"]
    assert outcome.verified == []
    assert [s.status for s in outcome.stages[1:]] == [Status.SKIPPED, Status.SKIPPED]


# -- products whose credential is not a single opaque token ---------------------
#
# "Export OPENQA_OPENSUSE_ORG_TOKEN" is a complete instruction only where the
# product issues a token. openQA issues an API key and a secret, and its MCP
# endpoint wants them joined to the login name -- so an operator following that
# advice goes looking for something that does not exist. The exceptions are
# configuration, matched on the host.


def writer_for(ctx) -> McpWriter:
    ctx.settings.set("mcp.verification.final_recheck", "false")
    return McpWriter(ctx)


def test_the_variable_is_named_after_the_shape_of_its_value(ctx) -> None:
    """The name is the instruction: read it and you know what to set.

    ``OPENQA_..._API_KEY`` reads as "put the key here", and an operator who did
    exactly that got a 403 with nothing to explain it -- the value is three
    parts joined by colons.
    """
    writer = writer_for(ctx)
    candidate = walled("https://openqa.opensuse.org/mcp")
    assert writer.credential_env_name(candidate, "openqa-opensuse-org") == ("OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET")


def test_it_says_what_the_value_looks_like_and_where_to_get_it(ctx) -> None:
    hint = writer_for(ctx).credential_hint(walled("https://openqa.example.net/mcp"))
    assert "username:key:secret" in hint
    assert "not a single opaque token" in hint
    assert "https://openqa.example.net/api_keys" in hint, "{host} is substituted, not printed"


def test_a_target_with_no_profile_still_gets_the_generic_name(ctx) -> None:
    writer = writer_for(ctx)
    candidate = walled("https://gitlab.example.com/api/v4/mcp")
    assert writer.credential_env_name(candidate, "gitlab-example-com") == ("GITLAB_EXAMPLE_COM_TOKEN")
    # No profile means we do not know what this product issues -- which is worth
    # saying, because the variable is called ..._TOKEN and that is a guess.
    hint = writer.credential_hint(candidate)
    assert "check its own documentation" in hint
    assert "username:key:secret" not in hint, "that is openQA's shape, not everyone's"


def test_a_known_shape_beats_a_name_that_contradicts_it(ctx) -> None:
    """An agent's *name* loses to a *shape*, including the agent's own.

    The agent may name the variable after one part of a multi-part value --
    which is exactly what happened -- so a name is only followed when nothing
    better describes the value.
    """
    candidate = walled("https://openqa.opensuse.org/mcp")
    candidate.secret_env["OPENQA_API_TOKEN"] = "env:OPENQA_API_TOKEN"
    assert writer_for(ctx).credential_env_name(candidate, "openqa-opensuse-org") == (
        "OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET"
    )


def test_a_name_is_still_followed_when_nothing_knows_the_shape(ctx) -> None:
    """An agent that read the vendor's docs outranks the generic template."""
    candidate = walled("https://gitlab.example.com/mcp")
    candidate.secret_env["GITLAB_PERSONAL_ACCESS_TOKEN"] = "env:GITLAB_PERSONAL_ACCESS_TOKEN"
    assert writer_for(ctx).credential_env_name(candidate, "gitlab-example-com") == ("GITLAB_PERSONAL_ACCESS_TOKEN")


def test_the_operators_own_reference_outranks_everything(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.bearer_token_ref", "env:MY_OWN_VAR")
    writer = writer_for(ctx)
    assert writer.credential_env_name(walled("https://openqa.example.net/mcp"), "x") == "MY_OWN_VAR"


def test_an_unusable_match_pattern_is_skipped_not_fatal(ctx, caplog) -> None:
    ctx.settings.set("mcp.discovery.auth.profiles.broken.match", "([unclosed")
    ctx.settings.set("mcp.discovery.auth.profiles.broken.env", "NOPE")
    with caplog.at_level("WARNING"):
        name = writer_for(ctx).credential_env_name(walled("https://gitlab.example.com/mcp"), "gitlab-example-com")
    assert name == "GITLAB_EXAMPLE_COM_TOKEN"
    assert "broken" in caplog.text


def test_the_shape_reaches_the_operator_not_just_the_variable_name(ctx) -> None:
    """The briefing and the remediation both have to carry it."""
    outcome = TargetOutcome(
        target="https://openqa.opensuse.org", provisional=[walled("https://openqa.opensuse.org/mcp")]
    )
    result, writer = finalise(ctx, outcome)

    registration = writer.published[0]
    assert registration.needs_env == ["OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET"]
    assert "username:key:secret" in registration.credential_hint
    assert any("username:key:secret" in line for line in registration.remediation)

    briefing = json.dumps(writer.briefing(writer.published))
    assert "username:key:secret" in briefing
    assert "api_keys" in briefing
    assert result.steps, "the phase still reports the registration"
