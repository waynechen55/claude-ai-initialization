"""Four addresses from one run on another machine (2026-10-18), each wrong a different way.

* build-example-net: the existing CLI copy was proven broken -- "its manifest reads
  BUILD_EXAMPLE_NET_PASSWORD, which the entry does not pass" -- and was written
  anyway, as "waiting for a credential", over two bridges that could work.
* build-example-net, build-opensuse-org: the scripts read ``SSH_AUTH_SOCK`` and the
  entries expanded ``${BUILD_EXAMPLE_NET_SSH_AUTH_SOCK:-}`` -- a name nobody has.
* smelt-example-net: ``dashboard.qa.example.net/mcp`` (Example's QA dashboard) was taken
  as smelt's "native endpoint" on "same organisation" alone, closed the package
  stage, and the connected mcp-graphql entry was never compared with it.
* sd-example-com: its customer API, detected behind its login, was "confirmed" not
  to exist ("answered 401"), and with no credential of its own on the machine
  mcp-atlassian kept the slot over the bridge built for that API.
* build-example-net again: "refused every login" when no login was ever sent --
  every route said "not set" or "no SSH key".

Learned: proven broken waits for nothing; the session's variables keep the
session's names; an organisation runs many services; a policy that forbids new
work never hides what is registered; one test for an API, wherever it is asked.
"""

from __future__ import annotations

from types import SimpleNamespace

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery import api_kinds, login_check
from initialize_claude_ai.mcp_auto_discovery.grounding import (
    SERVED_ELSEWHERE,
    UNGROUNDED,
    IdentityRule,
    assess,
    organisation_tie,
)
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)

SMELT = "https://smelt.example.test"


def waiting(name: str, *, api_kind: str = "", start_failure: str = "", login: str = "") -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name=name,
        command=["python3", f"{name}.py"],
        origin=Stage.BRIDGE,
        source=f"{name} (test)",
        api_kind=api_kind,
        start_failure=start_failure,
    )
    candidate.secret_env = {"TOKEN": "env:TOKEN"}
    candidate.handshake = Handshake(
        ok=True, tools=["get_my_requests"], round_trip=True, credential_pending=["TOKEN"], login=login
    )
    return candidate


# -- proven broken waits for nothing ------------------------------------------------------------------------


def test_a_broken_existing_entry_is_never_the_one_waiting() -> None:
    broken = waiting("build-example-test", start_failure="its manifest reads PASSWORD, which the entry does not pass")
    broken.confidence = 0.9
    bridge = waiting("build-example-test-client-bridge-basic")
    stage = StageOutcome(stage=Stage.PACKAGE, candidates=[broken, bridge])
    assert stage.note_provisional(1) == [bridge]


def test_one_already_waiting_is_dropped_once_it_is_proven_broken() -> None:
    old = waiting("old")
    outcome = TargetOutcome(target="https://build.example.test", provisional=[old])
    old.start_failure = "does not start"
    bridge = waiting("bridge")
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[bridge]), 1)
    assert outcome.provisional == [bridge]


def test_the_writer_never_registers_a_broken_candidate_as_waiting(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    broken = waiting("build-example-test", start_failure="it exited")
    written = McpWriter(ctx).publish(TargetOutcome(target="https://build.example.test", provisional=[broken]))
    assert all(r.candidate is not broken or r.refused for r in written)


# -- the session's variables keep the session's names --------------------------------------------------------


def test_a_session_variable_is_written_under_its_own_name(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(
        kind="stdio",
        name="b",
        command=["python3", "bridge.py"],
        env={"SSH_AUTH_SOCK": "${BUILD_EXAMPLE_TEST_SSH_AUTH_SOCK:-}"},
    )
    candidate.secret_env = {"PASSWORD": "env:BUILD_EXAMPLE_TEST_PASSWORD", "SSH_AUTH_SOCK": "env:SSH_AUTH_SOCK"}
    writer = McpWriter(ctx)
    entry = writer.entry_for(candidate, "build-example-test")
    assert entry["env"]["SSH_AUTH_SOCK"] == "${SSH_AUTH_SOCK:-}"
    assert "SSH_AUTH_SOCK" not in writer.exported_variables(candidate), "never something to export"
    assert writer._exported_name("SSH_AUTH_SOCK", "build-example-test") == "SSH_AUTH_SOCK"


def test_a_read_session_variable_is_declared_under_its_own_name(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.env_contract import Read
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    read = Read(name="SSH_AUTH_SOCK", default=None, source="bridge.py", line=1)
    assert McpWriter(ctx).declared_value(read, "build-example-test", []) == "${SSH_AUTH_SOCK:-}"


def test_session_variables_are_configured(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.env_contract import EnvContract

    contract = EnvContract(ctx.settings)
    assert contract.session("SSH_AUTH_SOCK") and not contract.session("BUILD_EXAMPLE_TEST_PASSWORD")
    ctx.settings.set("mcp.env_contract.session_variables", [])
    assert not EnvContract(ctx.settings).session("SSH_AUTH_SOCK")


# -- an organisation runs many services ----------------------------------------------------------------------


def answered(**kw) -> Handshake:
    return Handshake(
        ok=True,
        round_trip=True,
        server_name=kw.pop("server_name", "PerlServer"),
        tools=kw.pop("tools", ["list_blocked", "list_submissions"]),
        **kw,
    )


def url(where: str, **kw) -> Candidate:
    return Candidate(kind="url", name="d", url=where, transport=Transport.STREAMABLE_HTTP, handshake=answered(**kw))


def test_another_service_of_the_organisation_is_not_the_address(ctx) -> None:
    verdict = assess(url("https://dashboard.qa.example.test/mcp"), SMELT, served_by=["dashboard.qa.example.test"])
    assert verdict.kind == UNGROUNDED and "another service" in verdict.reason


def test_an_organisation_front_or_a_product_named_sibling_is() -> None:
    assert assess(url("https://mcp.example.test/mcp"), SMELT).kind == SERVED_ELSEWHERE
    assert assess(url("https://smelt-api.example.test/mcp"), SMELT).kind == SERVED_ELSEWHERE
    told = url("https://dashboard.qa.example.test/mcp", instructions="Reads SMELT incidents")
    assert assess(told, SMELT).kind == SERVED_ELSEWHERE


def test_an_api_host_launcher_is_still_believed() -> None:
    osc = Candidate(
        kind="stdio",
        name="osc",
        command=["osc-mcp"],
        env={"OBS_API_URL": "https://api.example.test"},
        transport=Transport.STDIO,
        handshake=answered(),
    )
    assert assess(osc, "https://build.example.test").kind == SERVED_ELSEWHERE
    jira = Candidate(
        kind="stdio",
        name="a",
        command=["uvx", "mcp-atlassian"],
        env={"JIRA_URL": "https://jira.example.test"},
        transport=Transport.STDIO,
        handshake=answered(),
    )
    assert assess(jira, "https://desk.example.test").kind == UNGROUNDED


def test_the_organisation_alone_can_be_restored() -> None:
    candidate = url("https://dashboard.qa.example.test/mcp")
    assert organisation_tie(candidate, "smelt.example.test", "dashboard.qa.example.test") == ""
    assert organisation_tie(
        candidate, "smelt.example.test", "dashboard.qa.example.test", IdentityRule(organisation_needs_tie=False)
    )


# -- a closed tier still judges what is registered -----------------------------------------------------------


def test_a_policy_closed_stage_still_rechecks_the_existing_entry(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    existing = Candidate(
        kind="url",
        name="smelt-example-test",
        url=f"{SMELT}/graphql",
        origin=Stage.PACKAGE,
        transport=Transport.STREAMABLE_HTTP,
        handshake=answered(tools=["query-graphql"]),
    )
    assert existing.verified

    def reuse(name, outcome, exhaustive=False):
        pipeline._reused[(outcome.target, name)] = [existing]
        return None

    monkeypatch.setattr(pipeline, "reuse", reuse)
    monkeypatch.setattr(pipeline, "heal_or_discard", lambda *a, **k: None)
    outcome = TargetOutcome(target=SMELT)
    blocked = StageOutcome(stage=Stage.PACKAGE, status=Status.SKIPPED, detail="a native MCP endpoint exists")
    blocked.findings["blocked_by_policy"] = blocked.detail
    stage = pipeline.recheck_blocked(Stage.PACKAGE, outcome, blocked)
    assert existing in outcome.verified and "re-checked" in stage.detail
    assert stage.findings["blocked_by_policy"]


def test_a_settled_stage_is_not_rechecked(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "reuse", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not asked")))
    settled = StageOutcome(stage=Stage.PACKAGE, status=Status.SKIPPED, detail="already answered")
    assert pipeline.recheck_blocked(Stage.PACKAGE, TargetOutcome(target=SMELT), settled) is settled
    ctx.settings.set("mcp.discovery.reuse.recheck_when_blocked", False)
    settled.findings["blocked_by_policy"] = "x"
    assert pipeline.recheck_blocked(Stage.PACKAGE, TargetOutcome(target=SMELT), settled) is settled


# -- one test for an API, wherever it is asked ---------------------------------------------------------------


def jsm(settings):
    return next(k for k in api_kinds.kinds(settings) if k.name == "jira_service_management")


def test_an_api_behind_its_login_is_confirmed_as_it_was_detected(settings) -> None:
    kind = jsm(settings)

    def send(method, where, content_type, body):
        status = 404 if where.endswith(api_kinds.DEFAULT_CONTROL_NAME) else 401
        return SimpleNamespace(
            status=status,
            text='{"message":"Client must be authenticated"}',
            headers={"content-type": "application/json"},
            content_type="application/json",
        )

    where = "https://desk.example.test/rest/servicedeskapi/info"
    assert api_kinds.confirm(kind, where, send=send) == ""
    assert api_kinds.confirm(kind, where, send=send, behind_login=False).startswith("answered 401")


def test_a_site_refusing_every_path_alike_is_not_confirmed(settings) -> None:
    def send(method, where, content_type, body):
        return SimpleNamespace(
            status=401, text="{}", headers={"content-type": "application/json"}, content_type="application/json"
        )

    assert api_kinds.confirm(jsm(settings), "https://x.example.test/rest/servicedeskapi/info", send=send)


# -- between two unproven, the one built for the API this host serves ----------------------------------------


def test_the_detected_api_takes_the_slot_when_neither_proved() -> None:
    package = waiting("desk-example-test", login=login_check.UNCHECKED)
    outcome = TargetOutcome(target="https://desk.example.test", provisional=[package])
    bridge = waiting("desk-example-test-bridge", api_kind="jira_service_management", login=login_check.UNCHECKED)
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[bridge]), 1)
    assert outcome.provisional == [bridge]
    kept = TargetOutcome(target="https://desk.example.test", provisional=[package])
    # Equally proved otherwise: with the preference off, the API kind alone takes nothing.
    kept.absorb(
        StageOutcome(stage=Stage.BRIDGE, candidates=[waiting("b2", api_kind="x", login=login_check.UNCHECKED)]),
        1,
        prefer_detected_api=False,
    )
    assert kept.provisional == [package]


def test_proof_still_decides_first() -> None:
    proven = waiting("desk-example-test", login=login_check.ACCEPTED)
    outcome = TargetOutcome(target="https://desk.example.test", provisional=[proven])
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[waiting("b", api_kind="x")]), 1)
    assert outcome.provisional == [proven]


# -- missing is not refused ----------------------------------------------------------------------------------


def missing(name: str, detail: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["python3", f"{name}.py"])
    candidate.secret_env = {"PASSWORD": "env:BUILD_EXAMPLE_TEST_PASSWORD"}
    candidate.handshake = Handshake(
        ok=True,
        tools=["whoami"],
        round_trip=True,
        login=login_check.REJECTED,
        login_refused=True,
        login_credential="fp",
        login_detail=detail,
    )
    return candidate


def test_no_login_sent_is_said_as_missing_with_each_route(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    outcome = TargetOutcome(target="https://build.example.test")
    outcome.stages.append(
        StageOutcome(
            stage=Stage.BRIDGE,
            candidates=[
                missing("basic", "401 Unauthorized: BUILD_EXAMPLE_TEST_PASSWORD not set"),
                missing("signature", "401 Unauthorized: no SSH key to sign with"),
            ],
        )
    )
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_refused_every_way(result, outcome)
    [step] = [s for s in result.steps if s.name.endswith(":: your login")]
    assert "nothing on this machine can log in" in step.detail
    assert "BUILD_EXAMPLE_TEST_PASSWORD" in step.remediation[0] and "sign in on its web page" not in step.remediation[0]


def test_a_real_refusal_is_still_a_refusal(settings) -> None:
    assert login_check.credential_missing("401 Unauthorized: PASSWORD not set", settings)
    assert not login_check.credential_missing("401 Unauthorized: invalid token", settings)


# -- packaged settings equal the code ------------------------------------------------------------------------


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery.env_contract import SESSION_VARIABLES

    assert tuple(settings.str_list("mcp.env_contract.session_variables")) == SESSION_VARIABLES
    assert tuple(settings.str_list("mcp.verification.login_check.missing_credential_markers")) == (
        login_check.DEFAULT_MISSING_CREDENTIAL_MARKERS
    )
    assert settings.bool_("mcp.registration.identity.organisation_needs_tie") is True
    assert settings.bool_("mcp.discovery.reuse.recheck_when_blocked") is True
    assert settings.bool_("mcp.discovery.auth.prefer_detected_api") is True
    assert settings.bool_("mcp.discovery.reuse.keep_operator_references") is True
    from initialize_claude_ai.mcp_auto_discovery.same_server import SCRIPT_EXTENSIONS

    assert tuple(settings.str_list("mcp.registration.fold_script_extensions")) == SCRIPT_EXTENSIONS


# -- a reference stays a reference; one server is one entry, wherever its script lives --------------------------


def test_an_existing_reference_is_written_back_not_its_value(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    monkeypatch.setenv("BUILD_EXAMPLE_TEST_USERNAME", "someone")
    entry = {
        "command": "/usr/bin/python3",
        "args": ["/opt/bridges/mcp_client_bridge_build_example_test.py"],
        "env": {
            "BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME}",
            "MCP_CLIENT_SERVICE": "https://build.example.test",
        },
    }
    candidate = ExistingRegistrations.for_run(ctx).candidate(
        Registration(name="build-example-test", entry=entry, source=PROJECT_FILE), Stage.BRIDGE
    )
    assert candidate.env["BUILD_EXAMPLE_TEST_USERNAME"] == "someone", "expanded to be launched"
    written = McpWriter(ctx).entry_for(candidate, "build-example-test")
    assert "someone" not in str(written)
    assert written["env"]["BUILD_EXAMPLE_TEST_USERNAME"].startswith("${BUILD_EXAMPLE_TEST_USERNAME")


def test_a_literal_copy_of_what_you_export_becomes_the_reference(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    monkeypatch.setenv("BUILD_EXAMPLE_TEST_USERNAME", "someone")
    entry = {
        "command": "/usr/bin/python3",
        "args": ["/opt/bridges/b.py"],
        "env": {"BUILD_EXAMPLE_TEST_USERNAME": "someone", "MCP_CLIENT_SERVICE": "https://build.example.test"},
    }
    candidate = ExistingRegistrations.for_run(ctx).candidate(
        Registration(name="build-example-test", entry=entry, source=PROJECT_FILE), Stage.BRIDGE
    )
    written = McpWriter(ctx).entry_for(candidate, "build-example-test")
    assert "someone" not in str(written)


def test_one_generated_script_in_two_directories_is_one_server(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import same_server

    def bridge(path: str, name: str) -> Candidate:
        return Candidate(
            kind="stdio",
            name=name,
            command=["/usr/bin/python3.13", path],
            env={"MCP_CLIENT_SERVICE": "https://build.example.test"},
        )

    here = bridge("/home/me/project/bridges/mcp_client_bridge_build_example_test.py", "build-example-test")
    there = bridge("${ICA_MCP_BRIDGES:-/state/bridges}/mcp_client_bridge_build_example_test.py", "basic")
    assert same_server.key(here, settings) == same_server.key(there, settings)
    other = Candidate(
        kind="stdio",
        name="o",
        command=["/usr/bin/python3.13", "/x/mcp_client_bridge_build_example_test.py"],
        env={"MCP_CLIENT_SERVICE": "https://other.example.test"},
    )
    assert same_server.key(here, settings) != same_server.key(other, settings), "another host is another server"


# == second run on the other machine (2026-10-18, pid 60022) ===================================================
#
# build-example-net refused the login in its entry: it was build.opensuse.org's. Both client bridges drove an osc
# that machine did not have. build-opensuse-org ran a build of osc-mcp from the agent workspace beside the
# installed one, and asked for BUILD_OPENSUSE_ORG_OSC_MCP_USER/_PASSWORD with "shape unknown".


class FakeRunner:
    """which() answers from a set the fake installers add to; run() records what it ran."""

    def __init__(self, present, installs=None):
        self.present, self.installs, self.ran = set(present), dict(installs or {}), []

    def which(self, name, extra_dirs=()):
        return f"/usr/bin/{name}" if name in self.present else None

    def run(self, argv, **kw):
        self.ran.append(list(argv))
        self.present |= set(self.installs.get(argv[0], ()))
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def client_bridge() -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="build-example-test-client-bridge",
        command=["python3", "/b/mcp_client_bridge_build_example_test.py"],
    )
    candidate.requires = {"osc": [["zypper", "install", "osc"], ["pipx", "install", "osc"]]}
    return candidate


def test_a_missing_client_is_installed_before_the_trial(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    runner = FakeRunner({"python3", "pipx"}, installs={"pipx": {"osc"}})
    monkeypatch.setattr(ctx, "runner", runner)
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier, "_provisioned", lambda *a, **k: "")
    assert verifier._ensure_required(client_bridge()) == ""
    assert runner.ran == [["pipx", "install", "osc"]], "only installers that are present are tried"


def test_a_client_that_cannot_be_installed_makes_the_bridge_broken(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    monkeypatch.setattr(ctx, "runner", FakeRunner({"python3"}))
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier, "_provisioned", lambda *a, **k: "")
    why = verifier._ensure_required(client_bridge())
    assert why.startswith("osc, which this server runs for every call, is not installed") and "zypper" in why


def test_an_existing_bridge_names_its_client_through_its_manifest(ctx, tmp_path) -> None:
    import json as _json

    from initialize_claude_ai.mcp_auto_discovery.client_bridge import requirements

    manifest = tmp_path / "m.client.json"
    manifest.write_text(_json.dumps({"client": "osc", "read": ["osc", "-A", "https://x", "api", "{path}"]}))
    existing = Candidate(
        kind="stdio", name="b", command=["python3", "/b/b.py"], env={"MCP_CLIENT_MANIFEST": str(manifest)}
    )
    found = requirements(existing, ctx.settings)
    assert list(found) == ["osc"] and found["osc"], "the configured ways to install osc"


def test_a_login_another_organisation_accepts_is_named(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    def tried(name: str, verdict: str) -> Candidate:
        candidate = Candidate(kind="stdio", name=name, command=["x"])
        candidate.handshake = Handshake(
            ok=True,
            tools=["whoami"],
            round_trip=True,
            login=verdict,
            login_refused=verdict == login_check.REJECTED,
            login_parts=["pw-print"],
        )
        return candidate

    internal = TargetOutcome(target="https://build.corp.test")
    internal.stages.append(
        StageOutcome(stage=Stage.BRIDGE, candidates=[tried("build-corp-test", login_check.REJECTED)])
    )
    public = TargetOutcome(target="https://build.example.org")
    public.stages.append(
        StageOutcome(stage=Stage.BRIDGE, candidates=[tried("build-example-org", login_check.ACCEPTED)])
    )
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_login_of_another_service(result, [internal, public])
    [step] = result.steps
    assert "build.corp.test refused the login build.example.org accepts" in step.remediation[0]
    assert "corp.test issues you" in step.remediation[0]


def test_one_login_across_one_organisation_is_not_reported(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    def tried(target: str, verdict: str) -> TargetOutcome:
        candidate = Candidate(kind="stdio", name="c", command=["x"])
        candidate.handshake = Handshake(ok=True, login=verdict, login_parts=["pw-print"])
        outcome = TargetOutcome(target=target)
        outcome.stages.append(StageOutcome(stage=Stage.BRIDGE, candidates=[candidate]))
        return outcome

    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_login_of_another_service(
        result, [tried("https://a.corp.test", login_check.REJECTED), tried("https://b.corp.test", login_check.ACCEPTED)]
    )
    assert not result.steps


def test_secret_parts_are_printed_apart_from_the_user_name() -> None:
    from initialize_claude_ai.mcp_auto_discovery.secret_policy import secret_part_prints

    one = secret_part_prints({"OSC_MCP_USER": "someone", "OSC_MCP_PASSWORD": "pw"})
    other = secret_part_prints({"BUILD_X_USERNAME": "another", "BUILD_X_PASSWORD": "pw"})
    assert one == other and len(one) == 1, "the same password, whoever the user"


def test_a_login_part_says_whose_account_it_is(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import bridge_auth

    text = bridge_auth.describe_part("USERNAME", "build.corp.test", settings)
    assert "build.corp.test" in text and "the account corp.test issues you" in text


def test_a_login_slot_is_named_for_what_it_holds(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(
        kind="stdio", name="osc-mcp", command=["osc-mcp"], env={"OSC_MCP_API": "https://api.example.org"}
    )
    candidate.secret_env = {"OSC_MCP_USER": "env:OSC_MCP_USER", "OSC_MCP_PASSWORD": "env:OSC_MCP_PASSWORD"}
    writer = McpWriter(ctx)
    entry = writer.entry_for(candidate, "build-example-org")
    assert entry["env"]["OSC_MCP_USER"].startswith("${BUILD_EXAMPLE_ORG_USERNAME")
    assert entry["env"]["OSC_MCP_PASSWORD"].startswith("${BUILD_EXAMPLE_ORG_PASSWORD")
    assert "shape unknown" not in writer.credential_placeholder(candidate, "BUILD_EXAMPLE_ORG_PASSWORD")


def test_a_token_keeps_the_name_its_server_gives_it(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(kind="stdio", name="m", command=["m"], env={"URL": "https://example.test"})
    candidate.secret_env = {"VENDOR_API_KEY": "env:VENDOR_API_KEY"}
    assert McpWriter(ctx)._exported_name("VENDOR_API_KEY", "example-test", candidate) == "EXAMPLE_TEST_VENDOR_API_KEY"


def test_the_installed_program_replaces_a_workspace_build(ctx, monkeypatch, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery import runtime
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    workspace = tmp_path / "agent-workspace"
    (workspace / "artifacts" / "x" / "bin").mkdir(parents=True)
    monkeypatch.setattr(runtime, "workspace_for", lambda ctx: workspace)
    monkeypatch.setattr(ctx, "runner", FakeRunner({"osc-mcp"}))
    built = workspace / "artifacts" / "x" / "bin" / "osc-mcp"
    candidate = Candidate(kind="stdio", name="osc-mcp", command=[str(built)])
    candidate.handshake = Handshake(ok=True, server_name="osc-mcp", tools=["a", "b"], round_trip=True)
    writer = McpWriter(ctx)

    def verify(variants, **kw):
        for v in variants:
            v.handshake = Handshake(ok=True, server_name="osc-mcp", tools=["b", "a"], round_trip=True)
        return list(variants)

    monkeypatch.setattr(writer.verifier, "verify_all", verify)
    assert writer.prefer_installed(candidate)
    assert candidate.command == ["/usr/bin/osc-mcp"]


def test_a_different_program_of_the_same_name_is_not_taken(ctx, monkeypatch, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery import runtime
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    workspace = tmp_path / "agent-workspace"
    monkeypatch.setattr(runtime, "workspace_for", lambda ctx: workspace)
    monkeypatch.setattr(ctx, "runner", FakeRunner({"osc-mcp"}))
    built = str(workspace / "bin" / "osc-mcp")
    candidate = Candidate(kind="stdio", name="osc-mcp", command=[built])
    candidate.handshake = Handshake(ok=True, server_name="osc-mcp", tools=["a"], round_trip=True)
    writer = McpWriter(ctx)

    def verify(variants, **kw):
        for v in variants:
            v.handshake = Handshake(ok=True, server_name="something-else", tools=["z"], round_trip=True)
        return list(variants)

    monkeypatch.setattr(writer.verifier, "verify_all", verify)
    assert writer.prefer_installed(candidate) == "" and candidate.command == [built]


def test_second_run_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import bridge_auth
    from initialize_claude_ai.mcp_auto_discovery.writer import ROLE_PARTS

    assert settings.bool_("mcp.discovery.clients_install_missing") is True
    assert settings.bool_("mcp.verification.login_check.name_other_service_login") is True
    assert settings.str_("mcp.bridge.generated.auth.account") == bridge_auth.DEFAULT_ACCOUNT
    assert settings.bool_("mcp.registration.prefer_installed_program") is True
    assert settings.bool_("mcp.registration.name_by_role") is True
    assert tuple(settings.str_list("mcp.registration.name_by_role_parts")) == ROLE_PARTS


def test_the_user_name_of_a_login_is_never_dropped_as_an_unneeded_setting(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    monkeypatch.delenv("BUILD_EXAMPLE_TEST_USERNAME", raising=False)
    candidate = Candidate(
        kind="stdio",
        name="b",
        command=["python3", "/b/b.py"],
        env={"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME}"},
    )
    candidate.secret_env = {"BUILD_EXAMPLE_TEST_PASSWORD": "env:BUILD_EXAMPLE_TEST_PASSWORD"}
    candidate.unset_settings = ["BUILD_EXAMPLE_TEST_USERNAME"]
    env = McpWriter(ctx)._without_unset_settings(candidate.env, candidate)
    assert env["BUILD_EXAMPLE_TEST_USERNAME"] == "${BUILD_EXAMPLE_TEST_USERNAME:-}"


# == third run on the other machine (2026-10-19, pid 3558) ======================================================
#
# build-example-net was kept without BUILD_EXAMPLE_NET_USERNAME although its own report said it needed it; its bridge
# then told the operator to export a variable the entry never passed. The installed osc-mcp was refused for a
# workspace build of the same server, a local server's slot was renamed to a variable it never reads, and a
# kept bridge's token fell back to "shape unknown".


def client_manifest(tmp_path, parts=("BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD")):
    import json as _json

    path = tmp_path / "build-example-test.basic.client.json"
    path.write_text(
        _json.dumps({"client": "osc", "read": ["osc"], "parts": {p: "" for p in parts}, "optional_parts": []})
    )
    return str(path)


def test_an_entry_is_never_written_short_of_what_its_bridge_reads(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(
        kind="stdio", name="b", command=["python3", "/b/b.py"], env={"MCP_CLIENT_MANIFEST": client_manifest(tmp_path)}
    )
    candidate.secret_env = {"BUILD_EXAMPLE_TEST_PASSWORD": "env:BUILD_EXAMPLE_TEST_PASSWORD"}
    writer = McpWriter(ctx)
    assert writer.complete_slots(candidate) == ["BUILD_EXAMPLE_TEST_USERNAME"]
    entry = writer.entry_for(candidate, "build-example-test")
    assert entry["env"]["BUILD_EXAMPLE_TEST_USERNAME"] == "${BUILD_EXAMPLE_TEST_USERNAME:-}"


def test_an_existing_entry_missing_only_a_slot_is_repaired_not_replaced(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, Registration
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    entry = {
        "command": "python3",
        "args": ["/b/b.py"],
        "env": {
            "MCP_CLIENT_MANIFEST": client_manifest(tmp_path),
            "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD}",
        },
    }
    candidate = Candidate(kind="stdio", name="build-example-test", command=["python3", "/b/b.py"])
    why = TraditionalPipeline(ctx).repair_or_mismatch(
        Registration(name="build-example-test", entry=entry, source=PROJECT_FILE),
        candidate,
        TargetOutcome(target="https://build.example.test"),
    )
    assert why == "" and candidate.completed_slots == ["BUILD_EXAMPLE_TEST_USERNAME"]
    ctx.settings.set("mcp.discovery.reuse.repair_missing_slots", False)
    assert "BUILD_EXAMPLE_TEST_USERNAME" in TraditionalPipeline(ctx).repair_or_mismatch(
        Registration(name="build-example-test", entry=entry, source=PROJECT_FILE),
        Candidate(kind="stdio", name="x", command=["x"]),
        TargetOutcome(target="https://build.example.test"),
    )


def test_a_variable_the_entry_does_not_pass_is_named_as_such(tmp_path) -> None:
    """The generated client bridge tells "not passed" from "passed empty"."""
    import json as _json
    import os as _os
    import subprocess
    import sys

    from initialize_claude_ai.mcp_auto_discovery.client_bridge import _SOURCE

    script = tmp_path / "bridge.py"
    script.write_text(_SOURCE)
    manifest = tmp_path / "m.json"
    manifest.write_text(
        _json.dumps(
            {
                "server": "s",
                "client": "osc",
                "read": ["osc", "api", "{path}"],
                "config": "",
                "config_variable": "",
                "isolate": [],
                "parts": {"X_USERNAME": "user = {value}"},
                "extra_lines": [],
                "file_parts": [],
                "optional_parts": [],
                "key_part": "",
                "key_files": [],
                "timeout": 5,
                "tools": [{"name": "whoami", "summary": "", "command": ["osc"], "path": "", "params": {}}],
            }
        )
    )
    requests = (
        "\n".join(
            _json.dumps(m)
            for m in [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}},
            ]
        )
        + "\n"
    )

    def ask(env):
        base = {"PATH": _os.environ.get("PATH", ""), "MCP_CLIENT_MANIFEST": str(manifest)}
        out = subprocess.run(
            [sys.executable, str(script)],
            input=requests,
            capture_output=True,
            text=True,
            env={**base, **env},
            timeout=30,
        ).stdout
        return [_json.loads(line) for line in out.splitlines() if line.strip()][-1]["result"]["content"][0]["text"]

    assert "the entry does not pass X_USERNAME" in ask({})
    assert "X_USERNAME not set -- export it" in ask({"X_USERNAME": ""})


def test_a_local_servers_slot_keeps_the_key_it_reads(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(kind="stdio", name="osc-mcp", command=["osc-mcp"])
    candidate.secret_env = {"OSC_MCP_PASSWORD": "env:OSC_MCP_PASSWORD"}
    assert McpWriter(ctx).prepare_provisional(candidate, "build-example-org") == ["OSC_MCP_PASSWORD"]
    assert list(candidate.secret_env) == ["OSC_MCP_PASSWORD"]


def test_the_installed_program_is_compared_by_outcome(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    def walled(name: str) -> Candidate:
        candidate = Candidate(kind="stdio", name=name, command=[name])
        candidate.handshake = Handshake(ok=False, requires_auth=True, auth_evidence="failed to get credentials")
        return candidate

    assert McpWriter._answers_alike(walled("build"), walled("installed"))
    working = Candidate(kind="stdio", name="w", command=["w"])
    working.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    assert not McpWriter._answers_alike(working, walled("installed")), "a working build is not given up for less"


def test_a_kept_bridge_is_described_from_its_manifest(ctx, tmp_path) -> None:
    import json as _json

    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    manifest = tmp_path / "desk.operations.json"
    manifest.write_text(
        _json.dumps(
            {
                "base_url": "https://desk.example.test",
                "auth": {"scheme": "bearer", "prefix": "Bearer", "variables": {"token": "DESK_TOKEN"}},
            }
        )
    )
    candidate = Candidate(
        kind="stdio", name="desk", command=["python3", "/b/bridge.py"], env={"MCP_BRIDGE_MANIFEST": str(manifest)}
    )
    candidate.secret_env = {"DESK_TOKEN": "env:DESK_TOKEN"}
    placeholder = McpWriter(ctx).credential_placeholder(candidate, "DESK_TOKEN")
    assert "desk.example.test" in placeholder and "bearer" in placeholder.lower() and "shape unknown" not in placeholder


def test_the_log_header_names_the_code_that_ran() -> None:
    from initialize_claude_ai.core.logging_setup import code_fingerprint

    assert code_fingerprint().startswith("sha256:")


def test_third_run_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.discovery.reuse.repair_missing_slots") is True
    assert settings.bool_("mcp.registration.complete_slots") is True
    assert settings.bool_("mcp.registration.keep_server_slot_keys") is True
    assert settings.bool_("mcp.registration.describe_from_manifest") is True
    assert "does not pass" in settings.str_list("mcp.verification.login_check.missing_credential_markers")


def test_the_current_entry_is_tried_beside_the_one_that_last_worked(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import known_good
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    remembered = Candidate(kind="stdio", name="remembered", command=["a"])
    current = Candidate(kind="stdio", name="current", command=["b"])
    monkeypatch.setattr(known_good, "candidates", lambda *a, **k: [remembered])
    monkeypatch.setattr(ExistingRegistrations, "for_target", lambda self, *a, **k: [current])
    pipeline = TraditionalPipeline(ctx)
    asked: dict = {}

    def verify_all(candidates, stop_at_first=True, **kw):
        asked["stop"] = stop_at_first
        return []

    monkeypatch.setattr(pipeline.verifier, "verify_all", verify_all)
    pipeline.reuse(Stage.PACKAGE, TargetOutcome(target="https://build.example.org"))
    assert asked["stop"] is False, "both are judged; proof decides, not order"


def test_the_current_registration_keeps_its_own_name(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    remembered = Candidate(kind="stdio", name="build-example-org", command=["python3", "/b/bridge.py"])
    entry = {"command": "/opt/osc-mcp", "env": {"OSC_MCP_API": "https://api.example.org"}}
    current = ExistingRegistrations.for_run(ctx).candidate(
        Registration(name="build-example-org", entry=entry, source=PROJECT_FILE), Stage.PACKAGE
    )
    names = McpWriter(ctx).name_all([remembered, current], "https://build.example.org")
    assert names[1] == "build-example-org" and names[0] != "build-example-org"


def test_two_servers_for_one_address_share_its_credential_variables(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    writer = McpWriter(ctx)
    first = Candidate(kind="stdio", name="osc-mcp", command=["osc-mcp"], env={"OSC_MCP_API": "https://api.example.org"})
    first.secret_env = {"OSC_MCP_PASSWORD": "env:BUILD_EXAMPLE_ORG_PASSWORD"}
    writer.published.append(
        Registration(
            name="build-example-org",
            candidate=first,
            entry={
                "env": {"OSC_MCP_PASSWORD": "${BUILD_EXAMPLE_ORG_PASSWORD}", "OSC_MCP_API": "https://api.example.org"}
            },
        )
    )
    writer.address_of.update(
        {"build-example-org": "https://build.example.org", "build-example-org-stdio": "https://build.example.org"}
    )
    second = Candidate(
        kind="stdio",
        name="bridge",
        command=["python3", "/b/b.py"],
        env={"MCP_CLIENT_SERVICE": "https://build.example.org"},
    )
    second.secret_env = {"BUILD_EXAMPLE_ORG_PASSWORD": "env:BUILD_EXAMPLE_ORG_PASSWORD"}
    assert (
        writer._shared_name("BUILD_EXAMPLE_ORG_PASSWORD", "build-example-org-stdio", second)
        == "BUILD_EXAMPLE_ORG_PASSWORD"
    )


def test_an_outdated_generated_bridge_is_refreshed_before_its_trial(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.client_bridge import _SOURCE
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.dry_run = False
    script = tmp_path / "mcp_client_bridge_x.py"
    lines = _SOURCE.splitlines()
    script.write_text("\n".join(lines[:2] + ["# an older body"]) + "\n")
    candidate = Candidate(kind="stdio", name="x", command=["python3", str(script)])
    assert TraditionalPipeline(ctx).refresh_generated_script(candidate, TargetOutcome(target="https://x.example.org"))
    assert script.read_text() == _SOURCE
    other = tmp_path / "somebody_elses.py"
    other.write_text("#!/usr/bin/env python3\nprint('mine')\n")
    stranger = Candidate(kind="stdio", name="y", command=["python3", str(other)])
    assert (
        TraditionalPipeline(ctx).refresh_generated_script(stranger, TargetOutcome(target="https://x.example.org")) == ""
    )
    assert other.read_text().endswith("print('mine')\n"), "a script this program did not write is never touched"


# == fourth run on the other machine (2026-10-19, pid 53581, code sha256:ef1cdcaa15f7) =========================
#
# build-example-net's username slot was there at its trial and missing from the file -- by a write path the publish
# check did not guard -- while the claude CLI held a different copy that loads first. build-opensuse-org ran
# /usr/local/bin/osc-mcp, and ICA_MCP_OSC_MCP, which moves it, was named nowhere the operator reads.


def short_entry(tmp_path) -> dict:
    return {
        "command": "python3",
        "args": ["/b/b.py"],
        "env": {
            "MCP_CLIENT_MANIFEST": client_manifest(tmp_path),
            "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD:-}",
        },
    }


def test_the_file_as_written_is_checked_and_repaired(ctx, sandbox, tmp_path) -> None:
    import json as _json

    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    path = sandbox / ".mcp.json"
    ctx.settings.set("mcp.mcp_json_path", str(path))
    path.write_text(
        _json.dumps({"mcpServers": {"build-example-test": short_entry(tmp_path), "other": {"command": "x"}}})
    )
    writer = McpWriter(ctx)
    rows = writer.verify_written({"build-example-test"})
    written = _json.loads(path.read_text())["mcpServers"]
    assert written["build-example-test"]["env"]["BUILD_EXAMPLE_TEST_USERNAME"] == "${BUILD_EXAMPLE_TEST_USERNAME:-}"
    assert written["other"] == {"command": "x"}, "an entry outside the run is left alone"
    assert rows and "BUILD_EXAMPLE_TEST_USERNAME" in rows[0]["change"]


def test_a_cli_copy_missing_a_slot_is_replaced_by_the_files_entry(ctx, sandbox, tmp_path, monkeypatch) -> None:
    import json as _json

    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    path = sandbox / ".mcp.json"
    ctx.settings.set("mcp.mcp_json_path", str(path))
    good = short_entry(tmp_path)
    good["env"]["BUILD_EXAMPLE_TEST_USERNAME"] = "${BUILD_EXAMPLE_TEST_USERNAME:-}"
    path.write_text(_json.dumps({"mcpServers": {"build-example-test": good}}))
    monkeypatch.setattr(
        ExistingRegistrations,
        "cli_config_entries",
        lambda self: {"build-example-test": (short_entry(tmp_path), ["local"])},
    )
    ran: list = []
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setattr(
        ctx.runner,
        "run",
        lambda argv, **kw: (
            ran.append(list(argv)) or SimpleNamespace(ok=True, simulated=False, returncode=0, stdout="", stderr="")
        ),
    )
    rows = McpWriter(ctx).verify_written({"build-example-test"})
    assert any(a[:3] == ["/usr/bin/claude", "mcp", "remove"] for a in ran)
    added = [a for a in ran if a[:3] == ["/usr/bin/claude", "mcp", "add-json"]]
    assert added and "BUILD_EXAMPLE_TEST_USERNAME" in added[0][4] and added[0][-2:] == ["--scope", "local"]
    assert rows[0]["where"] == "the claude CLI" and "lacked" in rows[0]["change"]


def test_each_machine_specific_path_says_which_variable_moves_it(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    entry = {"command": "${ICA_MCP_OSC_MCP:-/usr/local/bin/osc-mcp}", "env": {"PATH": "/usr/bin:/bin"}}
    assert McpWriter._path_variables_in(entry) == {"ICA_MCP_OSC_MCP": "/usr/local/bin/osc-mcp"}


def test_fourth_run_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.registration.verify_written") is True
    assert settings.bool_("mcp.registration.replace_divergent_cli_copies") is True
    assert settings.bool_("mcp.registration.say_how_to_move") is True


# == fifth run (2026-10-19, pid 93893, code sha256:a01bf5702417 -- the current code) ===========================
#
# build-example-net's username slot was dropped on every reuse: ${NAME:-} expanded to "", no reference was kept, and
# empty values are left out of an entry (see test_mcp_entry_round_trip). Its Example login was then refused over basic
# auth while api.example.net asked for an SSH-key signature -- a route no remedy named.


def test_a_refused_login_names_the_routes_it_was_not_tried_on(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(ctx)
    writer.login_plans["https://build.example.test"] = {
        "chosen": {"method": "basic"},
        "offered": [
            {"host": "build.example.test", "scheme": "basic", "evidence": 'basic realm="API login"'},
            {
                "host": "api.example.test",
                "scheme": "Signature",
                "kind": "client",
                "evidence": 'Signature realm="Use your developer account",headers="(created)"',
            },
        ],
    }
    candidate = Candidate(kind="stdio", name="b", command=["python3", "/b/b.py"])
    candidate.secret_env = {"BUILD_EXAMPLE_TEST_USERNAME": "env:U", "BUILD_EXAMPLE_TEST_PASSWORD": "env:P"}
    candidate.handshake = Handshake(
        ok=True,
        login=login_check.REJECTED,
        login_refused=True,
        login_detail="osc exited 1: HTTP Error 401: Unauthorized",
    )
    remedy = writer.login_remedy(candidate, [], "https://build.example.test")
    assert "Signature at api.example.test" in remedy and "Use your developer account" in remedy
    assert "Register the key's .pub" in remedy, "the route's own setup step, from the client that performs it"


def test_nothing_sent_is_not_a_refusal_when_ranking() -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import proof_rank

    missing = Candidate(kind="stdio", name="m", command=["m"])
    missing.handshake = Handshake(
        ok=True,
        login=login_check.REJECTED,
        login_refused=True,
        login_detail="401 Unauthorized: no SSH key to sign with",
    )
    refused = Candidate(kind="stdio", name="r", command=["r"])
    refused.handshake = Handshake(
        ok=True, login=login_check.REJECTED, login_refused=True, login_detail="HTTP Error 401: Unauthorized"
    )
    assert proof_rank(missing) < proof_rank(refused)


def test_a_kept_entrys_login_counts_as_another_services(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    internal = TargetOutcome(target="https://build.corp.test")
    refused = Candidate(kind="stdio", name="build-corp-test", command=["x"])
    refused.handshake = Handshake(
        ok=True,
        login=login_check.REJECTED,
        login_refused=True,
        login_parts=["pw"],
        login_detail="HTTP Error 401: Unauthorized",
    )
    internal.stages.append(StageOutcome(stage=Stage.BRIDGE, candidates=[refused]))
    public = TargetOutcome(target="https://build.example.org")
    kept = Candidate(kind="stdio", name="build-example-org", command=["osc-mcp"])
    kept.existing_name = "build-example-org"
    kept.handshake = Handshake(ok=False, login_parts=["pw"], error="kept: Claude Code connects to it")
    public.connected_existing = {"build-example-org": "claude_cli"}
    public.stages.append(StageOutcome(stage=Stage.PACKAGE, candidates=[kept]))
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_login_of_another_service(result, [internal, public])
    [step] = result.steps
    assert "refused the login build.example.org uses (its build-example-org entry)" in step.remediation[0]


def test_fifth_run_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.verification.login_check.name_other_routes") is True
    assert settings.bool_("mcp.verification.login_check.other_service_counts_kept") is True
