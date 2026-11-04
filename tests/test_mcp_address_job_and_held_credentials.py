"""build-example-net and sd-example-com, as the other machine's log showed them.

build-example-net: its working client bridge (``claude mcp list``: connected) was
judged "``${ICA_MCP_PYTHON3_13}`` is not installed" -- read through ``claude mcp
get``, which prints ``${NAME:-default}`` as ``${NAME}`` -- and replaced by an
mcp-openapi-proxy that "worked" on build.example.net's public ``/about``: no usage
task matched its path-named tools, so its login became optional
(``"EXTRA_HEADERS": "${BUILD_EXAMPLE_NET_EXTRA_HEADERS:-}"``), while the shell had
exported ``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`` -- exactly what that slot takes.
The CLI copy then could not be removed: "exists in multiple scopes".

sd-example-com: "logins offered: OAuth@sd.example.com, Basic@api.example.com; chosen:
token" -- api.example.com credited to it by spelling, the password ruled out because
its challenge names OAuth, and the only credential tried was jira.example.com's
token, whose refusals were read as sd's verdicts.
"""

from __future__ import annotations

import base64
import json
import sys

import pytest

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery import bridge_auth, inventory, login_check, login_methods
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome

ENCODED = base64.b64encode(b"alice:wonderland").decode()


# -- reading entries the way Claude Code keeps them ----------------------------------------------


def test_cli_entries_are_read_from_claude_code_s_own_configuration(ctx, sandbox, monkeypatch, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    monkeypatch.chdir(tmp_path)
    entry = {"command": "${ICA_MCP_PYTHON3_13:-/usr/bin/python3.13}", "args": ["bridge.py"]}
    (sandbox / "home" / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"build-example-test": entry},
                "projects": {
                    str(tmp_path): {"mcpServers": {"build-example-test": entry, "local-only": {"command": "x"}}}
                },
            }
        )
    )
    held = ExistingRegistrations(ctx).cli_config_entries()
    assert held["build-example-test"][0]["command"] == entry["command"], "the default is kept, as Claude Code keeps it"
    assert held["build-example-test"][1] == ["local", "user"] and held["local-only"][1] == ["local"]


def test_a_variable_derived_from_a_file_name_resolves_back(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    monkeypatch.setattr(ctx.runner, "which", lambda name, **_: "/usr/bin/python3.13" if name == "python3.13" else None)
    assert McpWriter(ctx).resolve_program_variable("ICA_MCP_PYTHON3_13") == "/usr/bin/python3.13"
    assert McpWriter(ctx).resolve_program_variable("SOMEBODY_ELSES") == ""


def test_derived_names_are_remembered_when_written(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, recorded_path_variables

    ctx.dry_run = False
    writer = McpWriter(ctx)
    assert writer.portable("/opt/tools/bin/python3.13", writer.path_roots()).startswith("${ICA_MCP_PYTHON3_13:-")
    assert recorded_path_variables(ctx)["ICA_MCP_PYTHON3_13"] == "/opt/tools/bin/python3.13"
    assert writer.resolve_program_variable("ICA_MCP_PYTHON3_13") == "/opt/tools/bin/python3.13"


def test_a_connected_entry_is_never_judged_broken_by_the_run_s_own_launch(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.dry_run = False
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {
                        "command": "${ICA_NOBODY_KNOWS}",
                        "args": ["--server", "https://build.example.test"],
                    }
                }
            }
        )
    )
    monkeypatch.setattr(ExistingRegistrations, "cli_connected", lambda self, name: True)
    outcome = TargetOutcome(target="https://build.example.test")
    TraditionalPipeline(ctx).reuse("package", outcome)
    assert "build-example-test" in outcome.connected_existing
    assert "build-example-test" in outcome.answered_existing
    assert not outcome.broken_existing


def test_a_cli_copy_is_removed_from_every_scope_holding_it(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    calls: list[list[str]] = []

    class Done:
        ok, simulated = True, False

    ctx.dry_run = False
    monkeypatch.setattr(ctx.runner, "run", lambda argv, **_: calls.append(list(argv)) or Done())
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "cli_scopes", lambda name: ["local", "user"])
    assert writer._remove_from_cli("claude", "build-example-test", "replace") is True
    assert calls == [
        ["claude", "mcp", "remove", "build-example-test", "--scope", "local"],
        ["claude", "mcp", "remove", "build-example-test", "--scope", "user"],
    ]


# -- a public call is not the job ---------------------------------------------------------------------

PUBLIC_ONLY_SERVER = """
import json, os, sys
tools = [{"name": "get_about", "inputSchema": {"type": "object", "properties": {}}},
         {"name": "get_person_by_login", "inputSchema": {"type": "object", "properties": {"login": {"type": "string"}},
                                                          "required": ["login"]}}]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "build-example-test", "version": "0.4.0"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "<about><title>Open Build Service API</title></about>"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_public_call_does_not_make_the_login_optional(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.delenv("BUILD_EXAMPLE_TEST_EXTRA_HEADERS", raising=False)
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    script = tmp_path / "proxy.py"
    script.write_text(PUBLIC_ONLY_SERVER)
    candidate = Candidate(
        kind="stdio",
        name="openapi-proxy",
        command=[sys.executable, str(script)],
        env={"SERVER_URL_OVERRIDE": "https://build.example.test"},
    )
    candidate.secret_env = {"EXTRA_HEADERS": "env:BUILD_EXAMPLE_TEST_EXTRA_HEADERS"}
    candidate = CandidateVerifier(ctx).verify(candidate)
    shake = candidate.handshake
    assert not shake.credential_optional, "an anonymous /about is not the build service's job"
    assert not candidate.verified


def test_a_working_server_that_cannot_be_asked_the_job_settles_nothing(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    pipeline.api_kinds_for = lambda outcome: {}  # type: ignore[method-assign]
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.auth_plan = {"product": "open_build_service"}
    proxy = Candidate(kind="stdio", name="openapi-proxy", command=["uvx", "mcp-openapi-proxy"])
    proxy.handshake = Handshake(
        ok=True, tools=["get_about", "get_architectures"], round_trip=True, login=login_check.UNCHECKED
    )
    assert pipeline.misses_the_job(proxy, outcome)
    assert pipeline.unproved_with_alternative(proxy, outcome), "it does not settle the address"
    proxy.handshake.tools.append("get_my_notifications")
    assert not pipeline.misses_the_job(proxy, outcome)
    accepted = Candidate(kind="stdio", name="x", command=["x"])
    accepted.handshake = Handshake(ok=True, tools=["gitlab_get_project"], round_trip=True, login=login_check.ACCEPTED)
    outcome.auth_plan = {"product": "gitlab"}
    assert not pipeline.unproved_with_alternative(accepted, outcome), "an accepted login is proof enough"


# -- logins: proved by trying, not limited by a challenge -----------------------------------------------


def respond(status: int, text: str, challenge: str = ""):
    return Response(url="", status=status, headers={"www-authenticate": challenge} if challenge else {}, text=text)


def test_a_password_the_challenge_does_not_name_is_found_by_a_stand_in(settings) -> None:
    get = lambda url: respond(401, '{"message":"Client must be authenticated"}', 'OAuth realm="sd"')  # noqa: E731
    understood = lambda url, headers: respond(401, '{"message":"Basic Authentication Failure - AUTHENTICATED_FAILED"}')  # noqa: E731
    found = login_methods.understood_methods("sd.example.test", "jira", [], settings, get, understood)
    assert [o["method"] for o in found] == ["basic"] and "stand-in" in found[0]["evidence"]
    ignored = lambda url, headers: respond(401, '{"message":"Client must be authenticated"}', 'OAuth realm="sd"')  # noqa: E731
    assert login_methods.understood_methods("sd.example.test", "jira", [], settings, get, ignored) == []
    plan = login_methods.plan(
        "sd.example.test",
        "jira",
        found,
        settings,
        same_login={"basic": {"ENCODED": "BUILD_EXAMPLE_TEST_BASE64", "HOST": "build.example.test"}},
    )
    assert plan["chosen"]["method"] == "basic", "the login the operator holds comes before a token to create"
    assert "single sign-on" in " ".join(login_methods.describe(plan, "sd.example.test"))


def test_without_anything_held_the_method_order_decides_as_before(settings) -> None:
    plan = login_methods.plan("gitlab.example.test", "gitlab", [], settings)
    assert plan["chosen"]["method"] == "token"


def test_a_generated_bridge_follows_the_login_plan(settings) -> None:
    assert bridge_auth.method_from_plan({"chosen": {"method": "basic"}})["scheme"] == "basic"
    token = {"chosen": {"method": "token", "sent_as": "Authorization: Bearer <token>"}}
    assert bridge_auth.method_from_plan(token)["scheme"] == "bearer"
    header = {"chosen": {"method": "apikey", "sent_as": "X-BUGZILLA-API-KEY: <key>"}}
    assert bridge_auth.method_from_plan(header) == {
        "scheme": "apikey",
        "in": "header",
        "name": "X-BUGZILLA-API-KEY",
        "evidence": "the login plan for the address (apikey)",
    }
    assert bridge_auth.method_from_plan({"chosen": {"method": "client"}}) == {}


def test_the_plan_decides_before_an_api_kind_s_default_but_not_before_a_server_s_spec(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder

    generator = BridgeBuilder(ctx)
    kind_default = {"components": {"securitySchemes": {"default": {"type": "http", "scheme": "bearer"}}}}
    plan = {"chosen": {"method": "basic"}}
    assert generator.auth_method(kind_default, {}, plan=plan, spec_is_evidence=False)["scheme"] == "basic"
    assert generator.auth_method(kind_default, {}, plan=plan, spec_is_evidence=True)["scheme"] == "bearer"


# -- what the operator already holds ------------------------------------------------------------------------


def test_the_inventory_reads_names_and_shapes(settings) -> None:
    env = {
        "BUILD_EXAMPLE_TEST_USERNAME": "alice",
        "BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD": ENCODED,
        "BUILD_EXAMPLE_TEST_SSH_PUBLIC_KEY": "/home/a/.ssh/id.pub",
        "OTHER_TEST_PASSWORD": "x",
    }
    assert inventory.held("build.example.test", settings, env) == {
        "basic": {"ENCODED": "BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD"}
    }
    wrong = {"BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD": "not-base64-of-a-login"}
    assert inventory.held("build.example.test", settings, wrong) == {}, "never a password because of its name"
    pair = {"BUILD_EXAMPLE_TEST_USERNAME": "a", "BUILD_EXAMPLE_TEST_PASSWORD": "b"}
    assert inventory.held("build.example.test", settings, pair)["basic"] == {
        "USERNAME": "BUILD_EXAMPLE_TEST_USERNAME",
        "PASSWORD": "BUILD_EXAMPLE_TEST_PASSWORD",
    }


def test_same_login_hosts_are_one_organisation(settings) -> None:
    assert inventory.same_login_hosts(
        "sd.example.com", ["build.example.de", "jira.example.com", "other.org"], settings
    ) == ["build.example.de", "jira.example.com"]


def test_a_slot_is_filled_by_what_it_holds(settings) -> None:
    form = inventory.slot_part("EXTRA_HEADERS", settings)
    assert form == "FORM:Authorization:Basic {ENCODED}"
    values = inventory.values_for({"ENCODED": "V"}, {"V": ENCODED})
    assert values["USERNAME"] == "alice" and values["PASSWORD"] == "wonderland"
    assert inventory.fill_slot(form, values) == f"Authorization:Basic {ENCODED}"
    assert inventory.slot_part("SD_EXAMPLE_TEST_USERNAME", settings) == "USERNAME"


def test_a_waiting_server_is_offered_what_the_operator_exports(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD", ENCODED)
    candidate = Candidate(
        kind="stdio",
        name="openapi-proxy",
        command=["uvx", "mcp-openapi-proxy"],
        env={"SERVER_URL_OVERRIDE": "https://build.example.test"},
    )
    candidate.secret_env = {"EXTRA_HEADERS": "env:BUILD_EXAMPLE_TEST_EXTRA_HEADERS"}
    CandidateVerifier(ctx)._borrow_held(candidate, ["EXTRA_HEADERS"], "build.example.test")
    assert candidate.in_use["EXTRA_HEADERS"].reveal() == f"Authorization:Basic {ENCODED}"
    assert candidate.held_names["EXTRA_HEADERS"] == "Authorization:Basic ${BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD}"


def test_only_the_parts_held_means_the_slot_is_built_at_start_up(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("BUILD_EXAMPLE_TEST_USERNAME", "alice")
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_PASSWORD", "wonderland")
    candidate = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy"])
    CandidateVerifier(ctx)._borrow_held(candidate, ["EXTRA_HEADERS"], "build.example.test")
    assert candidate.held_names["EXTRA_HEADERS"] == (
        "COMPOSE:Authorization:Basic {b64:BUILD_EXAMPLE_TEST_USERNAME:BUILD_EXAMPLE_TEST_PASSWORD}"
    )


def test_another_address_s_login_is_only_tried_never_written_under_its_names(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("BUILD_EXAMPLE_DE_BASE64_OF_USER_PASSWORD", ENCODED)
    ctx.state["mcp_servers"] = ["sd.example.com", "build.example.de"]
    candidate = Candidate(kind="stdio", name="bridge", command=["python3", "bridge.py"])
    CandidateVerifier(ctx)._borrow_held(
        candidate, ["SD_EXAMPLE_COM_USERNAME", "SD_EXAMPLE_COM_PASSWORD"], "sd.example.com"
    )
    assert candidate.in_use["SD_EXAMPLE_COM_USERNAME"].reveal() == "alice"
    assert "single sign-on" in candidate.in_use["SD_EXAMPLE_COM_PASSWORD"].source
    assert candidate.held_names == {}, "suggested, never read under another address's names"


def test_a_composed_slot_is_written_from_the_held_variables(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = Candidate(
        kind="stdio",
        name="proxy",
        command=["uvx", "mcp-openapi-proxy"],
        env={"SERVER_URL_OVERRIDE": "https://build.example.test"},
    )
    candidate.secret_env = {"EXTRA_HEADERS": "env:X"}
    candidate.composed = {"EXTRA_HEADERS": "Authorization:Basic ${BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD}"}
    entry = McpWriter(ctx).entry_for(candidate, "build-example-test")
    assert entry["env"]["EXTRA_HEADERS"] == "Authorization:Basic ${BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD}"


def test_a_slot_built_from_parts_starts_through_the_composer(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path))
    candidate = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy"])
    candidate.secret_env = {"EXTRA_HEADERS": "env:X"}
    candidate.composed = {"EXTRA_HEADERS": "COMPOSE:Authorization:Basic {b64:B_USERNAME:B_PASSWORD}"}
    entry = McpWriter(ctx).entry_for(candidate, "build-example-test")
    assert "EXTRA_HEADERS" not in entry.get("env", {})
    assert entry["env"]["B_USERNAME"] == "${B_USERNAME}" and entry["env"]["B_PASSWORD"] == "${B_PASSWORD}"
    assert entry["args"][1:3] == ["--set", "EXTRA_HEADERS=Authorization:Basic {b64:B_USERNAME:B_PASSWORD}"]
    assert entry["args"][3:] == ["--", "uvx", "mcp-openapi-proxy"]
    assert (tmp_path / "mcp_compose.py").is_file()


# -- foreign credentials, start-up wait, connected entries --------------------------------------------------


def test_a_token_other_hosts_send_is_known_as_theirs(ctx, sandbox) -> None:
    from initialize_claude_ai.core.secrets import Secret
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    token = "Zq8vK2mN4pR7sT1wX5yA9bC3dE6fG0hJ2kL4"  # gitleaks:allow (synthetic test value)
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jira-example-test": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://jira.example.test", "JIRA_PERSONAL_TOKEN": token},
                    },
                    "sd-example-test": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://sd.example.test", "JIRA_PERSONAL_TOKEN": token},
                    },
                }
            }
        )
    )
    existing = ExistingRegistrations(ctx)
    existing.registrations()
    assert existing.bound_elsewhere(Secret(token), "sd.example.test") == ["jira.example.test"]


def test_a_slow_first_start_is_reported_and_the_wait_raised(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy==0.4.0"])
    candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True, start_seconds=1.0)
    ctx.state["mcp_start_seconds"] = {"uvx mcp-openapi-proxy==0.4.0": 57.0}
    registration = Registration(name="build-example-test", candidate=candidate, entry={})
    McpWriter(ctx)._within_client_timeout(registration, candidate)
    assert any("57s" in line and "MCP_TIMEOUT" in line for line in registration.remediation)
    assert ctx.env.values.get("MCP_TIMEOUT") == str(86 * 1000)


def test_a_quick_start_says_nothing(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    candidate = Candidate(kind="stdio", name="proxy", command=["uvx", "x"])
    candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True, start_seconds=2.0)
    registration = Registration(name="x", candidate=candidate, entry={})
    McpWriter(ctx)._within_client_timeout(registration, candidate)
    assert registration.remediation == []


def test_connected_status_holds_no_place_unless_asked(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    outcome = TargetOutcome(target="https://build.example.test")
    outcome.connected_existing = {"build-example-test": "claude_cli"}
    outcome.answered_existing = {"build-example-test": "claude_cli"}
    unproven = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy"])
    unproven.handshake = Handshake(ok=True, tools=["get_about"], round_trip=True, login=login_check.UNCHECKED)
    registration = Registration(name="build-example-test", candidate=unproven, entry={"command": "uvx"})
    # "Connected" is a status, not proof: it holds no place by itself (gitlab.example.net's
    # bridge was refused for a CLI copy the run had discarded). The old rule, on request:
    assert McpWriter(ctx)._holds_its_place(registration, outcome, {}) == ""
    ctx.settings.set("mcp.registration.connected_holds_place", True)
    assert "Claude Code connects" in McpWriter(ctx)._holds_its_place(registration, outcome, {})
    proven = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy"])
    proven.handshake = Handshake(ok=True, tools=["get_my_notifications"], round_trip=True, login=login_check.ACCEPTED)
    registration = Registration(name="build-example-test", candidate=proven, entry={"command": "uvx"})
    assert "Claude Code connects" not in McpWriter(ctx)._holds_its_place(registration, outcome, {})


# -- the packaged settings equal the code ----------------------------------------------------------------


def test_packaged_settings_equal_the_code(settings) -> None:
    config = settings.section("mcp.discovery.auth.inventory")
    assert config.dict_("parts") == inventory.DEFAULT_PARTS
    assert tuple(config.str_list("composite_patterns")) == inventory.DEFAULT_COMPOSITE_PATTERNS
    assert config.dict_("methods") == inventory.DEFAULT_METHODS
    probe = settings.section("mcp.packages.credential_probe")
    assert probe.str_("placeholder_user") == login_methods.DEFAULT_STANDIN_USER
    assert probe.str_("placeholder") == login_methods.DEFAULT_STANDIN_SECRET
    auth = settings.section("mcp.discovery.auth")
    assert (
        auth.bool_("try_unoffered_methods")
        and auth.bool_("api_host_same_product")
        and auth.bool_("prefer_held_methods")
    )
    assert settings.bool_("mcp.contract.address_job") and settings.bool_(
        "mcp.verification.login_check.public_needs_the_job"
    )
    assert settings.bool_("mcp.verification.login_check.borrow_held")
    timeout = settings.section("mcp.registration.client_timeout")
    assert timeout.str_("variable") == "MCP_TIMEOUT" and timeout.float_("seconds") == 30
    assert settings.str_("mcp.discovery.reuse.claude_config") == "{config_dir}/.claude.json"
    assert settings.bool_("mcp.registration.replace_literal_cli_copies") is True


@pytest.mark.parametrize("name", ["get_my_notifications", "whoami", "get_my_account"])
def test_the_build_service_job_matches_path_named_tools(settings, name) -> None:
    from initialize_claude_ai.mcp_auto_discovery import contract

    tasks = contract.product_tasks(settings, ["open_build_service"])
    assert contract.askable(tasks, [name])


def test_stage_outcome_import_is_used() -> None:
    assert StageOutcome(stage="package").stage == "package"
