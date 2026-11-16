"""Two wrong results from one run on a machine with no credentials.

* confluence.example.com/mcp redirected to the login page -- as every path that
  does not exist does -- and "the discovery agent says it needs a credential"
  made it an MCP endpoint. It closed the package, bridge and strategy stages,
  beat mcp-atlassian on "fewer values to supply" (its header token was not
  counted), and stayed written after Claude Code said "Unexpected content type:
  text/html".
* gitlab.example.net's bridge was refused because Claude Code "connects to the
  existing gitlab-example-net" -- an old claude CLI copy this run had discarded for
  naming a file not on the machine -- and .mcp.json ended with no gitlab entry.
"""

from __future__ import annotations

import json

from initialize_claude_ai.mcp_auto_discovery import claude_check, evidence
from initialize_claude_ai.mcp_auto_discovery.claude_check import Outcome
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

URL = "https://wiki.example.test/mcp"


def _inconclusive(claim: str = "the discovery agent says it needs a credential") -> Candidate:
    candidate = Candidate(kind="url", name="wiki-example-test", url=URL, auth_claim=claim)
    candidate.handshake = Handshake(
        ok=False,
        status=200,
        inconclusive=True,
        content_type="text/html",
        error="response was not a JSON-RPC message (<html>...)",
    )
    return candidate


# -- an endpoint is proved, never claimed -------------------------------------------------------


def test_a_reply_like_a_missing_path_is_not_an_endpoint_whatever_anybody_says(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier.prober, "control_answer", lambda url: (200, "text/html"))
    candidate = _inconclusive()
    assert not verifier.credit_auth_claim(candidate)
    assert not candidate.needs_credential and not candidate.handshake.requires_auth
    assert any("a path that does not exist" in n for n in candidate.notes)


def test_a_reply_unlike_a_missing_path_is_credited_with_its_proof(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier.prober, "control_answer", lambda url: (404, "application/json"))
    candidate = _inconclusive()
    assert verifier.credit_auth_claim(candidate) and candidate.needs_credential
    assert "unlike a path that does not exist" in candidate.handshake.endpoint_proof


def test_the_old_credulous_reading_is_one_switch_away(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    ctx.settings.set("mcp.discovery.auth.claim_needs_control", False)
    monkeypatch.setattr(verifier.prober, "control_answer", lambda url: (200, "text/html"))
    assert verifier.credit_auth_claim(_inconclusive())


def test_an_agent_s_report_is_a_claim_that_closes_nothing() -> None:
    record = DueDiligence.from_payload("https://wiki.example.test", {"auth_required_endpoints": [URL]})
    assert record is not None and record.auth_walls == [] and record.claimed["auth_walls"] == [URL]
    assert not record.native_exists and record.to_dict()["claimed_by_agent_unproven"] == {"auth_walls": [URL]}


# -- a header credential is a value to supply -----------------------------------------------------


def test_a_header_credential_counts_as_a_value_to_supply(settings) -> None:
    url = Candidate(kind="url", name="wiki", url=URL, headers={"Authorization": "Bearer ${WIKI_EXAMPLE_TEST_TOKEN}"})
    url.handshake = Handshake(ok=False, requires_auth=True, auth_evidence=["the challenge names resource_metadata"])
    assert url.needs_credential and evidence.unsupplied(url, settings) == 1
    settings.set("mcp.discovery.auth.evidence.count_header_credentials", False)
    assert evidence.unsupplied(url, settings) == 0


# -- Claude Code says it is not MCP: withdrawn, handed over ------------------------------------------


def test_claude_code_saying_not_mcp_withdraws_the_entry_and_restores_the_one_it_replaced(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.dry_run = False
    writer = McpWriter(ctx)
    previous = {"command": "uvx", "args": ["mcp-atlassian"], "env": {"CONFLUENCE_URL": "https://wiki.example.test"}}
    entry = {"type": "http", "url": URL, "headers": {"Authorization": "Bearer ${WIKI_EXAMPLE_TEST_TOKEN}"}}
    writer.json_path.write_text(json.dumps({"mcpServers": {"wiki-example-test": entry}}))
    candidate = _inconclusive()
    candidate.handshake.requires_auth = True
    registration = Registration(
        name="wiki-example-test",
        candidate=candidate,
        entry=entry,
        written=True,
        had_previous=True,
        previous_entry=previous,
        target="https://wiki.example.test",
    )
    outcome = TargetOutcome(target="https://wiki.example.test")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    outcome.due_diligence.note("auth_walls", [URL])
    monkeypatch.setattr(claude_check, "enabled", lambda s: True)
    monkeypatch.setattr(
        claude_check,
        "connect_check",
        lambda *a, **k: Outcome(
            claude_check.FAILED,
            "could not connect",
            reason="CLIENT_HTTP_UNEXPECTED_CONTENT: Unexpected content type: text/html",
            reason_class="not_mcp",
        ),
    )
    writer._check_through_claude(registration, outcome)
    assert json.loads(writer.json_path.read_text())["mcpServers"]["wiki-example-test"] == previous
    assert registration.refused and not registration.written
    assert candidate.identity() in outcome.consumer_failed, "the address is handed over"
    assert URL not in outcome.due_diligence.auth_walls and URL in outcome.due_diligence.ruled_out


# -- status holds no place; kept means written --------------------------------------------------------


def test_an_existing_entry_no_copy_of_which_was_judged_holds_no_place(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    outcome = TargetOutcome(target="https://gitlab.example.test")
    outcome.connected_existing = {"gitlab-example-test": "claude_cli"}
    outcome.answered_existing = {"gitlab-example-test": "claude_cli"}
    assert not McpWriter(ctx)._keepable(outcome, "gitlab-example-test"), "kept means written: nothing to write"
    ctx.settings.set("mcp.registration.unjudged_is_keepable", True)
    assert McpWriter(ctx)._keepable(outcome, "gitlab-example-test")


def test_the_new_bridge_is_not_refused_for_a_connected_copy(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    outcome = TargetOutcome(target="https://gitlab.example.test")
    outcome.connected_existing = {"gitlab-example-test": "claude_cli"}
    outcome.answered_existing = {"gitlab-example-test": "claude_cli"}
    bridge = Candidate(
        kind="stdio",
        name="gitlab-example-test",
        command=["python3", "bridge.py"],
        secret_env={"GITLAB_EXAMPLE_TEST_TOKEN": "env:GITLAB_EXAMPLE_TEST_TOKEN"},
        api_kind="graphql",
    )
    bridge.handshake = Handshake(ok=True, tools=["get_current_user"], credential_pending=["GITLAB_EXAMPLE_TEST_TOKEN"])
    registration = Registration(name="gitlab-example-test", candidate=bridge, entry={"command": "python3"})
    assert McpWriter(ctx)._holds_its_place(registration, outcome, {}) == ""


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.discovery.auth.claim_needs_control") is True
    assert settings.bool_("mcp.policy.agent_claims_close_tiers") is False
    assert settings.bool_("mcp.discovery.auth.evidence.count_header_credentials") is True
    assert settings.bool_("mcp.registration.claude_check.withdraw_not_mcp") is True
    assert settings.bool_("mcp.registration.connected_holds_place") is False
    assert settings.bool_("mcp.registration.unjudged_is_keepable") is False
    assert settings.dict_("mcp.registration.claude_check.failure_classes", {}) == claude_check.FAILURE_CLASSES


def test_a_copy_that_failed_never_speaks_for_its_name_nor_holds_a_place(ctx, monkeypatch) -> None:
    """confluence-example-com: the file's URL copy failed (a login page), the CLI's mcp-atlassian waited for its token."""
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(ctx)
    failed = Candidate(kind="url", name="wiki-example-test", url=URL)
    failed.handshake = Handshake(ok=False, status=200, inconclusive=True, error="response was not a JSON-RPC message")
    waiting = Candidate(
        kind="stdio",
        name="wiki-example-test",
        command=["uvx", "mcp-atlassian"],
        secret_env={"CONFLUENCE_PERSONAL_TOKEN": "env:CONFLUENCE_PERSONAL_TOKEN"},
    )
    waiting.handshake = Handshake(
        ok=True, tools=["confluence_search"], credential_pending=["CONFLUENCE_PERSONAL_TOKEN"]
    )
    outcome = TargetOutcome(target="https://wiki.example.test")
    monkeypatch.setattr(writer, "_existing_candidates", lambda o, n: [failed, waiting])
    assert writer._existing_candidate(outcome, "wiki-example-test") is waiting
    monkeypatch.setattr(writer, "_existing_candidates", lambda o, n: [failed])
    assert not writer._keepable(outcome, "wiki-example-test"), "a copy shown unable to work holds nothing"


def test_a_launcher_still_downloading_is_given_room_and_judged_by_its_answer(ctx, monkeypatch) -> None:
    """mcp-atlassian 'did not start' as written while uvx was downloading pydantic-core; it was discarded."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    prober = McpProber(ctx)
    ctx.settings.set("mcp.verification.launcher_warm_timeout_seconds", 123.0)
    seen = []
    answers = [
        Handshake(ok=False, error="no response to initialize: Downloading pydantic-core (2.1MiB)"),
        Handshake(ok=True, tools=["confluence_search"]),
    ]

    def once(command, **kw):
        seen.append(kw.get("timeout"))
        return answers.pop(0)

    monkeypatch.setattr(prober, "_probe_stdio_once", once)
    assert prober.probe_stdio(["uvx", "mcp-atlassian"], timeout=25).ok and seen == [25, 123.0]
    broken = [Handshake(ok=False, error="process exited with rc=1: ModuleNotFoundError: no module named x")]
    monkeypatch.setattr(prober, "_probe_stdio_once", lambda command, **kw: broken.pop(0))
    assert not prober.probe_stdio(["uvx", "x"]).ok, "a real failure is not relaunched"
