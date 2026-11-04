"""Proof decides, not order: a guess may not block the bridge the run can build, nor blame a working credential.

sd.example.com, last run: in agentic mode the agent wrote its own bridge for the
Service Desk customer API. It first asked for a token *or* a user name and
password, then read SD_EXAMPLE_COM_USERNAME and SD_EXAMPLE_COM_PASSWORD without
declaring them, and was refused with 401. That ended the bridge stage. The
report said "its service rejected the credential this machine already uses",
and the bridge generated from the documented operations, which sends a bearer
token as the service documents, was never built. mcp-atlassian answered ``[]``
to every read tool with a working token and with a dead one alike, and on a
machine where it lives in the claude CLI, its CLI copy would have kept loading
in place of any bridge written to the file.
"""

from __future__ import annotations

import json

from initialize_claude_ai.mcp_auto_discovery import login_check, secret_policy
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    proof_rank,
    unconfirmed_refusal,
)

TARGET = "https://sd.example.test"
JSM = {
    "jira_service_management": {
        "label": "Jira Service Management customer",
        "url": TARGET + "/rest/servicedeskapi/info",
    }
}


def with_jsm(pipeline: TraditionalPipeline) -> TraditionalPipeline:
    pipeline.api_kinds_for = lambda outcome: dict(JSM)  # type: ignore[method-assign]
    return pipeline


def waiting(name: str, origin: str = Stage.PACKAGE, login: str = "", confidence: float = 0.5) -> Candidate:
    candidate = Candidate(
        kind="stdio", name=name, command=["python3", f"{name}.py"], origin=origin, confidence=confidence
    )
    candidate.handshake = Handshake(
        ok=True,
        tools=["t"],
        round_trip=True,
        credential_pending=["SD_TOKEN"],
        login=login,
        login_refused=login in login_check.REFUSED,
    )
    return candidate


# -- the bridge the run can build is tried before the address is settled ---------------------


def test_a_waiting_server_does_not_skip_the_generated_bridge(ctx) -> None:
    pipeline = with_jsm(TraditionalPipeline(ctx))
    outcome = TargetOutcome(target=TARGET, provisional=[waiting("pkg")])
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is None
    assert pipeline.settled(Stage.STRATEGY, outcome, exhaustive=False) is not None, "only the bridge stage generates"


def test_proved_access_or_a_tried_bridge_settles_as_before(ctx) -> None:
    pipeline = with_jsm(TraditionalPipeline(ctx))
    outcome = TargetOutcome(target=TARGET, provisional=[waiting("pkg", login=login_check.ACCEPTED)])
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is not None
    outcome = TargetOutcome(target=TARGET, provisional=[waiting("pkg")])
    outcome.stages.append(
        StageOutcome(stage=Stage.BRIDGE, findings={"generated_api_kinds": ["jira_service_management"]})
    )
    assert pipeline.untried_bridges(outcome) == []


def test_the_rule_can_be_turned_off(ctx) -> None:
    ctx.settings.set("mcp.discovery.generated_bridge_settles_first", False)
    pipeline = with_jsm(TraditionalPipeline(ctx))
    outcome = TargetOutcome(target=TARGET, provisional=[waiting("pkg")])
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is not None


def test_an_agent_bridge_that_proved_nothing_is_joined_by_the_generated_one(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    with_jsm(pipeline.fallback)
    agent_bridge = waiting("sd-example-test-bridge", origin=Stage.BRIDGE)
    agent_stage = StageOutcome(
        stage=Stage.BRIDGE, mode=Mode.AGENTIC, candidates=[agent_bridge], provisional=[agent_bridge]
    )
    generated = waiting("generated", origin=Stage.BRIDGE, login=login_check.ACCEPTED)
    generated.api_kind = "jira_service_management"
    delegated = StageOutcome(
        stage=Stage.BRIDGE,
        mode=Mode.TRADITIONAL,
        candidates=[generated],
        findings={"generated_api_kinds": ["jira_service_management"]},
    )
    monkeypatch.setattr(pipeline, "_agent_attempts", lambda name, target: (agent_stage, ""))
    monkeypatch.setattr(pipeline, "_delegate", lambda name, target: delegated)

    stage = pipeline._run_stage(Stage.BRIDGE, TargetOutcome(target=TARGET), agentic=True, unavailable="")
    assert agent_bridge in stage.candidates and generated in stage.candidates
    assert "also_tried" in stage.findings and "agentic_inconclusive" not in stage.findings


# -- candidates are ranked by proof ----------------------------------------------------------------


def test_proof_outranks_arriving_first() -> None:
    refused = waiting("agent", origin=Stage.BRIDGE, login=login_check.REJECTED, confidence=0.9)
    accepted = waiting("generated", origin=Stage.BRIDGE, login=login_check.ACCEPTED, confidence=0.1)
    assert proof_rank(accepted) < proof_rank(refused)
    stage = StageOutcome(stage=Stage.BRIDGE, candidates=[refused, accepted])
    assert stage.note_provisional(1) == [accepted]


def test_a_proven_candidate_takes_the_slot_of_an_unconfirmed_refusal() -> None:
    refused = waiting("agent", origin=Stage.BRIDGE, login=login_check.REJECTED)
    outcome = TargetOutcome(target=TARGET)
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[refused]))
    accepted = waiting("generated", origin=Stage.STRATEGY, login=login_check.ACCEPTED)
    outcome.absorb(StageOutcome(stage=Stage.STRATEGY, candidates=[accepted]))
    assert outcome.provisional == [accepted]


# -- a refusal is a verdict on the credential only when it is confirmed ------------------------


def test_a_refusal_from_a_bridge_nobody_checked_is_unconfirmed(settings) -> None:
    refused = waiting("agent", origin=Stage.BRIDGE, login=login_check.REJECTED)
    assert unconfirmed_refusal(refused) and login_check.keeps_searching(refused, settings)
    refused.auth_method_evidence = "the API description's security scheme"
    assert not unconfirmed_refusal(refused) and not login_check.keeps_searching(refused, settings)


def test_a_published_server_s_refusal_still_settles(settings) -> None:
    refused = waiting("pkg", origin=Stage.PACKAGE, login=login_check.REJECTED)
    assert not login_check.keeps_searching(refused, settings)


def test_unconfirmed_refusals_can_be_made_to_settle(settings) -> None:
    settings.set("mcp.verification.login_check.unconfirmed_rejection_settles", True)
    refused = waiting("agent", origin=Stage.BRIDGE, login=login_check.REJECTED)
    assert not login_check.keeps_searching(refused, settings)


def test_one_credential_accepted_and_refused_means_the_refuser_is_broken() -> None:
    fingerprint = secret_policy.credential_fingerprint(["a-credential-value"])
    refused = waiting("agent", origin=Stage.BRIDGE, login=login_check.REJECTED)
    refused.handshake.login_credential = fingerprint
    accepted = waiting("generated", origin=Stage.BRIDGE, login=login_check.ACCEPTED)
    accepted.handshake.login_credential = fingerprint
    outcome = TargetOutcome(target=TARGET)
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[refused, accepted]))
    assert outcome.provisional == [accepted]
    assert not refused.needs_credential and "accepted by generated" in refused.handshake.error


def test_the_fingerprint_never_carries_the_value() -> None:
    value = "a-credential-value"
    assert secret_policy.credential_fingerprint([value]) == secret_policy.credential_fingerprint([value])
    assert value not in secret_policy.credential_fingerprint([value])
    assert secret_policy.credential_fingerprint([]) == ""
    assert "login_credential" not in json.dumps(Handshake(login_credential="x").to_dict(), default=str)


# -- an unproved existing entry is replaced everywhere -----------------------------------------


def test_an_unproved_existing_entry_is_replaced_when_a_bridge_proved_access(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sd-example-test": {"command": "uvx", "args": ["mcp-atlassian"], "env": {"JIRA_URL": TARGET}},
                }
            }
        )
    )
    existing = ExistingRegistrations.for_run(ctx)
    old = existing.candidate(existing.registrations()[0], "package")
    old.handshake = Handshake(
        ok=True,
        tools=["jira_get_all_projects"],
        round_trip=True,
        login=login_check.UNCHECKED,
        login_detail="answered with nothing",
    )
    bridge = waiting("bridge", origin=Stage.BRIDGE, login=login_check.ACCEPTED)
    bridge.api_kind = "jira_service_management"
    outcome = TargetOutcome(target=TARGET, provisional=[bridge])
    outcome.stages.append(StageOutcome(stage=Stage.PACKAGE, candidates=[old]))
    found = McpWriter(ctx)._unusable_existing(outcome)
    assert found["sd-example-test"][0] is old and "never proved access" in found["sd-example-test"][1]

    ctx.settings.set("mcp.registration.replace_unproved_existing", False)
    assert "sd-example-test" not in McpWriter(ctx)._unusable_existing(outcome)


# -- what a script reads, and one variable per credential ------------------------------------------


def test_an_undeclared_credential_read_is_named(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    script = tmp_path / "bridge.py"
    script.write_text(
        'import os\nuser = os.environ.get("SD_EXAMPLE_TEST_USERNAME")\n'
        'password = os.environ.get("SD_EXAMPLE_TEST_PASSWORD")\n'
    )
    candidate = Candidate(
        kind="stdio", name="sd-example-test", command=["python3", str(script)], env={"MCP_BRIDGE_BASE_URL": TARGET}
    )
    env: dict[str, str] = {}
    McpWriter(ctx).declare_reads(candidate, "sd-example-test", env, [])
    assert any("PASSWORD" in name for name in candidate.undeclared_credentials)
    assert all(name in json.dumps(env) for name in candidate.undeclared_credentials)


def test_the_variable_an_existing_entry_reads_is_reused(ctx, sandbox) -> None:
    from initialize_claude_ai.core.secrets import Secret
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    value = "".join(chr(c) for c in (114, 101, 117, 115, 101, 45, 116, 111, 107, 101, 110, 45, 55))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sd-example-test": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": TARGET, "JIRA_PERSONAL_TOKEN": value},
                    },
                }
            }
        )
    )
    existing = ExistingRegistrations.for_run(ctx)
    existing.registrations()
    assert existing.credentials_for("sd.example.test")
    assert existing.variable_for(Secret(value)) == "SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN"

    bridge = Candidate(
        kind="stdio", name="sd-example-test", command=["python3", "bridge.py"], env={"MCP_BRIDGE_BASE_URL": TARGET}
    )
    bridge.secret_env = {"SD_EXAMPLE_TEST_TOKEN": "env:SD_EXAMPLE_TEST_TOKEN"}
    bridge.reuse_names = {"SD_EXAMPLE_TEST_TOKEN": "SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN"}
    bridge.handshake = Handshake(ok=True, tools=["t"], round_trip=True)
    entry = McpWriter(ctx).entry_for(bridge, "sd-example-test")
    assert "SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN" in json.dumps(entry) and value not in json.dumps(entry)


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.discovery.generated_bridge_settles_first") is True
    assert settings.bool_("mcp.verification.login_check.unconfirmed_rejection_settles") is False
    assert settings.bool_("mcp.verification.login_check.reuse_variable_names") is True
    assert settings.bool_("mcp.registration.replace_unproved_existing") is True
    assert settings.bool_("mcp.registration.name_undeclared_credentials") is True
