"""Remember what worked; read every way a service can be logged in to.

build.opensuse.org's osc-mcp answered ``get_request 11111`` -- a call that needs
the login -- on Oct 14; the next day's run wrote a bridge that failed every call
and knew nothing of the entry that had worked. build.example.net answers Basic and
its API host api.example.net answers ``Signature`` (SSH-key signed requests, which
only osc performs); the program read one challenge and would have asked for "a
token sent under the word Signature".
"""

from __future__ import annotations

import json

import pytest

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery import bridge_auth, known_good, login_check, login_methods
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome

TARGET = "https://build.example.test"
WORKED = {
    "command": "go",
    "args": ["run", "github.com/example/osc-mcp@v0.2.1"],
    "env": {"OSC_MCP_API": "api.example.test", "OSC_MCP_USER": "${OBS_USER}", "OSC_MCP_PASSWORD": "${OBS_PASSWORD}"},
}


def worked(contract: list | None = None, login: str = login_check.ACCEPTED, reason: str = "") -> Candidate:
    candidate = Candidate(kind="stdio", name="build-example-test", command=["go", "run", "x"])
    candidate.handshake = Handshake(
        ok=True,
        tools=["get_request"],
        round_trip=True,
        login=login,
        login_tool="get_request",
        login_reason=reason,
        contract=contract if contract is not None else [{"task": "read request 11111", "passed": True}],
    )
    return candidate


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    return ctx


# -- 11. the record -----------------------------------------------------------------------------------


def test_an_entry_that_did_the_job_is_recorded_without_any_value(live) -> None:
    assert known_good.record(live, TARGET, "build-example-test", WORKED, worked())
    row = known_good.last(live, TARGET)
    assert row["entry"] == WORKED and row["tasks"] == ["read request 11111"]
    assert oct(known_good.path(live).stat().st_mode)[-3:] == "600"


def test_what_did_not_do_the_job_is_never_recorded(live) -> None:
    assert not known_good.record(live, TARGET, "x", WORKED, worked(contract=[{"task": "t", "passed": False}]))
    assert not known_good.record(live, TARGET, "x", WORKED, worked(contract=[], reason=login_check.PUBLIC_ONLY))
    literal = {"command": "x", "env": {"OBS_PASSWORD": "a-literal-password-value-123"}}
    assert not known_good.record(live, TARGET, "x", literal, worked())
    assert known_good.last(live, TARGET) is None


def test_a_dry_run_records_nothing(ctx) -> None:
    assert not known_good.record(ctx, TARGET, "x", WORKED, worked())


# -- 12. tried first ---------------------------------------------------------------------------------


def test_the_last_working_entry_is_tried_first_at_its_tier(live, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    live.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {"command": "python3", "args": ["bridge.py"], "env": {"BASE_URL": TARGET}}
                }
            }
        )
    )
    known_good.record(live, TARGET, "build-example-test", WORKED, worked())
    tier = ExistingRegistrations.for_run(live).tier_of(WORKED)
    [candidate] = known_good.candidates(live, TARGET, tier)
    assert candidate.command == ["go"] and "last worked" in candidate.source
    assert ExistingRegistrations.for_run(live).registration_for(candidate) is None, "not a current registration"
    assert known_good.candidates(live, TARGET, Stage.ENDPOINT if tier != Stage.ENDPOINT else Stage.PACKAGE) == []


def test_an_entry_still_registered_as_it_was_is_not_tried_twice(live, sandbox) -> None:
    live.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"build-example-test": WORKED}}))
    known_good.record(live, TARGET, "build-example-test", WORKED, worked())
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    assert known_good.candidates(live, TARGET, ExistingRegistrations.for_run(live).tier_of(WORKED)) == []


# -- 13 and 14. what changed, and what never worked ------------------------------------------------------


def test_what_changed_is_said_in_names_never_values() -> None:
    now = {
        "command": "python3",
        "args": ["bridge.py"],
        "env": {
            "BASE_URL": "https://api.example.test",
            "OBS_PASSWORD": "${BUILD_PASSWORD}",
            "OBS_TOKEN": "s3cr3t-v4lue-xyz-0123456789abcdef",  # gitleaks:allow (synthetic test value)
        },
    }
    changes = known_good.differences(WORKED, now)
    text = " | ".join(changes)
    assert "go run github.com/example/osc-mcp@v0.2.1" in text and "python3 bridge.py" in text
    assert "OBS_USER" in text and "BUILD_PASSWORD" in text
    assert "s3cr3t" not in text


def test_the_writer_looks_back_when_nothing_works_now(live, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {"command": "python3", "args": ["bridge.py"], "env": {"BASE_URL": TARGET}}
                }
            }
        )
    )
    known_good.record(live, TARGET, "build-example-test", WORKED, worked())
    writer = McpWriter(live)
    writer._look_back(TargetOutcome(target=TARGET), [])
    [row] = writer.lookbacks
    assert row["name"] == "build-example-test" and any("launch" in c for c in row["changes"])


def test_only_public_successes_are_never_proved() -> None:
    assert known_good.never_proved(worked(reason=login_check.PUBLIC_ONLY))
    assert not known_good.never_proved(worked())


# -- 15. every way to log in ----------------------------------------------------------------------------


def test_every_challenge_is_read() -> None:
    found = login_methods.schemes(
        'Basic realm="API login", Signature realm="Use your developer account",headers="(created)"'
    )
    assert [c["scheme"] for c in found] == ["Basic", "Signature"]
    assert 'headers="(created)"' in found[1]["text"]


def test_a_signature_is_a_client_s_never_a_token(settings) -> None:
    assert login_methods.classify("Signature", settings)["kind"] == login_methods.CLIENT
    assert login_methods.classify("Basic", settings) == {"kind": login_methods.HEADER, "method": "basic"}
    assert login_methods.classify("Weird", settings)["kind"] == login_methods.UNKNOWN
    assert bridge_auth.method_from_challenge('Signature realm="x"', settings) == {}
    assert bridge_auth.method_from_challenge('Basic realm="x"', settings)["scheme"] == "basic"


def test_the_api_host_is_asked_too(settings) -> None:
    assert login_methods.api_hosts("build.example.test", settings) == ["api.example.test"]


def answers(table: dict[str, tuple[int, str, str]]):
    def get(url: str):
        status, text, challenge = table.get(url, (404, "", ""))
        headers = {"www-authenticate": challenge} if challenge else {}
        return Response(url=url, status=status, headers=headers, text=text)

    return get


def test_the_product_and_its_challenges_are_found(settings) -> None:
    get = answers(
        {
            "https://build.example.test/about": (200, "<about><title>Open Build Service API</title></about>", ""),
            "https://build.example.test/search/package?match=@name='initialize-claude-ai'&limit=1": (
                401,
                "",
                'basic realm="API login"',
            ),
            "https://api.example.test/search/package?match=@name='initialize-claude-ai'&limit=1": (
                401,
                "",
                'Signature realm="dev",headers="(created)"',
            ),
            "https://api.example.test/about": (200, "<about><title>Open Build Service API</title></about>", ""),
        }
    )
    product = login_methods.recognise("build.example.test", settings, get)
    assert product == "open_build_service"
    offered = login_methods.challenges(
        "build.example.test", login_methods.products(settings)[product], settings, get, product_name=product
    )
    assert {(o["host"], o["kind"]) for o in offered} == {
        ("build.example.test", "header"),
        ("api.example.test", "client"),
    }


def test_an_api_host_of_another_product_is_not_the_target_s(settings) -> None:
    """api.{domain} is a guess from the name: gitlab.example.test's api.example.test is the build service's."""
    get = answers(
        {
            "https://gitlab.example.test/users/sign_in": (200, "GitLab", ""),
            "https://gitlab.example.test/api/v4/user": (401, "", 'Bearer realm="gitlab"'),
            "https://api.example.test/about": (200, "<about><title>Open Build Service API</title></about>", ""),
            "https://api.example.test/api/v4/user": (401, "", 'Signature realm="dev"'),
        }
    )
    product = login_methods.recognise("gitlab.example.test", settings, get)
    assert product == "gitlab"
    offered = login_methods.challenges(
        "gitlab.example.test", login_methods.products(settings)[product], settings, get, product_name=product
    )
    assert {o["host"] for o in offered} == {"gitlab.example.test"}
    assert login_methods.same_product_api_hosts("sd.example.test", "", settings, get) == [], "unrecognised: none"


# -- 16. choosing ---------------------------------------------------------------------------------


def test_a_token_that_cannot_read_is_never_chosen_and_a_password_beats_an_ssh_key(settings) -> None:
    """The old ranking, kept reachable: with methods_equal off, a password still comes before a key."""
    settings.set("mcp.discovery.auth.methods_equal", False)
    offered = [
        {"host": "build.example.test", "scheme": "basic", "kind": "header", "method": "basic"},
        {"host": "api.example.test", "scheme": "Signature", "kind": "client", "method": "client"},
    ]
    plan = login_methods.plan("build.example.test", "open_build_service", offered, settings)
    assert plan["chosen"]["method"] == "basic" and plan["excluded"]
    assert any(a.get("method") == "client" for a in plan["alternatives"]), "the key stays an alternative"
    offered = offered[1:]
    plan = login_methods.plan("build.example.test", "open_build_service", offered, settings)
    text = " ".join(login_methods.describe(plan, "build.example.test"))
    assert "your account at https://api.example.test" in text and "cannot read" in text
    assert "never used" in text, "osc's own setup on this machine is never relied on"


def test_a_token_that_can_read_comes_first(settings) -> None:
    plan = login_methods.plan("gitlab.example.test", "gitlab", [], settings)
    assert plan["chosen"]["method"] == "token"
    text = " ".join(login_methods.describe(plan, "gitlab.example.test"))
    assert "https://gitlab.example.test/-/user_settings/personal_access_tokens" in text and "read_api" in text


def test_a_password_candidate_ranks_below_when_a_token_can_read(ctx) -> None:
    """The old ranking, kept reachable: with methods_equal off, a token outranks a password."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.settings.set("mcp.discovery.auth.methods_equal", False)
    outcome = TargetOutcome(target="https://gitlab.example.test")
    outcome.auth_plan = login_methods.plan("gitlab.example.test", "gitlab", [], ctx.settings)
    password = Candidate(kind="stdio", name="a", confidence=0.8)
    password.secret_env = {"GITLAB_PASSWORD": "env:GITLAB_PASSWORD"}
    token = Candidate(kind="stdio", name="b", confidence=0.8)
    token.secret_env = {"GITLAB_TOKEN": "env:GITLAB_TOKEN"}
    TraditionalPipeline(ctx).weigh_login_methods(
        StageOutcome(stage=Stage.PACKAGE, candidates=[password, token]), outcome
    )
    assert password.confidence < token.confidence


def test_packaged_settings_equal_the_code(settings) -> None:
    auth = settings.section("mcp.discovery.auth")
    assert auth.dict_("methods") == login_methods.DEFAULT_METHODS
    assert tuple(auth.str_list("method_order")) == login_methods.DEFAULT_METHOD_ORDER
    assert tuple(auth.str_list("api_host_patterns")) == login_methods.DEFAULT_API_HOST_PATTERNS
    assert tuple(auth.str_list("challenge_paths")) == login_methods.DEFAULT_CHALLENGE_PATHS
    kg = settings.section("mcp.known_good")
    assert kg.str_("file") == known_good.DEFAULT_FILE and kg.int_("keep") == known_good.DEFAULT_KEEP
    assert settings.float_("mcp.ranking.penalties.password_when_token") == 0.2


# -- every kind of login counts the same (mcp.discovery.auth.methods_equal) ----------------------


def test_with_methods_equal_the_api_hosts_own_login_is_chosen_whatever_its_kind(settings) -> None:
    """build.example.net: its API host offers only a key signature; the password is the web host's."""
    offered = [
        {"host": "build.example.test", "scheme": "basic", "kind": "header", "method": "basic"},
        {"host": "api.example.test", "scheme": "Signature", "kind": "client", "method": "client"},
    ]
    plan = login_methods.plan("build.example.test", "open_build_service", offered, settings)
    assert plan["chosen"]["method"] == "client"
    assert any(a.get("method") == "basic" for a in plan["alternatives"]), "the password stays an alternative"


def test_with_methods_equal_a_password_the_api_host_offers_is_kept(settings) -> None:
    """build.opensuse.org: its API host offers the password, so the password stays first."""
    offered = [
        {"host": "api.example.test", "scheme": "basic", "kind": "header", "method": "basic"},
        {"host": "api.example.test", "scheme": "Signature", "kind": "client", "method": "client"},
    ]
    plan = login_methods.plan(
        "build.example.test",
        "open_build_service",
        offered,
        settings,
        held={"basic": {"USERNAME": "X", "PASSWORD": "Y"}},
    )
    assert plan["chosen"]["method"] == "basic"


def test_with_methods_equal_no_kind_of_login_is_penalised(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    outcome = TargetOutcome(target="https://gitlab.example.test")
    outcome.auth_plan = login_methods.plan("gitlab.example.test", "gitlab", [], ctx.settings)
    password = Candidate(kind="stdio", name="a", confidence=0.8)
    password.secret_env = {"GITLAB_PASSWORD": "env:GITLAB_PASSWORD"}
    token = Candidate(kind="stdio", name="b", confidence=0.8)
    token.secret_env = {"GITLAB_TOKEN": "env:GITLAB_TOKEN"}
    TraditionalPipeline(ctx).weigh_login_methods(
        StageOutcome(stage=Stage.PACKAGE, candidates=[password, token]), outcome
    )
    assert password.confidence == token.confidence
