"""Prove the login, not the connection.

build-example-net "worked" for days: every successful call was ``get_about``, which
build.example.net answers anyone, and every call that needed a login failed. Its
entries carried the login under names nothing read
(``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`` for mcp-openapi-proxy, which reads
``API_KEY``/``EXTRA_HEADERS``), behind an auth type the server does not
implement (``API_AUTH_TYPE=Basic``), under a header *named* ``Bearer``, and --
in the end -- build.opensuse.org's login, which belongs to another instance.

The fake servers below are real processes speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import login_check, secret_policy, value_shapes
from initialize_claude_ai.mcp_auto_discovery.env_contract import EnvContract
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (112, 114, 111, 111, 102, 45, 116, 111, 107, 45, 57))

#: MODE: public -- every call answers the same with or without a login;
#: gated -- calls need the token; ignored -- 401 whatever is sent.
SERVER = """
import json, os, sys
mode = os.environ.get("MODE", "gated")
logged_in = os.environ.get("OBS_TOKEN") == os.environ.get("EXPECTED")
tools = [{"name": n, "inputSchema": {"type": "object", "properties": {}}} for n in ("get_about", "list_projects")]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-obs", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        if mode == "public" or (mode == "gated" and logged_in):
            result = {"content": [{"type": "text", "text": json.dumps([{"name": "openSUSE:Factory"}])}]}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: authentication required"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, tmp_path):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.secrets.resolve = (
        lambda reference, **_: Secret(TOKEN)
        if reference.startswith("env:") and reference.endswith("OBS_TOKEN")
        else None
    )  # type: ignore[assignment]
    return ctx


def fake(tmp_path, mode: str, expected: str = TOKEN) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="build-example-test",
        command=[sys.executable, str(script)],
        env={"BASE_URL": "https://build.example.test", "MODE": mode, "EXPECTED": expected},
    )
    candidate.secret_env = {"OBS_TOKEN": "env:OBS_TOKEN"}
    return candidate


# -- 1. a call anybody can make proves nothing ----------------------------------------------------


def test_an_answer_anybody_gets_proves_nothing(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify(fake(tmp_path, "public")).handshake
    assert shake.login == login_check.UNCHECKED and shake.login_reason == login_check.PUBLIC_ONLY
    assert "with no credential" in shake.login_detail


def test_a_call_that_needs_the_login_proves_it(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify(fake(tmp_path, "gated")).handshake
    assert shake.login == login_check.ACCEPTED and shake.login_reason == ""


def test_the_comparison_can_be_turned_off(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.compare_with_anonymous", False)
    assert CandidateVerifier(live).verify(fake(tmp_path, "public")).handshake.login == login_check.ACCEPTED


# -- 2. refused exactly as an anonymous caller ----------------------------------------------------


def test_a_refusal_identical_to_an_anonymous_one_says_the_credential_may_not_be_sent(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify(fake(tmp_path, "ignored")).handshake
    assert shake.login == login_check.REJECTED and shake.login_reason == login_check.SAME_AS_ANONYMOUS
    remedy = login_check.remedy(
        shake.login,
        host="build.example.test",
        variables=["OBS_TOKEN"],
        detail=shake.login_detail,
        settings=live.settings,
        reason=shake.login_reason,
    )
    assert "sends it at all" in remedy and TOKEN not in remedy


def test_same_answer_ignores_what_changes_between_calls() -> None:
    assert login_check.same_answer("401 at https://a.test/x/1 id 0123abcd99", "401 at https://b.test/y id ffee0011aa")
    assert not login_check.same_answer("401 Unauthorized", "403 Forbidden")


# -- 3. a header name is a header name ----------------------------------------------------------------


def test_a_scheme_word_is_not_a_header_name(settings) -> None:
    problem = value_shapes.mismatch("MCP_BRIDGE_AUTH_HEADER", "Bearer", settings)
    assert problem is not None and "Authorization" in problem["expected"]
    assert value_shapes.mismatch("MCP_BRIDGE_AUTH_HEADER", "Authorization", settings) is None
    assert value_shapes.mismatch("API_HEADERS", "Authorization:Bearer x", settings) is None


# -- 4. a credential under a name nothing reads -------------------------------------------------------


@pytest.fixture
def installed_proxy(tmp_path, monkeypatch):
    """A fake installed package, where the uv cache keeps one: it reads API_KEY and EXTRA_HEADERS."""
    home = tmp_path / "home"
    package = home / ".cache/uv/archive-v0/abc123/fake_openapi_proxy"
    package.mkdir(parents=True)
    (package / "auth.py").write_text(
        'import os\nkey = os.getenv("API_KEY")\nextra = os.getenv("EXTRA_HEADERS")\nurl = os.getenv("OPENAPI_SPEC_URL")\n'
    )
    monkeypatch.setenv("HOME", str(home))
    return package


def test_a_package_s_reads_come_from_its_installed_source(settings, installed_proxy) -> None:
    reader, reads, _ = EnvContract(settings).all_reads(["uvx", "fake-openapi-proxy@0.4.0"])
    assert reader == "fake-openapi-proxy" and {"API_KEY", "EXTRA_HEADERS"} <= reads


def test_a_credential_under_an_unread_name_is_flagged(ctx, installed_proxy) -> None:
    candidate = Candidate(
        kind="stdio",
        name="build-example-test",
        command=["uvx", "fake-openapi-proxy"],
        env={"OPENAPI_SPEC_URL": "https://build.example.test/api.yaml"},
    )
    candidate.secret_env = {"BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD": "env:X"}
    problems = CandidateVerifier(ctx).unread_credentials(candidate)
    assert problems and problems[0]["name"] == "BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD"
    assert "API_KEY" in problems[0]["remedy"]


def test_a_read_credential_is_not_flagged(ctx, installed_proxy) -> None:
    candidate = Candidate(kind="stdio", name="x", command=["uvx", "fake-openapi-proxy"])
    candidate.secret_env = {"API_KEY": "env:X"}
    assert CandidateVerifier(ctx).unread_credentials(candidate) == []


def test_a_script_reading_its_credential_by_a_computed_name_is_never_flagged(ctx, tmp_path) -> None:
    script = tmp_path / "bridge.py"
    script.write_text(
        'import os, json\nname = json.load(open("m.json"))["var"]\ntoken = os.environ.get(name)\n'
        'base = os.environ.get("MCP_BRIDGE_BASE_URL")\n'
    )
    candidate = Candidate(kind="stdio", name="x", command=[sys.executable, str(script)])
    candidate.secret_env = {"X_TOKEN": "env:X_TOKEN"}
    assert CandidateVerifier(ctx).unread_credentials(candidate) == [], "no literal credential read: nothing proves it"


# -- 5. stand-ins never lock an account ----------------------------------------------------------------


def test_a_stand_in_never_sits_beside_a_real_user_name(ctx) -> None:
    launch = {
        "command": ["x"],
        "env": {"BUILD_EXAMPLE_TEST_USERNAME": "operator", "BASE_URL": "https://b.test"},
        "secret_env": {"BUILD_EXAMPLE_TEST_PASSWORD": "real"},
    }
    verifier = CandidateVerifier(ctx)
    standin = verifier._without_credentials(launch, standin="stand-in")
    assert standin["env"]["BUILD_EXAMPLE_TEST_USERNAME"] != "operator"
    assert standin["secret_env"]["BUILD_EXAMPLE_TEST_PASSWORD"] == "stand-in"
    anonymous = verifier._without_credentials(launch)
    assert "BUILD_EXAMPLE_TEST_USERNAME" not in anonymous["env"] and anonymous["secret_env"] == {}
    assert anonymous["env"]["BASE_URL"] == "https://b.test"


def test_a_credential_refused_twice_is_not_tried_again(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    shake = Handshake(login=login_check.REJECTED, login_credential="fp-1")
    assert verifier.login_budget_left("fp-1")
    verifier.count_refusal(shake)
    verifier.count_refusal(shake)
    assert not verifier.login_budget_left("fp-1") and verifier.login_budget_left("fp-2")


# -- 6. one login is one instance's -------------------------------------------------------------------


def test_the_parts_of_a_basic_credential_are_compared(settings) -> None:
    import base64

    pair = base64.b64encode(b"operator:" + TOKEN.encode()).decode()
    parts = secret_policy.credential_parts("Authorization: Basic " + pair)
    assert TOKEN in parts and secret_policy.credential_fingerprint([TOKEN]) not in repr(parts)


def test_the_same_login_on_two_services_is_not_reported(ctx, sandbox, monkeypatch) -> None:
    import base64

    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    pair = base64.b64encode(b"operator:" + TOKEN.encode()).decode()
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-opensuse-example": {
                        "command": "python3",
                        "args": ["obs.py"],
                        "env": {"BASE_URL": "https://api.opensuse.example", "OBS_PASSWORD": TOKEN},
                    },
                    "build-example-example": {
                        "command": "uvx",
                        "args": ["proxy"],
                        "env": {
                            "SERVER_URL": "https://build.example.example",
                            "EXTRA_HEADERS": "Authorization: Basic " + pair,
                        },
                    },
                }
            }
        )
    )
    ExistingRegistrations.for_run(ctx).registrations()
    findings = [f for f in McpWriter(ctx).shared_secret_findings() if f.get("same_value")]
    # The same user name and password may work on several servers: never a finding.
    assert findings == []


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import env_contract, verifier

    probe = settings.section("mcp.packages.credential_probe")
    assert tuple(probe.str_list("identity_patterns")) == verifier.DEFAULT_IDENTITY_PATTERNS
    assert probe.str_("placeholder_user") == verifier.DEFAULT_PLACEHOLDER_USER
    assert probe.int_("max_refused_logins_per_credential") == verifier.DEFAULT_MAX_REFUSED_LOGINS
    contract = settings.section("mcp.env_contract")
    assert contract.dict_("package_sources") == env_contract.DEFAULT_PACKAGE_SOURCES
    assert tuple(contract.str_list("package_read_patterns")) == env_contract.PACKAGE_READ_PATTERNS
    check = settings.section("mcp.verification.login_check")
    assert check.bool_("compare_with_anonymous") and check.bool_("flag_unread_credentials")
    assert check.dict_("remedies") == login_check.DEFAULT_REMEDIES
    assert settings.bool_("mcp.registration.compare_credential_values") is True
