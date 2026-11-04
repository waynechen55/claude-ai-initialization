"""Two wrong entries an earlier version wrote kept their places on a machine with no credentials.

* build-opensuse-org's mcp-openapi-proxy was "! Connected · tools fetch failed --
  Request timed out" in Claude Code, read as connected, and kept its place over
  the client bridge that logs in the way build.opensuse.org asks ("Claude Code
  connects to the existing ..., and X proved less") -- although it had proved
  nothing either, and sent a kind of login the address offers nowhere.
* sd.example.com's earliest stage kept @atlassian-dc-mcp/jira -- the core Jira API
  a customer account is turned away from -- over the bridge for the customer
  API the run had detected: nothing proved either, so tier order decided.
* The report asked for BUILD_OPENSUSE_ORG_EXTRA_HEADERS, a value no login
  could fill.

A place is now kept by proof, never by status; every chooser shares one
comparison by what is known; and the report never asks for a value that
cannot work. Tested from the other machine's actual previous output.
"""

from __future__ import annotations

from initialize_claude_ai.mcp_auto_discovery import claude_check, evidence, login_check
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    evidence_beats,
    evidence_key,
)

BUILD_PLAN = {
    "offered": [{"scheme": "basic", "host": "build.example.test"}, {"scheme": "Basic", "host": "api.example.test"}],
    "chosen": {"method": "basic"},
}


def _waiting(name: str, env: dict, *, api_kind: str = "", secret: str = "") -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name=name,
        command=["python3", f"{name}.py"],
        env=dict(env),
        secret_env={secret: f"env:{secret}"} if secret else {},
        api_kind=api_kind,
    )
    candidate.handshake = Handshake(
        ok=True, tools=["t"], round_trip=True, credential_pending=[secret] if secret else []
    )
    return candidate


# -- Claude Code's status, read one way ---------------------------------------------------------------


def test_a_warning_status_is_not_connected(settings) -> None:
    assert (
        claude_check.status_verdict("uvx proxy - ! Connected · tools fetch failed — Request timed out", settings)
        == "failed"
    )
    assert claude_check.status_verdict("npx x - ✔ Connected", settings) == "connected"
    assert claude_check.status_verdict("https://x (HTTP) - ✘ Failed to connect", settings) == "failed"
    assert claude_check.status_verdict("nothing said", settings) == ""


# -- what is known decides -------------------------------------------------------------------------


def test_a_header_value_tells_its_login_and_an_unoffered_one_goes_last(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.api_host_prefixes", ["api."])
    proxy = _waiting(
        "proxy", {"EXTRA_HEADERS": "Authorization:Bearer ${BUILD_EXAMPLE_EXTRA_HEADERS}"}, secret="EXTRA_HEADERS"
    )
    bridge = _waiting("client-bridge", {}, secret="BUILD_EXAMPLE_PASSWORD")
    assert evidence.scheme_of(proxy, ctx.settings) == "bearer"
    proxy.evidence = evidence.key(ctx, proxy, "build.example.test", BUILD_PLAN)
    bridge.evidence = evidence.key(ctx, bridge, "build.example.test", BUILD_PLAN)
    assert proxy.evidence[1] == 3 and evidence_beats(bridge, proxy) and not evidence_beats(proxy, bridge)


def test_a_chosen_token_method_counts_bearer_as_offered(ctx) -> None:
    plan = {
        "offered": [{"scheme": "OAuth", "host": "sd.example.test"}, {"scheme": "basic", "host": "sd.example.test"}],
        "chosen": {"method": "token"},
    }
    token = _waiting("jira", {}, secret="JIRA_PERSONAL_TOKEN")
    assert evidence.key(ctx, token, "sd.example.test", plan)[1] != 2, "Jira takes tokens it does not advertise"


def test_the_bridge_for_the_detected_api_takes_a_slot_from_an_earlier_stage() -> None:
    core = _waiting("sd-example-atlassian-dc", {}, secret="JIRA_API_TOKEN")
    core.handshake.login = login_check.UNCHECKED
    outcome = TargetOutcome(target="https://sd.example.test", provisional=[core])
    bridge = _waiting("sd-example-bridge", {}, secret="SD_EXAMPLE_TOKEN", api_kind="jira_service_management")
    bridge.handshake.login = login_check.UNCHECKED
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[bridge]), 1)
    assert outcome.provisional == [bridge]


def test_proof_still_outranks_evidence() -> None:
    proven = _waiting("proven", {}, secret="X_TOKEN")
    proven.handshake.login = login_check.ACCEPTED
    other = _waiting("other", {}, secret="Y_TOKEN", api_kind="k")
    assert not evidence_beats(other, proven)
    assert evidence_key(proven) < evidence_key(other)


# -- a place is kept by proof -----------------------------------------------------------------------


def test_an_incumbent_that_proved_nothing_keeps_no_place_over_a_better_ranked_candidate(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    proxy = _waiting("build-example-test", {"EXTRA_HEADERS": "Authorization:Bearer ${X}"}, secret="EXTRA_HEADERS")
    proxy.evidence = (1, 2, 1)
    bridge = _waiting("build-example-test-client", {}, secret="BUILD_EXAMPLE_PASSWORD")
    bridge.evidence = (1, 0, 1)
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.connected_existing["build-example-test"] = "claude_cli"
    outcome.answered_existing["build-example-test"] = "claude_cli"
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidate", lambda o, n: proxy)
    registration = Registration(name="build-example-test", candidate=bridge, entry={"command": "python3"})
    assert writer._holds_its_place(registration, outcome, {}) == ""
    proxy.handshake.contract = [{"task": "read the account's own record", "passed": True}]
    assert writer._holds_its_place(registration, outcome, {}) != "", "one that did its job keeps its place"


# -- never ask for a value that cannot work ------------------------------------------------------------


def test_an_unoffered_login_is_said_instead_of_asking_for_a_value(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.discovery.auth.api_host_prefixes", ["api."])
    proxy = _waiting("build-example-test", {"EXTRA_HEADERS": "Authorization:Bearer ${X}"}, secret="EXTRA_HEADERS")
    outcome = TargetOutcome(target="https://build.example.test", auth_plan=BUILD_PLAN)
    said = McpWriter(ctx)._unoffered_login(proxy, outcome)
    assert "offers nowhere" in said and "bearer" in said
    bridge = _waiting("client", {}, secret="BUILD_EXAMPLE_PASSWORD")
    assert McpWriter(ctx)._unoffered_login(bridge, outcome) == ""


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import types

    marks = settings.section("mcp.registration")
    assert list(marks.str_list("failed_markers", [])) == list(claude_check.FAILED_MARKERS)
    assert list(marks.str_list("degraded_patterns", [])) == list(claude_check.DEGRADED_PATTERNS)
    assert marks.bool_("keeping_needs_proof") is True and marks.bool_("say_unoffered_login") is True
    ev = settings.section("mcp.discovery.auth.evidence")
    assert ev.dict_("value_schemes", {}) == evidence.VALUE_SCHEMES
    assert ev.dict_("method_schemes", {}) == evidence.METHOD_SCHEMES
    assert ev.bool_("one_comparison") is types.ONE_COMPARISON
    assert ev.bool_("unknown_below_offered") is True


def test_an_unknown_login_ranks_below_one_the_address_offers(ctx) -> None:
    """build-opensuse-org: the proxy (one value, unknown kind) beat the Basic client bridge (two values)."""
    plan = {"offered": [{"scheme": "basic", "host": "build.example.test"}], "chosen": {"method": "client"}}
    proxy = _waiting("proxy", {"EXTRA_HEADERS": "${BUILD_EXAMPLE_EXTRA_HEADERS}"}, secret="EXTRA_HEADERS")
    bridge = _waiting("client-bridge", {}, secret="BUILD_EXAMPLE_PASSWORD")
    bridge.handshake.credential_pending = ["BUILD_EXAMPLE_USERNAME", "BUILD_EXAMPLE_PASSWORD"]
    proxy.evidence = evidence.key(ctx, proxy, "build.example.test", plan)
    bridge.evidence = evidence.key(ctx, bridge, "build.example.test", plan)
    assert proxy.evidence[1] == 2 and bridge.evidence[1] == 1
    assert evidence_beats(bridge, proxy), "the offered login first; values to supply only break ties"


def test_a_challenge_seen_before_counts_when_the_host_gives_none_this_time(ctx) -> None:
    """api.opensuse.org's Basic went missing in one run (its public page answered 503)."""
    from initialize_claude_ai.mcp_auto_discovery import login_methods

    ctx.dry_run = False
    first = [
        {"host": "build.example.test", "scheme": "basic", "method": "basic", "evidence": "Basic"},
        {"host": "api.example.test", "scheme": "Basic", "method": "basic", "evidence": "Basic realm=x"},
    ]
    assert login_methods.with_remembered(ctx, "build.example.test", list(first)) == first
    later = login_methods.with_remembered(ctx, "build.example.test", [first[0]])
    assert any(o["host"] == "api.example.test" and o.get("remembered") for o in later)
    assert len([o for o in later if o["host"] == "build.example.test"]) == 1, "this run's own answer wins"
    ctx.settings.set("mcp.discovery.auth.remember_challenges", False)
    assert login_methods.with_remembered(ctx, "build.example.test", [first[0]]) == [first[0]]
