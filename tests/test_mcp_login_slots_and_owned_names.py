"""Every server keeps a way to log in, and every entry reads names only it reads.

gitlab-example-net (graphql-mcp-server) and bugzilla-opensuse-org (mcp-bugzilla)
were written with no credential slot at all: their candidates declared none,
both servers answered anybody, and nothing asked what they read a login from --
their own code said (API_ACCESS_TOKEN, BUGZILLA_API_KEY). bugzilla-example-com read
``${BUGZILLA_API_KEY:-…}``: a shell's revoked BUGZILLA_API_KEY won over the key
given for the entry, and ``${SSL_CERT_FILE:-…}`` lost to a login shell's deleted
/tmp bundle. And a GraphQL answer of ``{"currentUser": null}`` counted as a
login accepted.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport


def _script(tmp_path, body: str):
    path = tmp_path / "server.py"
    path.write_text("import os\n" + body)
    return path


def _verifier(ctx):
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    return CandidateVerifier(ctx)


def _writer(ctx):
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    return McpWriter(ctx)


# -- fix 1: the server's own credential variable becomes its slot -----------------------------


def test_a_server_that_reads_a_login_is_given_its_slot(ctx, tmp_path) -> None:
    script = _script(tmp_path, 'TOKEN = os.environ.get("API_ACCESS_TOKEN")\n')
    candidate = Candidate(
        kind="stdio",
        name="graphql",
        command=[sys.executable, str(script)],
        env={"GRAPHQL_ENDPOINT": "https://gitlab.example.test/api/graphql"},
    )
    added = _verifier(ctx).slot_server_credential(candidate)
    assert added == ["API_ACCESS_TOKEN"] and candidate.secret_env == {"API_ACCESS_TOKEN": "env:API_ACCESS_TOKEN"}
    assert any("its server's own code reads its login" in n for n in candidate.notes)


def test_among_several_the_one_naming_the_product_is_its_login(ctx, tmp_path) -> None:
    script = _script(
        tmp_path,
        'A = os.environ.get("BUGZILLA_API_KEY")\nB = os.environ.get("OTHER_SERVICE_TOKEN")\n'
        'C = os.environ.get("MCP_AUTH_HEADER")\nD = os.environ.get("BUGZILLA_AUTH_MODE")\n',
    )
    candidate = Candidate(
        kind="stdio",
        name="bz",
        command=[sys.executable, str(script)],
        env={"BUGZILLA_SERVER": "https://bugzilla.example.org"},
    )
    assert _verifier(ctx).slot_server_credential(candidate) == ["BUGZILLA_API_KEY"]


def test_a_choice_of_logins_is_reported_never_guessed(ctx, tmp_path) -> None:
    script = _script(tmp_path, 'A = os.environ.get("JIRA_API_TOKEN")\nB = os.environ.get("JIRA_PASSWORD")\n')
    candidate = Candidate(
        kind="stdio", name="jira", command=[sys.executable, str(script)], env={"JIRA_URL": "https://jira.example.test"}
    )
    assert _verifier(ctx).slot_server_credential(candidate) == [] and not candidate.secret_env
    assert any("which one is yours to say" in n for n in candidate.notes)


def test_a_candidate_that_can_send_a_login_is_left_alone(ctx, tmp_path) -> None:
    script = _script(tmp_path, 'TOKEN = os.environ.get("API_ACCESS_TOKEN")\n')
    declared = Candidate(
        kind="stdio", name="x", command=[sys.executable, str(script)], secret_env={"MY_TOKEN": "env:MY_TOKEN"}
    )
    carried = Candidate(
        kind="stdio",
        name="y",
        command=[sys.executable, str(script)],
        env={"HEADERS": '{"Authorization": "Bearer ${EXAMPLE_TEST_TOKEN}"}'},
    )
    verifier = _verifier(ctx)
    assert verifier.slot_server_credential(declared) == [] and verifier.slot_server_credential(carried) == []


def test_the_switch_turns_it_off(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.verification.server_credential.enabled", False)
    script = _script(tmp_path, 'TOKEN = os.environ.get("API_ACCESS_TOKEN")\n')
    candidate = Candidate(kind="stdio", name="x", command=[sys.executable, str(script)])
    assert _verifier(ctx).slot_server_credential(candidate) == []


def test_the_slot_is_written_optional_under_the_entry_s_own_name(ctx, tmp_path) -> None:
    """Proved to work without it, the slot stays: ${NAME:-}, never dropped."""
    script = _script(tmp_path, 'TOKEN = os.environ.get("API_ACCESS_TOKEN")\n')
    candidate = Candidate(
        kind="stdio",
        name="graphql",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"GRAPHQL_ENDPOINT": "https://gitlab.example.test/api/graphql"},
    )
    _verifier(ctx).slot_server_credential(candidate)
    candidate.handshake = Handshake(
        ok=True, transport=Transport.STDIO, tools=["q"], round_trip=True, credential_optional=["API_ACCESS_TOKEN"]
    )
    entry = _writer(ctx).entry_for(candidate, "gitlab-example-test")
    assert entry["env"]["API_ACCESS_TOKEN"] == "${GITLAB_EXAMPLE_TEST_API_ACCESS_TOKEN:-}"


def test_the_same_package_s_other_entry_names_the_slot_when_the_source_cannot(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import record_package_credentials, sibling_credential

    record_package_credentials(
        ctx,
        {
            "command": "uvx",
            "args": ["some-bugzilla-mcp==1.0", "--server", "https://a"],
            "env": {"BUGZILLA_API_KEY": "${A_BUGZILLA_API_KEY:-}", "BUGZILLA_AUTH_MODE": "q"},
        },
    )
    assert sibling_credential(ctx, ["uvx", "some-bugzilla-mcp==2.0", "--server", "https://b"]) == "BUGZILLA_API_KEY"
    assert sibling_credential(ctx, ["uvx", "another-package"]) == ""


def test_a_package_credential_is_withheld_from_the_trial(ctx, tmp_path, monkeypatch) -> None:
    """A shell exporting a revoked BUGZILLA_API_KEY for something else must not decide the trial."""
    from initialize_claude_ai.mcp_auto_discovery import env_contract

    monkeypatch.setattr(
        env_contract.EnvContract, "all_reads", lambda self, argv: ("pkg", {"BUGZILLA_API_KEY", "BUGZILLA_SERVER"}, [])
    )
    candidate = Candidate(kind="stdio", name="bz", command=["uvx", "pkg"])
    withheld = _verifier(ctx)._withheld(candidate, ["uvx", "pkg"], {}, {}, [], {})
    assert "BUGZILLA_API_KEY" in withheld and "BUGZILLA_SERVER" not in withheld


# -- fix 3: names only the entry reads -----------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "server", "host", "expected"),
    [
        (
            "BUGZILLA_API_KEY",
            "bugzilla-opensuse-org",
            "bugzilla.opensuse.org",
            "BUGZILLA_OPENSUSE_ORG_BUGZILLA_API_KEY",
        ),
        ("BUGZILLA_API_KEY", "bugzilla-example-com", "bugzilla.example.com", "BUGZILLA_EXAMPLE_COM_BUGZILLA_API_KEY"),
        ("GITHUB_PERSONAL_ACCESS_TOKEN", "github-com", "github.com", "GITHUB_PERSONAL_ACCESS_TOKEN"),
        (
            "BUILD_EXAMPLE_MCP_BRIDGE_AUTH_VALUE",
            "build-example-de",
            "build.example.de",
            "BUILD_EXAMPLE_MCP_BRIDGE_AUTH_VALUE",
        ),
        ("SD_EXAMPLE_COM_TOKEN", "sd-example-com", "sd.example.com", "SD_EXAMPLE_COM_TOKEN"),
        ("SSH_AUTH_SOCK", "build-example-net", "build.example.net", "SSH_AUTH_SOCK"),
        ("ICA_MCP_UVX", "bugzilla-example-com", "bugzilla.example.com", "ICA_MCP_UVX"),
        ("JIRA_PERSONAL_TOKEN", "jira-example-com", "jira.example.com", "JIRA_EXAMPLE_COM_JIRA_PERSONAL_TOKEN"),
    ],
)
def test_a_reference_reads_a_name_only_its_entry_reads(ctx, variable, server, host, expected) -> None:
    assert _writer(ctx).owned_reference(variable, server, host=host) == expected


def test_this_machine_s_own_bundle_reads_the_per_machine_trust_name(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.core import trust_store

    bundle = tmp_path / "system.pem"
    bundle.write_text("x")
    monkeypatch.setattr(trust_store, "system_bundle", lambda s: str(bundle))
    writer = _writer(ctx)
    assert writer.owned_reference("SSL_CERT_FILE", "bugzilla-example-com", str(bundle)) == "ICA_MCP_CA_BUNDLE"
    assert writer.owned_reference("NODE_EXTRA_CA_CERTS", "x-example-test") == "ICA_MCP_CA_BUNDLE", (
        "no bundle: the lever"
    )
    assert (
        writer.owned_reference("SSL_CERT_FILE", "x-example-test", "/opt/our-ca.pem") == "X_EXAMPLE_TEST_SSL_CERT_FILE"
    )
    assert writer.program_variables()["ICA_MCP_CA_BUNDLE"] == str(bundle), "phase 1 can set it on every machine"


def test_the_switch_writes_the_documented_names(ctx) -> None:
    ctx.settings.set("mcp.registration.own_reference_names", False)
    assert _writer(ctx).owned_reference("BUGZILLA_API_KEY", "bugzilla-example-com") == "BUGZILLA_API_KEY"


def test_a_login_claude_code_holds_under_the_old_name_is_kept_under_the_new(ctx) -> None:
    ctx.claude_home.mkdir(parents=True, exist_ok=True)
    (ctx.claude_home / "settings.json").write_text(json.dumps({"env": {"JIRA_PERSONAL_TOKEN": "a-token-value-1234"}}))
    candidate = Candidate(
        kind="stdio",
        name="jira",
        command=["uvx", "mcp-atlassian"],
        env={"JIRA_URL": "https://jira.example.test"},
        references={"JIRA_PERSONAL_TOKEN": "${JIRA_PERSONAL_TOKEN}"},
    )
    candidate.env["JIRA_PERSONAL_TOKEN"] = "x"
    entry = _writer(ctx).entry_for(candidate, "jira-example-test")
    assert entry["env"]["JIRA_PERSONAL_TOKEN"] == "${JIRA_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN}"
    assert candidate.kept_out["JIRA_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN"].reveal() == "a-token-value-1234"


def test_names_the_operator_already_exports_for_the_entry_are_kept(ctx) -> None:
    candidate = Candidate(kind="stdio", name="proxy", command=["uvx", "mcp-openapi-proxy"])
    candidate.secret_env = {"EXTRA_HEADERS": "env:X"}
    candidate.composed = {"EXTRA_HEADERS": "COMPOSE:Authorization:Basic {b64:B_USERNAME:B_PASSWORD}"}
    entry = _writer(ctx).entry_for(candidate, "build-example-test")
    assert entry["env"]["B_USERNAME"] == "${B_USERNAME}" and entry["env"]["B_PASSWORD"] == "${B_PASSWORD}"


# -- fix 4: answering as nobody is not a login ------------------------------------------------------


def test_a_null_identity_is_an_empty_answer_never_a_login_accepted(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check

    reply = {"result": {"content": [{"type": "text", "text": '{"data": {"currentUser": null, "project": null}}'}]}}
    verdict, detail = login_check.judge(reply, ctx.settings)
    assert verdict == login_check.EMPTY and "answered as nobody" in detail
    named = {"result": {"content": [{"type": "text", "text": '{"data": {"currentUser": {"username": "someone"}}}'}]}}
    assert login_check.judge(named, ctx.settings)[0] == login_check.ACCEPTED


def test_working_for_anybody_is_said_as_anonymous(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    candidate = Candidate(
        kind="stdio",
        name="bz",
        transport=Transport.STDIO,
        command=["uvx", "mcp-bugzilla"],
        env={"BUGZILLA_SERVER": "https://bugzilla.example.org"},
        secret_env={"BUGZILLA_API_KEY": "env:BUGZILLA_API_KEY"},
    )
    candidate.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        tools=["bug_info"],
        round_trip=True,
        credential_optional=["BUGZILLA_API_KEY"],
    )
    writer = _writer(ctx)
    [registration] = writer.publish(TargetOutcome(target="https://bugzilla.example.org", verified=[candidate]))
    assert registration.anonymous_until == ["BUGZILLA_EXAMPLE_ORG_BUGZILLA_API_KEY"]
    assert "public answers only -- not logged in" in registration.detail
    block = writer.briefing([registration])
    assert registration.name in json.dumps(block.get("these_answer_anonymously_until_given_a_login", {}))


# -- fix 8: the server's own log names the cause ----------------------------------------------------


def test_a_missing_trust_file_in_the_server_s_log_is_a_trust_cause(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import stderr_cause

    lines = [
        "warning: Ignoring invalid `SSL_CERT_FILE`. File does not exist: /tmp/tmp7qq96ypw/ca-bundle.pem.",
        "[INFO]: Starting Bugzilla MCP server on stdio",
        "FileNotFoundError: [Errno 2] No such file or directory",
    ]
    assert stderr_cause(lines, ctx.settings)["class"] == "trust"
    assert stderr_cause(["httpx.ConnectError: [Errno 111] Connection refused"], ctx.settings)["class"] == "transport"
    assert stderr_cause(["all is well"], ctx.settings) == {}


def test_a_trust_cause_is_repaired_with_this_machine_s_bundle(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.core import trust_store
    from initialize_claude_ai.mcp_auto_discovery import repair

    system = tmp_path / "system.pem"
    system.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(trust_store, "system_bundle", lambda s: str(system))
    candidate = Candidate(
        kind="stdio",
        name="bz",
        command=["uvx", "mcp-bugzilla"],
        env={"SSL_CERT_FILE": "/tmp/gone-for-good/ca-bundle.pem"},
    )
    candidate.handshake = Handshake(ok=True, stderr_cause={"class": "trust", "line": "SSL_CERT_FILE does not exist"})
    assert repair.classify(candidate)[0] == "trust_file"
    [variant] = repair.use_system_bundle(candidate, {"settings": ctx.settings})
    assert variant.env["SSL_CERT_FILE"] == str(system)


def test_a_header_list_carrier_is_never_made_a_bare_slot(ctx, tmp_path) -> None:
    """HEADERS holds a JSON form; empty, mcp-graphql crashed -- its form is the adapter's to write."""
    script = _script(tmp_path, 'H = os.environ.get("HEADERS")\n')
    candidate = Candidate(
        kind="stdio",
        name="graphql",
        command=[sys.executable, str(script)],
        env={"ENDPOINT": "https://smelt.example.test/graphql/"},
    )
    assert _verifier(ctx).slot_server_credential(candidate) == [] and not candidate.secret_env


def test_an_optional_login_slot_costs_no_rank(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.ranking import CandidateRanker

    plain = Candidate(
        kind="stdio", name="a", command=["uvx", "srv"], handshake=Handshake(ok=True, tools=["t"], round_trip=True)
    )
    slotted = Candidate(
        kind="stdio",
        name="b",
        command=["uvx", "srv"],
        secret_env={"API_TOKEN": "env:API_TOKEN"},
        handshake=Handshake(ok=True, tools=["t"], round_trip=True, credential_optional=["API_TOKEN"]),
    )
    ranker = CandidateRanker(ctx)
    assert ranker._configuration(slotted)[0] == ranker._configuration(plain)[0]
    required = Candidate(
        kind="stdio",
        name="c",
        command=["uvx", "srv"],
        secret_env={"API_TOKEN": "env:API_TOKEN"},
        handshake=Handshake(ok=True, tools=["t"], round_trip=True),
    )
    assert ranker._configuration(required)[0] < ranker._configuration(plain)[0], "a required one still costs"


def test_a_trial_reads_the_owned_name_never_a_generic_shell_export(ctx, monkeypatch) -> None:
    """bugzilla-example-com's trial sent the run's exported BUGZILLA_API_KEY -- a key for something else."""
    monkeypatch.setenv("BUGZILLA_API_KEY", "a-generic-key-exported-for-something-else")
    candidate = Candidate(
        kind="stdio",
        name="bz",
        command=["uvx", "mcp-bugzilla"],
        env={"BUGZILLA_SERVER": "https://bugzilla.example.org"},
    )
    verifier = _verifier(ctx)
    assert verifier.resolve_owned(candidate, "env:BUGZILLA_API_KEY", label="t", key="BUGZILLA_API_KEY") is None
    monkeypatch.setenv("BUGZILLA_EXAMPLE_ORG_BUGZILLA_API_KEY", "the-key-given-for-this-instance")
    ctx.secrets._cache.clear()  # the resolver remembers per run; this is a second situation
    assert (
        verifier.resolve_owned(candidate, "env:BUGZILLA_API_KEY", label="t2", key="BUGZILLA_API_KEY").reveal()
        == "the-key-given-for-this-instance"
    )
    chosen = verifier.resolve_owned(candidate, "env:BUGZILLA_API_KEY", label="t3", key="EXTRA_HEADERS")
    assert chosen is not None, "a name chosen for another key is read as written"


def test_a_service_about_a_product_on_a_third_party_host_is_not_its_server() -> None:
    """githubcard.com names GitHub as any service about it would; it is not github.com's server."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import IdentityRule, identity_doubt

    card = Candidate(
        kind="url",
        name="mcp",
        url="https://githubcard.com/mcp",
        source="MCP registry remote (com.githubcard/mcp)",
        handshake=Handshake(
            ok=True,
            server_name="githubcard",
            tools=["create_card", "list_templates"],
            instructions="Card maker: turns a github user or repository into a card.",
        ),
    )
    doubt = identity_doubt(card, "github.com", ["githubcard.com"], IdentityRule())
    assert doubt.startswith("githubcard.com belongs to neither github.com nor its organisation"), doubt
    assert "never names github.com itself" in doubt and "'github'" in doubt, "a service about it names it too"
    assert identity_doubt(card, "github.com", ["githubcard.com"], IdentityRule(third_party_needs_host=False)) == ""
    own = Candidate(
        kind="url",
        name="mcp",
        url="https://mcp.thirdparty.test/mcp",
        handshake=Handshake(
            ok=True,
            server_name="x",
            tools=["get_issue"],
            advertised=["resource https://github.com/{owner}/{repo}/issues"],
        ),
    )
    assert identity_doubt(own, "github.com", ["mcp.thirdparty.test"], IdentityRule()) == "", (
        "naming the address is a tie"
    )
    vouched = Candidate(
        kind="url",
        name="mcp",
        url="https://githubcard.com/mcp",
        source="GitHub's own documentation lists this endpoint",
        handshake=card.handshake,
    )
    assert identity_doubt(vouched, "github.com", ["githubcard.com"], IdentityRule()) == "", (
        "the vendor speaking is a tie"
    )


def test_a_trust_file_from_another_machine_is_a_cause_to_repair(ctx) -> None:
    """graphql-mcp-server listed nothing: its SSL_CERT_FILE named the other machine's bundle."""
    from initialize_claude_ai.mcp_auto_discovery import repair

    candidate = Candidate(
        kind="stdio",
        name="gl",
        command=["uvx", "graphql-mcp-server"],
        env={"SSL_CERT_FILE": "/home/elsewhere/mcp-servers/gitlab/ca-bundle.pem"},
    )
    candidate.handshake = Handshake(ok=True, error="server exposes no tools, resources or prompts")
    assert repair.classify(candidate)[0] == "trust_file"


def test_a_server_logging_in_to_another_organisation_is_not_the_target_s() -> None:
    """An Apify scraper about GitHub named github.com in its tools and waited for APIFY_TOKEN."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess, foreign_login

    scanner = Candidate(
        kind="stdio",
        name="scanner",
        command=["npx"],
        args=["-y", "@x/mcp-github-org-scanner@1"],
        secret_env={"APIFY_TOKEN": "env:APIFY_TOKEN"},
        handshake=Handshake(ok=True, tools=["scan"], advertised=["resource https://github.com/{org}"]),
    )
    verdict = assess(scanner, "https://github.com")
    assert verdict.kind == UNGROUNDED and "logs in to apify" in verdict.reason
    own = Candidate(
        kind="stdio",
        name="gh",
        command=["npx"],
        args=["-y", "srv"],
        secret_env={"GITHUB_PERSONAL_ACCESS_TOKEN": "env:X"},
    )
    nobody = Candidate(kind="stdio", name="gq", command=["uvx", "srv"], secret_env={"API_ACCESS_TOKEN": "env:X"})
    assert foreign_login(own, "github.com") == "" and foreign_login(nobody, "gitlab.example.net") == ""
