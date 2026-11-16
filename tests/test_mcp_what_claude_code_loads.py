"""Thirteen servers from one run on another machine (2026-10-20), judged the way Claude Code loads them.

* openqa-example-net, developers-google-com: written ``"type": "stdio"`` beside a
  ``url`` -- an agent's transport word fell through a table lookup -- and Claude
  Code skipped both ("command: expected string"); the run said "registered".
  openqa-example-net's CLI copy was removed first and the add then refused.
* eleven more were listed in ``disabledMcpjsonServers``: ``/mcp`` showed none.
* gitlab-example-net: a waiting mcp-graphql settled the descent, the connected bridge
  a tier below was never looked at, and mcp-graphql took up to 73 s to start
  (Claude Code waits 30) and answered "Query too large".
* sd-example-com: two service-desk bridges asked with *no* token were ranked
  "REJECTED" and lost to an unchecked mcp-atlassian.
* bugzilla-example-com: the key sat under a variable mcp-bugzilla never reads.

Learned: an entry is judged as Claude Code reads and loads it; a verdict about a
credential needs one to have been sent; what worked here is not hidden by what
proved nothing; one start is not a start-up time; a slot must reach the server.
"""

from __future__ import annotations

import json
import os
import stat

from fake_secrets import fake

from initialize_claude_ai.mcp_auto_discovery import claude_check, login_check
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
    normalize_transport,
    proof_rank,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

# -- the entry's type comes from proof and kind, never from a word ------------------------------


def test_a_url_is_never_registered_as_stdio() -> None:
    assert normalize_transport("stdio", "url") == Transport.STREAMABLE_HTTP
    assert normalize_transport("Streamable HTTP", "url") == Transport.STREAMABLE_HTTP
    assert normalize_transport("server-sent events", "url") == Transport.SSE
    assert normalize_transport("whatever", "url") == Transport.STREAMABLE_HTTP
    assert normalize_transport("http", "stdio") == Transport.STDIO


def test_the_written_type_is_the_proved_transport_and_a_url_entry_carries_no_env(ctx) -> None:
    candidate = Candidate(
        kind="url",
        name="openqa",
        url="https://openqa.example.test/mcp",
        transport="stdio",
        env={"NODE_EXTRA_CA_CERTS": "/etc/ssl/ca.pem"},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STREAMABLE_HTTP, tools=["a"])
    entry = McpWriter(ctx).entry_for(candidate, "openqa-example-test")
    assert entry["type"] == "http" and entry["url"] and "env" not in entry and "command" not in entry
    assert any("launches nothing" in note for note in candidate.notes)


def test_an_agent_transport_word_is_normalised_and_a_description_is_no_name(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    pipeline = AgenticPipeline(ctx)
    long_name = "url https://developerknowledge.example.test/mcp transport streamable-http how found published " * 2
    candidate, _note = pipeline._candidate_from(
        {"kind": "url", "url": "https://developerknowledge.example.test/mcp", "transport": "stdio", "name": long_name},
        origin="endpoint",
    )
    assert candidate.transport == Transport.STREAMABLE_HTTP
    assert len(candidate.name) <= 64


# -- an entry Claude Code would skip is never written ---------------------------------------------


def test_claude_codes_shape_rules_refuse_what_it_would_skip(ctx) -> None:
    writer = McpWriter(ctx)
    assert "needs command" in writer.claude_rejects("x", {"type": "stdio", "url": "https://x.example.test/mcp"})
    assert writer.claude_rejects("x", {"type": "http", "url": "https://x.example.test/mcp", "headers": {}}) == ""
    assert writer.claude_rejects("x", {"type": "stdio", "command": "uvx", "args": ["a"]}) == ""
    assert "not one Claude Code registers" in writer.claude_rejects("x", {"type": "websocket", "url": "wss://x"})


def _fake_claude(tmp_path, status: str, add_rc: int = 0) -> str:
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = "add-json" ]; then echo added; exit {add_rc}; fi\n'
        f'if [ "$2" = "get" ]; then echo "$3:"; echo "  Status: {status}"; exit 0; fi\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_claude_code_itself_is_asked_with_a_throwaway_configuration(ctx, tmp_path, monkeypatch) -> None:
    ctx.dry_run = False
    ctx.settings.set("mcp.registration.validate_with_claude", True)
    ctx.settings.set("paths.claude", _fake_claude(tmp_path, "connected", add_rc=1))
    monkeypatch.setattr(type(ctx), "tool", lambda self, name, config_key="": str(tmp_path / "claude"))
    refused = McpWriter(ctx).claude_rejects("x", {"type": "stdio", "command": "uvx"})
    assert refused.startswith("Claude Code refuses it")


def test_the_connection_is_checked_through_claude_code_without_a_credential(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(type(ctx), "tool", lambda self, name, config_key="": _fake_claude(tmp_path, "✔ Connected"))
    assert claude_check.connect_check(ctx, "x", {"type": "stdio", "command": "uvx"}).outcome == claude_check.PASSED


def test_a_server_claude_code_cannot_reach_in_time_is_a_failure(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        type(ctx), "tool", lambda self, name, config_key="": _fake_claude(tmp_path, "✘ Failed to connect")
    )
    outcome = claude_check.connect_check(ctx, "x", {"type": "stdio", "command": "uvx"})
    assert outcome.outcome == claude_check.FAILED and "could not connect" in outcome.detail


def test_asking_for_its_credential_is_waiting_not_broken(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        type(ctx), "tool", lambda self, name, config_key="": _fake_claude(tmp_path, "△ Needs authentication")
    )
    outcome = claude_check.connect_check(ctx, "x", {"type": "http", "url": "https://x.example.test"}, waiting=True)
    assert outcome.outcome == claude_check.MISSING_VARIABLE


# -- which written servers Claude Code will load --------------------------------------------------


def _written(sandbox, names) -> None:
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {n: {"type": "stdio", "command": "uvx", "args": [n]} for n in names}})
    )


def test_a_server_switched_off_in_a_settings_file_is_named_with_the_file(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    _written(sandbox, ["a-example-test", "b-example-test"])
    (sandbox / ".claude").mkdir()
    (sandbox / ".claude" / "settings.local.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True, "disabledMcpjsonServers": ["a-example-test"]})
    )
    rows = McpWriter(ctx).claude_load_check(["a-example-test", "b-example-test"])
    assert [r["name"] for r in rows] == ["a-example-test"]
    assert "disabledMcpjsonServers" in rows[0]["why"] and "settings.local.json" in rows[0]["why"]


def test_an_unapproved_project_server_is_said_to_wait_for_approval(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    _written(sandbox, ["a-example-test"])
    rows = McpWriter(ctx).claude_load_check(["a-example-test"])
    assert rows and "approval" in rows[0]["why"]


def test_an_entry_claude_code_would_skip_is_named(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"o-example-test": {"type": "stdio", "url": "https://o.example.test/mcp"}}})
    )
    rows = McpWriter(ctx).claude_load_check(["o-example-test"])
    assert rows and rows[0]["why"].startswith("Claude Code skips it")


# -- a verdict about a credential needs one to have been sent -------------------------------------


def test_what_a_call_carried_is_a_fact() -> None:
    assert login_check.credential_in({"SD_EXAMPLE_TEST_TOKEN": "abc"})
    assert not login_check.credential_in({"SD_EXAMPLE_TEST_TOKEN": ""})
    assert not login_check.credential_in({"SD_EXAMPLE_TEST_TOKEN": "${SD_EXAMPLE_TEST_TOKEN}"})
    assert login_check.credential_in({"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"})
    assert not login_check.credential_in({"MCP_BRIDGE_BASE_URL": "https://sd.example.test"})


def test_a_refusal_with_nothing_sent_ranks_as_missing_not_rejected(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    shake = Handshake(ok=True, tools=["a"], login=login_check.REJECTED, login_detail="HTTP 401", credential_sent=False)
    CandidateVerifier(ctx).judge_login(shake)
    assert not shake.login_refused and shake.login_reason == login_check.NOTHING_SENT
    candidate = Candidate(kind="stdio", name="bridge", command=["x"])
    candidate.handshake = shake
    assert proof_rank(candidate) == 1
    assert "no credential was sent" in login_check.in_words(login_check.REJECTED, sent=False)
    assert "public" in login_check.in_words(login_check.ACCEPTED, sent=False)


def test_a_refusal_of_a_sent_credential_is_still_a_refusal(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    shake = Handshake(ok=True, tools=["a"], login=login_check.REJECTED, login_detail="HTTP 401", credential_sent=True)
    CandidateVerifier(ctx).judge_login(shake)
    assert shake.login_refused


# -- a tool whose query the service refuses cannot do the job ------------------------------------


def test_a_refused_query_is_a_failure_not_a_wait(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    assert login_check.job_refused(
        [{"tool": "currentUser", "verdict": "unchecked", "detail": "{'message': 'Query too large'}"}], ctx.settings
    )
    candidate = Candidate(kind="stdio", name="mcp-graphql", command=["uvx", "mcp-graphql"])
    candidate.handshake = Handshake(
        ok=True,
        tools=["currentUser"],
        credential_pending=["MCP_AUTH_TOKEN"],
        job_refused="currentUser: Query too large",
    )
    CandidateVerifier(ctx)._apply_job_refused(candidate)
    assert not candidate.handshake.ok and not candidate.handshake.credential_pending
    assert "cannot do the job" in candidate.handshake.error


# -- what worked here is not hidden by what proved nothing ----------------------------------------


def _waiting(name: str, worked: str = "") -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["x", name])
    candidate.handshake = Handshake(ok=True, tools=["a"], credential_pending=["T"], requires_auth=False)
    candidate.worked_before = worked
    return candidate


def test_the_entry_that_worked_here_keeps_its_place_over_one_that_proved_nothing() -> None:
    outcome = TargetOutcome(target="https://gitlab.example.test")
    graphql, bridge = _waiting("mcp-graphql"), _waiting("bridge", worked="Claude Code shows it connected")
    outcome.absorb(StageOutcome(stage=Stage.PACKAGE, candidates=[graphql], provisional=[graphql]))
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[bridge], provisional=[bridge]))
    assert outcome.provisional == [bridge]
    assert any("known to work here" in note for note in bridge.notes)


def test_a_descent_settled_by_a_waiting_server_says_so(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    outcome = TargetOutcome(target="https://gitlab.example.test")
    graphql = _waiting("mcp-graphql")
    outcome.provisional = [graphql]
    skipped = TraditionalPipeline(ctx).settled(Stage.BRIDGE, outcome, exhaustive=False)
    assert skipped is not None and skipped.findings.get("settled_unproven") is True


# -- a wait that fits goes where Claude Code reads it ---------------------------------------------


def test_the_start_up_wait_is_set_in_claude_codes_settings(ctx) -> None:
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    ctx.dry_run = False
    where = McpWriter(ctx).persist_client_env("MCP_TIMEOUT", "110000", "a slow server")
    assert str(settings_path(ctx)) in where
    assert SettingsWriter(ctx, settings_path(ctx)).read()["env"]["MCP_TIMEOUT"] == "110000"
    assert McpWriter(ctx).client_timeout_seconds() == 110.0 or os.environ.get("MCP_TIMEOUT")


# -- a credential slot must reach the server ------------------------------------------------------


def test_a_slot_the_server_does_not_read_is_moved_to_the_one_it_does() -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    candidate = Candidate(kind="stdio", name="bugzilla", command=["uvx", "mcp-bugzilla"])
    candidate.env = {"BUGZILLA_EXAMPLE_TEST_API_KEY": ""}
    candidate.references = {"BUGZILLA_EXAMPLE_TEST_API_KEY": "${BUGZILLA_EXAMPLE_TEST_API_KEY:-}"}
    assert CandidateVerifier._slot_keys(candidate, ["BUGZILLA_EXAMPLE_TEST_API_KEY"]) == [
        "BUGZILLA_EXAMPLE_TEST_API_KEY"
    ]
    CandidateVerifier._move_slot(candidate, "BUGZILLA_EXAMPLE_TEST_API_KEY", "BUGZILLA_API_KEY")
    assert candidate.references == {"BUGZILLA_API_KEY": "${BUGZILLA_EXAMPLE_TEST_API_KEY:-}"}
    assert "BUGZILLA_EXAMPLE_TEST_API_KEY" not in candidate.env


# -- one user name and password on several services is allowed ------------------------------------


def test_a_shared_login_is_never_called_another_instances(ctx) -> None:
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    def tried(target: str, verdict: str) -> TargetOutcome:
        candidate = Candidate(kind="stdio", name="c", command=["x"])
        candidate.handshake = Handshake(
            ok=True,
            login=verdict,
            login_parts=["pw-print"],
            credential_sent=True,
            login_refused=verdict == login_check.REJECTED,
        )
        outcome = TargetOutcome(target=target)
        outcome.stages.append(StageOutcome(stage=Stage.BRIDGE, candidates=[candidate]))
        return outcome

    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_login_of_another_service(
        result,
        [
            tried("https://build.corp.test", login_check.REJECTED),
            tried("https://build.example.org", login_check.ACCEPTED),
        ],
    )
    [step] = result.steps
    assert "is fine" in step.remediation[0], "sharing one login between services is allowed"


def test_an_entry_left_with_the_wrong_type_is_reshaped_after_writing(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.dry_run = False
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "d-example-test": {
                        "type": "stdio",
                        "url": "https://d.example.test/mcp",
                        "headers": {"Authorization": "Bearer ${D_TOKEN}"},
                        "env": {"NODE_EXTRA_CA_CERTS": "/etc/ssl/ca.pem"},
                    }
                }
            }
        )
    )
    rows = McpWriter(ctx).verify_written(["d-example-test"])
    entry = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["d-example-test"]
    assert entry["type"] == "http" and "env" not in entry and entry["headers"]
    assert rows and "reshaped" in rows[0]["change"]


def test_a_credential_the_entry_carried_is_kept_where_claude_code_reads_it(ctx, sandbox) -> None:
    from initialize_claude_ai.core.secrets import Secret
    from initialize_claude_ai.mcp_auto_discovery.writer import Registration
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="sd", command=["uvx", "x"])
    candidate.in_use = {"JIRA_PERSONAL_TOKEN": Secret("a-real-looking-token-value-1234567890", source="the entry")}
    candidate.exported_names = {"JIRA_PERSONAL_TOKEN": "SD_EXAMPLE_TEST_TOKEN"}
    registration = Registration(
        name="sd-example-test",
        candidate=candidate,
        entry={"command": "uvx", "env": {"JIRA_PERSONAL_TOKEN": "${SD_EXAMPLE_TEST_TOKEN}"}},
        needs_env=["SD_EXAMPLE_TEST_TOKEN"],
    )
    McpWriter(ctx)._keep_held_credentials(registration)
    path = settings_path(ctx)
    assert SettingsWriter(ctx, path).read()["env"]["SD_EXAMPLE_TEST_TOKEN"] == "a-real-looking-token-value-1234567890"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert registration.needs_env == []


def test_the_login_plan_asks_for_the_kind_of_credential_already_in_use(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import inventory, login_methods

    in_use = inventory.methods_of_slots(["JIRA_PERSONAL_TOKEN"], settings)
    assert "token" in in_use
    offered = [{"scheme": "basic", "host": "sd.example.test", "method": "basic", "kind": "header"}]
    plan = login_methods.plan(
        "sd.example.test",
        "jira",
        offered,
        settings,
        in_use=in_use,
        same_login={"basic": {"USERNAME": "X", "PASSWORD": "Y"}},
    )
    assert plan["chosen"].get("method") == "token"


def test_an_adapter_is_given_the_credential_the_address_holds(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import api_kinds

    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "GITLAB_EXAMPLE_TEST_TOKEN")
    monkeypatch.setattr(api_kinds, "pinned", lambda ctx, registry, package: package)
    found = {"graphql": {"url": "https://gitlab.example.test/api/graphql"}}
    [candidate] = [
        c
        for c in api_kinds.adapter_candidates(ctx, "https://gitlab.example.test", found)
        if "mcp-graphql" in " ".join(c.full_command())
    ]
    assert candidate.env["HEADERS"] == '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN}"}'


def test_an_adapter_without_a_held_credential_stays_anonymous(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import api_kinds

    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "")
    monkeypatch.setattr(api_kinds, "pinned", lambda ctx, registry, package: package)
    found = {"graphql": {"url": "https://gitlab.example.test/api/graphql"}}
    [candidate] = [
        c
        for c in api_kinds.adapter_candidates(ctx, "https://gitlab.example.test", found)
        if "mcp-graphql" in " ".join(c.full_command())
    ]
    assert "HEADERS" not in candidate.env


def test_a_trial_resolves_from_claude_codes_settings_too(ctx, monkeypatch) -> None:
    monkeypatch.delenv("ICA_EXAMPLE_TEST_TOKEN", raising=False)
    ctx.secrets.fallback_env = lambda: {"ICA_EXAMPLE_TEST_TOKEN": "from-settings"}
    secret = ctx.secrets.resolve("env:ICA_EXAMPLE_TEST_TOKEN", required=False, label="t")
    assert secret is not None and secret.reveal() == "from-settings"


def test_an_entry_whose_credential_is_kept_is_no_longer_waiting(ctx, sandbox) -> None:
    from initialize_claude_ai.core.secrets import Secret
    from initialize_claude_ai.mcp_auto_discovery.writer import Registration

    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="g", command=["npx", "-y", "mcp-graphql"])
    candidate.in_use = {
        "GITLAB_EXAMPLE_TEST_TOKEN": Secret(fake("glpat-", "abcdefgh", "ijklmnop", "qrst"), source="the entry")
    }
    registration = Registration(
        name="gitlab-example-test",
        candidate=candidate,
        provisional=True,
        entry={"command": "npx", "env": {"HEADERS": '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN}"}'}},
        needs_env=["GITLAB_EXAMPLE_TEST_TOKEN"],
    )
    McpWriter(ctx)._keep_held_credentials(registration)
    assert registration.needs_env == [] and not registration.provisional
