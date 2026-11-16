"""A substring is not a name, a publisher is not the vendor, and one claim is not seven.

The pattern behind all of it: **a server is a target's only if something about
the target says so** -- where the server is (the target's host or
organisation), what it is configured for (the entry names the target), or what
it says it is (its own answer names the target). A search hit, a registry
listing, an agent's claim and this program's own earlier registration are
claims, and a claim cannot outweigh a server that describes itself as
something else.

The run this pins down registered one entry for seven unrelated addresses:

.. code-block:: json

    "gitlab-example-net": {"type": "http", "url": "https://api.mcp.ai/p_examplep_administradores"}

-- and the same for jira.example.com, confluence.example.com, openqa.example.net,
sd.example.com, build.example.net and github.com. It is a Brazilian insurance
regulator's tool on a third party's platform, returned by an official-registry
search for "example" as ``io.github.mcp-dir/examplep_administradores-mcp``. Four
mistakes let it through, and each has a test here:

1. the package relevance gate found "example" inside "examplep", and "github" inside
   the namespace ``io.github.mcp-dir``;
2. the grounding rule's provenance test did the same, and read the URL's path
   as if it named the organisation;
3. the writer saw one endpoint claimed for seven services and called it a
   duplicate;
4. reuse copied a 44-character random literal into ``.mcp.json`` because the
   variable holding it (``SMELT_EXAMPLE_NET_HEADERS``) is not called a secret.

Both modes share every function tested here: the traditional pipeline and the
agentic one call the same relevance gate, the same grounding rule and the same
writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
from initialize_claude_ai.mcp_auto_discovery.grounding import (
    BASIS_VENDOR,
    HOSTING_NAMESPACES,
    SERVED_ELSEWHERE,
    UNGROUNDED,
    IdentityRule,
    assess,
    gateway_marker,
    hosting_namespaces_of,
    identity_doubt,
    identity_rule_of,
    mentions_host,
    names_organisation,
    names_word,
    points_elsewhere,
    registry_owner,
    text_names,
    vendor_said_so,
)
from initialize_claude_ai.mcp_auto_discovery.registry_client import product_tokens, relevance_of
from initialize_claude_ai.mcp_auto_discovery.secret_policy import looks_opaque
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

#: The entry exactly as the run registered it.
MCP_AI_URL = "https://api.mcp.ai/p_examplep_administradores"
MCP_AI_SOURCE = "MCP registry remote (io.github.mcp-dir/examplep_administradores-mcp)"

#: Every address it was registered for.
VICTIMS = [
    "gitlab.example.net",
    "jira.example.com",
    "confluence.example.com",
    "openqa.example.net",
    "sd.example.com",
    "build.example.net",
    "github.com",
]


def mcp_ai() -> Candidate:
    """The remote as the registry stage built it, after it handshaked."""
    candidate = Candidate(
        kind="url",
        name="examplep_administradores-mcp",
        url=MCP_AI_URL,
        transport=Transport.STREAMABLE_HTTP,
        source=MCP_AI_SOURCE,
        confidence=0.9,
    )
    candidate.handshake = mcp_ai_answer()
    return candidate


def mcp_ai_answer() -> Handshake:
    """What the server really says about itself once it is talking."""
    return Handshake(
        ok=True,
        server_name="mcp-ai",
        server_version="0.9.351",
        instructions=(
            "MCP.AI platform v0.9.351 | Adapters: consultas v1.1.0. NEW MCPs/tools/integrations "
            "come from the official mcp.ai catalog: the `marketplace` tool (action=search) finds "
            "and runs them."
        ),
        tools=[
            "connect",
            "marketplace",
            "report_bug",
            "show_version",
            "examplep_administradores_consultar",
            "toolkit_info",
        ],
        round_trip=True,
    )


def walled(url: str, source: str) -> Candidate:
    candidate = Candidate(kind="url", name="vendor", url=url, transport=Transport.STREAMABLE_HTTP, source=source)
    candidate.handshake = Handshake(
        ok=False, status=401, requires_auth=True, auth_evidence=["it answered application/json"]
    )
    return candidate


# -- 1 and 2: the regression, in both gates --------------------------------------------


@pytest.mark.parametrize("host", VICTIMS)
def test_the_mcp_ai_remote_is_not_relevant_to_any_of_them(host) -> None:
    assert relevance_of(mcp_ai(), product_tokens(host), host) == ""


@pytest.mark.parametrize("host", VICTIMS)
def test_the_mcp_ai_remote_is_refused_for_every_one_of_them(host) -> None:
    verdict = assess(mcp_ai(), f"https://{host}")
    assert verdict.kind == UNGROUNDED, verdict.reason
    assert not verdict.registrable


@pytest.mark.parametrize("host", VICTIMS)
def test_the_vendor_did_not_say_so(host) -> None:
    assert vendor_said_so(mcp_ai(), host) == ""


# -- what must keep passing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "endpoint", "source"),
    [
        ("https://github.com", "https://api.githubcopilot.com/mcp/", "GitHub's own documentation"),
        (
            "https://github.com",
            "https://api.githubcopilot.com/mcp/",
            "MCP registry remote (io.github.github/github-mcp-server)",
        ),
        (
            "https://developers.google.com/knowledge/api",
            "https://developerknowledge.googleapis.com/mcp",
            "MCP registry remote (com.googleapis.developerknowledge/mcp)",
        ),
    ],
)
def test_a_vendor_s_own_endpoint_is_still_believed(target, endpoint, source) -> None:
    verdict = assess(walled(endpoint, source), target)
    assert verdict.kind == SERVED_ELSEWHERE, verdict.reason
    assert verdict.basis == BASIS_VENDOR


def test_github_s_own_registry_entry_is_still_relevant_to_github() -> None:
    candidate = walled("https://api.githubcopilot.com/mcp/", "MCP registry remote (io.github.github/github-mcp-server)")
    assert relevance_of(candidate, product_tokens("github.com"), "github.com")


def test_a_slug_naming_the_product_is_still_relevant() -> None:
    """Whoever published "gitlab-mcp", it is about GitLab: relevance, not provenance."""
    candidate = Candidate(
        kind="stdio",
        name="gitlab-mcp",
        command=["npx", "-y", "gitlab-mcp"],
        source="MCP registry package (npm:gitlab-mcp) from io.github.someone/gitlab-mcp",
    )
    assert relevance_of(candidate, product_tokens("gitlab.example.com"), "gitlab.example.com")


def test_an_account_namespace_is_never_the_hosting_service() -> None:
    """io.github.someone/github-mcp on a stranger's host is not GitHub speaking."""
    candidate = walled("https://mcp.stranger.test/github", "MCP registry remote (io.github.someone/github-mcp)")
    assert vendor_said_so(candidate, "github.com") == ""
    assert assess(candidate, "https://github.com").kind == UNGROUNDED


# -- the helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "text", "expected"),
    [
        ("example", "examplep_administradores", False),
        ("example", "example-mcp", True),
        ("example", "Example's own documentation", True),
        ("example", "mcp_example", True),
        ("github", "githubcopilot", False),
        ("", "anything", False),
    ],
)
def test_names_word(word, text, expected) -> None:
    assert names_word(word, text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://jira.example.com/rest", True),
        ("https://mcp.jira.example.com/mcp", True),
        ("JIRA_URL=jira.example.com", True),
        ("https://jira.example.com.evil.net/", False),
        ("https://notjira.example.com/", False),
        ("https://jira.example.community/", False),
    ],
)
def test_mentions_host_by_label(text, expected) -> None:
    assert mentions_host(text, "jira.example.com") is expected


@pytest.mark.parametrize(
    ("word", "host", "expected"),
    [
        ("github", "docs.github.com", True),
        ("github", "api.githubcopilot.com", True),
        ("google", "developerknowledge.googleapis.com", True),
        ("example", "api.mcp.ai", False),
        ("example", "examplep.mcp.ai", False),
        ("example", "mcp.example.com", True),
    ],
)
def test_names_organisation(word, host, expected) -> None:
    assert names_organisation(word, host) is expected


def test_registry_owner() -> None:
    assert registry_owner("io.github.mcp-dir/examplep_administradores-mcp") == ("account", "mcp-dir")
    assert registry_owner("com.googleapis.developerknowledge/mcp") == (
        "domain",
        "developerknowledge.googleapis.com",
    )
    assert registry_owner("no-namespace") == ("", "")


def test_the_url_path_is_never_a_name() -> None:
    assert not text_names("example", MCP_AI_URL, keep_slugs=False)
    assert not text_names("example", "see https://api.mcp.ai/example for details")


def test_hosting_namespaces_are_configuration(ctx) -> None:
    assert hosting_namespaces_of(ctx.settings) == list(HOSTING_NAMESPACES)
    ctx.settings.set("mcp.packages.hosting_namespaces", ["dev.forge"])
    candidate = walled("https://mcp.stranger.test/x", "MCP registry remote (dev.forge.github/x)")
    # Under the configured list, dev.forge.github is an *account* called github...
    assert registry_owner("dev.forge.github/x", hosting_namespaces_of(ctx.settings)) == ("account", "github")
    assert vendor_said_so(candidate, "github.com", hosting_namespaces=hosting_namespaces_of(ctx.settings))
    # ...and without it, a reversed domain.
    assert registry_owner("dev.forge.github/x", ()) == ("domain", "github.forge.dev")


def test_a_lookalike_host_in_a_launcher_does_not_name_the_target() -> None:
    candidate = Candidate(
        kind="stdio",
        command=["npx", "-y", "mcp-graphql"],
        env={"ENDPOINT": "https://smelt.example.test.evil.net/graphql"},
    )
    assert assess(candidate, "https://smelt.example.test").kind == UNGROUNDED
    assert relevance_of(candidate, [], "smelt.example.test") == ""


# -- 3: one claim for unrelated addresses -----------------------------------------------


def shared(url: str = "https://mcp.platform.example/p_tool") -> Candidate:
    """A remote whose documentation names both alpha and beta -- a claim, not evidence.

    Its answer names both too, so the identity rule lets each claim through on
    its own; what stops it is the same claim turning up twice."""
    candidate = Candidate(
        kind="url",
        name="tool",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        source="listed in the alpha and beta documentation",
    )
    candidate.handshake = Handshake(ok=True, server_name="tool", tools=["alpha_search", "beta_search"], round_trip=True)
    return candidate


@pytest.fixture
def live(ctx, monkeypatch):
    """Writes the file for real; the CLI is reached only through a recording stub."""
    calls: list[list[str]] = []

    class Done:
        ok, simulated, stdout, stderr = True, False, "", ""

    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "claude")
    monkeypatch.setattr(ctx.runner, "run", lambda argv, **k: calls.append(list(argv)) or Done())
    monkeypatch.setattr(McpWriter, "_existing", lambda self, claude: set())
    ctx.dry_run = False
    ctx.calls = calls
    return ctx


def servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_the_same_claim_for_an_unrelated_address_is_refused_and_the_first_withdrawn(live, sandbox) -> None:
    writer = McpWriter(live)
    first = TargetOutcome(target="https://wiki.alpha.com", verified=[shared()])
    [a] = writer.publish(first)
    assert a.written and a.added_by_run
    assert "wiki-alpha-com" in servers(sandbox / ".mcp.json")

    second = TargetOutcome(target="https://tracker.beta.org", verified=[shared()])
    [b] = writer.publish(second)
    assert b.refused and "wiki.alpha.com" in b.refused
    assert "tracker-beta-org" not in servers(sandbox / ".mcp.json")

    # The earlier one is taken back out of the file and the CLI.
    assert a.withdrawn and a.refused and not a.written and not a.registered
    assert "wiki-alpha-com" not in servers(sandbox / ".mcp.json")
    assert any(call[1:4] == ["mcp", "remove", "wiki-alpha-com"] for call in live.calls)
    assert writer.take_withdrawn() == [a]
    assert writer.take_withdrawn() == [], "each withdrawal is handed out once"


def test_a_withdrawal_restores_the_entry_the_file_held_before(live, sandbox) -> None:
    before = {"type": "stdio", "command": "npx", "args": ["-y", "the-operator-s-own"]}
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"wiki-alpha-com": before}}))
    writer = McpWriter(live)
    writer.publish(TargetOutcome(target="https://wiki.alpha.com", verified=[shared()]))
    writer.publish(TargetOutcome(target="https://tracker.beta.org", verified=[shared()]))
    assert servers(sandbox / ".mcp.json")["wiki-alpha-com"] == before


def test_a_name_already_in_the_cli_is_never_removed(live, sandbox, monkeypatch) -> None:
    monkeypatch.setattr(McpWriter, "_existing", lambda self, claude: {"wiki-alpha-com"})
    writer = McpWriter(live)
    [a] = writer.publish(TargetOutcome(target="https://wiki.alpha.com", verified=[shared()]))
    writer.publish(TargetOutcome(target="https://tracker.beta.org", verified=[shared()]))
    assert a.withdrawn and not a.added_by_run
    assert not any("remove" in call for call in live.calls)


def test_addresses_of_one_organisation_may_share_a_claim(live, sandbox) -> None:
    writer = McpWriter(live)
    [a] = writer.publish(TargetOutcome(target="https://wiki.alpha.com", verified=[shared()]))
    [b] = writer.publish(TargetOutcome(target="https://tracker.alpha.com", verified=[shared()]))
    assert not a.refused and not b.refused


def test_an_entry_naming_its_target_is_never_a_shared_claim(live, sandbox) -> None:
    def named(host: str) -> Candidate:
        candidate = Candidate(
            kind="stdio",
            command=["npx", "-y", "mcp-graphql@2.0.4"],
            env={"ENDPOINT": f"https://{host}/graphql"},
        )
        candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
        return candidate

    # The shared-claim guard is the subject, not starting servers: the start
    # check would launch the real `npx mcp-graphql` (network, minutes).
    live.settings.set("mcp.registration.verify_as_written", False)
    writer = McpWriter(live)
    [a] = writer.publish(TargetOutcome(target="https://wiki.alpha.com", verified=[named("wiki.alpha.com")]))
    [b] = writer.publish(TargetOutcome(target="https://tracker.beta.org", verified=[named("tracker.beta.org")]))
    assert not a.refused and not b.refused


def test_the_guard_can_be_turned_off(live, sandbox) -> None:
    live.settings.set("mcp.registration.refuse_shared_claims", False)
    writer = McpWriter(live)
    [a] = writer.publish(TargetOutcome(target="https://wiki.alpha.com", verified=[shared()]))
    [b] = writer.publish(TargetOutcome(target="https://tracker.beta.org", verified=[shared()]))
    assert not a.refused and not b.refused


def test_the_phase_re_judges_an_address_whose_entry_was_withdrawn(live, sandbox) -> None:
    from initialize_claude_ai.core.report import PhaseResult, Status
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    writer = McpWriter(live)
    first = TargetOutcome(target="https://wiki.alpha.com", verified=[shared()], status=Status.OK)
    writer.publish(first)
    writer.publish(TargetOutcome(target="https://tracker.beta.org", verified=[shared()]))
    phase = McpPhase(live)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._settle_withdrawals(result, writer, [first], {first.target: first.target})
    assert first.status == Status.PARTIAL
    assert first.registered == []
    assert "registered none" in first.detail
    assert result.group_verdicts[first.target].status == Status.PARTIAL
    assert any("withdrew wiki-alpha-com" in step.name for step in result.steps)


# -- 4: an opaque literal under a harmless name -----------------------------------------


def opaque() -> str:
    """Random-looking, and computed, so no credential-shaped literal sits in the source."""
    return base64.b64encode(hashlib.sha256(b"fixture").digest()).decode()


def test_an_opaque_literal_is_a_credential_whatever_it_is_called() -> None:
    assert looks_opaque(opaque())


@pytest.mark.parametrize(
    "value",
    [
        "https://smelt.example.test/graphql/",
        "/root/.local/state/initialize-claude-ai/trust/ca-bundle.pem",
        "${NODE_EXTRA_CA_CERTS:-/etc/ssl/ca-bundle.pem}",
        "@zereight/mcp-gitlab@2.1.67",
        "mcp-atlassian==0.23.1",
        "false",
        "Authorization:Bearer ${TOKEN}",
        '{"Authorization": "x"}',
        "node_modules/some-package/dist/index.js",
    ],
)
def test_configuration_is_not_opaque(value) -> None:
    assert not looks_opaque(value)


def test_the_thresholds_are_configuration() -> None:
    assert not looks_opaque(opaque(), min_chars=100)
    # Two tests now: absolute bits per character, or a share of what the
    # value's own alphabet allows. Each is configuration; both must fail.
    assert not looks_opaque(opaque(), min_entropy=7.0, min_ratio=0)
    assert not looks_opaque(opaque(), min_entropy=7.0, min_ratio=2.0)


def test_reuse_never_copies_an_opaque_literal(ctx) -> None:
    secret = opaque()
    (Path.cwd() / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "smelt-example-test": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "mcp-graphql@2.0.4"],
                        "env": {
                            "ENDPOINT": "https://smelt.example.test/graphql/",
                            "SMELT_EXAMPLE_TEST_HEADERS": secret,
                        },
                    },
                }
            }
        )
    )
    [candidate] = ExistingRegistrations(ctx).for_target("smelt.example.test", Stage.PACKAGE)
    assert secret not in json.dumps(candidate.to_dict(), default=str)
    assert candidate.env["ENDPOINT"] == "https://smelt.example.test/graphql/"
    # Named after the entry once, not twice.
    assert candidate.secret_env == {"SMELT_EXAMPLE_TEST_HEADERS": "env:SMELT_EXAMPLE_TEST_HEADERS"}


def test_the_reuse_thresholds_come_from_settings(ctx) -> None:
    ctx.settings.set("mcp.discovery.reuse.opaque_value_min_chars", 1000)
    secret = opaque()
    (Path.cwd() / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "smelt-example-test": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "mcp-graphql@2.0.4"],
                        "env": {"ENDPOINT": "https://smelt.example.test/graphql/", "PAYLOAD": secret},
                    },
                }
            }
        )
    )
    [candidate] = ExistingRegistrations(ctx).for_target("smelt.example.test", Stage.PACKAGE)
    # A name that carries nothing (HEADERS now carries a credential by its
    # shape, see secret_policy.carries_credentials): below the configured
    # length, a random-looking value is configuration.
    assert candidate.env.get("PAYLOAD") == secret, "below the configured length it is configuration"


# -- 5: a claim cannot outweigh what the server says it is -------------------------------


def claimed(answer: Handshake | None, url: str = MCP_AI_URL) -> Candidate:
    """The mcp.ai remote as an agent might propose it: with a claim naming Example."""
    candidate = Candidate(
        kind="url",
        name="example-mcp",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        source="Example's own documentation lists this endpoint",
    )
    candidate.handshake = answer
    return candidate


@pytest.mark.parametrize("host", VICTIMS[:-1])
def test_a_claim_does_not_make_a_catalog_the_targets_server(host) -> None:
    verdict = assess(claimed(mcp_ai_answer()), f"https://{host}")
    assert verdict.kind == UNGROUNDED, verdict.reason
    assert "front for other servers" in verdict.reason
    assert "marketplace" in verdict.reason, "the refusal quotes what the server said"


def test_a_claimed_server_that_never_names_the_target_is_refused() -> None:
    answer = Handshake(ok=True, server_name="weather", tools=["forecast"], round_trip=True)
    verdict = assess(claimed(answer, "https://mcp.stranger.test/w"), "https://gitlab.example.net")
    assert verdict.kind == UNGROUNDED
    assert "never names gitlab.example.net" in verdict.reason


def test_a_claimed_third_party_server_naming_the_product_is_accepted() -> None:
    """A tool called getJiraIssue names Jira: read as words, not as one token."""
    answer = Handshake(ok=True, server_name="bridge", tools=["getJiraIssue"], round_trip=True)
    verdict = assess(claimed(answer, "https://mcp.stranger.test/j"), "https://jira.example.com")
    assert verdict.kind == SERVED_ELSEWHERE, verdict.reason


def test_a_catalog_naming_the_product_is_still_not_its_server() -> None:
    answer = mcp_ai_answer()
    answer.tools.append("gitlab_search")
    verdict = assess(claimed(answer), "https://gitlab.example.net")
    assert verdict.kind == UNGROUNDED
    assert gateway_marker(claimed(answer)) == "marketplace"


def test_an_unanswered_claim_stands_until_configured_otherwise() -> None:
    walled_answer = Handshake(ok=False, status=401, requires_auth=True, auth_evidence=["json"])
    candidate = claimed(walled_answer, "https://mcp.stranger.test/x")
    assert assess(candidate, "https://gitlab.example.net").kind == SERVED_ELSEWHERE
    strict = IdentityRule(accept_unanswered=False)
    assert assess(candidate, "https://gitlab.example.net", identity=strict).kind == UNGROUNDED


def test_the_identity_rule_is_configuration(ctx) -> None:
    default = identity_rule_of(ctx.settings)
    assert default.enabled and default.accept_unanswered
    assert default.gateway_markers == IdentityRule().gateway_markers
    ctx.settings.set("mcp.registration.identity.enabled", False)
    off = identity_rule_of(ctx.settings)
    assert assess(claimed(mcp_ai_answer()), "https://gitlab.example.net", identity=off).kind == SERVED_ELSEWHERE
    ctx.settings.set("mcp.registration.identity.enabled", True)
    ctx.settings.set("mcp.registration.identity.gateway_markers", [])
    answer = mcp_ai_answer()
    answer.tools.append("gitlab_search")
    rule = identity_rule_of(ctx.settings)
    assert assess(claimed(answer), "https://gitlab.example.net", identity=rule).kind == SERVED_ELSEWHERE


def test_a_vendors_own_domain_needs_no_answer_to_name_it() -> None:
    """api.githubcopilot.com is GitHub's: its tools need not say "github"."""
    candidate = walled("https://api.githubcopilot.com/mcp/", "GitHub's own documentation")
    candidate.handshake = Handshake(ok=True, server_name="srv", tools=["get_me"], round_trip=True)
    assert identity_doubt(candidate, "github.com", ["api.githubcopilot.com"]) == ""
    assert assess(candidate, "https://github.com").kind == SERVED_ELSEWHERE


def test_a_launcher_claimed_for_a_third_party_host_needs_the_same() -> None:
    candidate = Candidate(
        kind="stdio",
        name="x",
        command=["npx", "-y", "some-proxy@1.0.0"],
        env={"UPSTREAM": "https://api.mcp.ai/p_examplep_administradores"},
        source="Example's own documentation lists this server",
    )
    candidate.handshake = mcp_ai_answer()
    verdict = assess(candidate, "https://gitlab.example.net")
    assert verdict.kind == UNGROUNDED, verdict.reason


# -- 6: an existing registration proved wrong is retired --------------------------------


def existing(sandbox: Path, servers: dict) -> None:
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def stale_for(ctx, target: str, answer: Handshake | None = None) -> TargetOutcome:
    """Run the shared detection the way both pipelines do, on the project file only."""
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    outcome = TargetOutcome(target=target)
    candidates = ExistingRegistrations.for_run(ctx).for_target(target, Stage.ENDPOINT)
    for candidate in candidates:
        candidate.handshake = answer if answer is not None else mcp_ai_answer()
    TraditionalPipeline(ctx).note_stale(candidates, outcome)
    return outcome


BAD = {"type": "http", "url": MCP_AI_URL}
GOOD = {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "mcp-graphql@2.0.4"],
    "env": {"ENDPOINT": "https://smelt.example.test/graphql/"},
}


def test_a_registration_proved_wrong_is_retired_from_the_file(live, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD, "smelt-example-test": GOOD})
    outcome = stale_for(live, "https://gitlab.example.com")
    assert [s["name"] for s in outcome.stale] == ["gitlab-example-com"]
    assert outcome.to_dict()["stale_registrations"][0]["source"] == "project_file"

    [done] = McpWriter(live).retire(outcome)
    assert "removed from" in done["action"]
    assert servers(sandbox / ".mcp.json") == {"smelt-example-test": GOOD}
    assert ExistingRegistrations.for_run(live).for_target("https://gitlab.example.com", Stage.ENDPOINT) == [], (
        "a retired registration is not offered again"
    )
    assert McpWriter(live).retire(outcome) == [], "each is acted on once"


def test_its_cli_twin_goes_with_it(live, sandbox, monkeypatch) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    monkeypatch.setattr(McpWriter, "_existing", lambda self, claude: {"gitlab-example-com"})
    monkeypatch.setattr(ExistingRegistrations, "cli_entry", lambda self, name: dict(BAD))
    # A CLI copy lives in a CLI scope; the remove names it -- a bare remove
    # would take the project file's entry instead.
    monkeypatch.setattr(McpWriter, "cli_scopes", lambda self, name: ["local"])
    [done] = McpWriter(live).retire(outcome)
    assert "removed from the claude CLI" in done["action"]
    assert any(call[1:6] == ["mcp", "remove", "gitlab-example-com", "--scope", "local"] for call in live.calls)


def test_a_cli_entry_of_another_service_is_not_its_twin(live, sandbox, monkeypatch) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    monkeypatch.setattr(McpWriter, "_existing", lambda self, claude: {"gitlab-example-com"})
    monkeypatch.setattr(ExistingRegistrations, "cli_entry", lambda self, name: dict(GOOD))
    McpWriter(live).retire(outcome)
    assert not any("remove" in call for call in live.calls)


def test_a_cli_registration_is_removed_through_the_cli(live, sandbox) -> None:
    outcome = TargetOutcome(target="https://gitlab.example.com")
    outcome.stale.append({"name": "gitlab-example-com", "source": "claude_cli", "reason": "r"})
    [done] = McpWriter(live).retire(outcome)
    assert done["action"] == "removed from the claude CLI"


def test_a_name_that_is_not_ours_is_reported_not_removed(live, sandbox) -> None:
    existing(sandbox, {"my-gitlab": BAD})
    outcome = TargetOutcome(target="https://gitlab.example.com")
    outcome.stale.append({"name": "my-gitlab", "source": "project_file", "reason": "r"})
    [done] = McpWriter(live).retire(outcome)
    assert done["action"] == "reported, not removed"
    assert "delete 'my-gitlab'" in done["remediation"]
    assert "my-gitlab" in servers(sandbox / ".mcp.json")


def test_retirement_can_be_turned_off(live, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    live.settings.set("mcp.registration.retire_refused_existing", False)
    outcome = stale_for(live, "https://gitlab.example.com")
    [done] = McpWriter(live).retire(outcome)
    assert done["action"] == "reported, not removed"
    assert "gitlab-example-com" in servers(sandbox / ".mcp.json")


def test_an_entry_changed_since_it_was_read_is_left_alone(live, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    existing(sandbox, {"gitlab-example-com": {"type": "http", "url": "https://gitlab.example.com/mcp"}})
    [done] = McpWriter(live).retire(outcome)
    assert done["action"] == "reported, not removed"
    assert "changed after this run read it" in done["remediation"]


def test_a_dry_run_only_says_what_it_would_retire(ctx, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(ctx, "https://gitlab.example.com")
    [done] = McpWriter(ctx).retire(outcome)
    assert "would be removed" in done["action"]
    assert "gitlab-example-com" in servers(sandbox / ".mcp.json")


@pytest.mark.parametrize(
    ("entry", "answer", "why"),
    [
        (BAD, Handshake(ok=False, error="connection refused"), "an entry that is down is not wrong"),
        (BAD, Handshake(ok=True, tools=["gitlab_search"], round_trip=True), "its answer names it"),
        ({"type": "http", "url": "https://mcp.example.com/p"}, None, "the target's organisation"),
    ],
)
def test_only_an_entry_proved_wrong_is_stale(ctx, sandbox, entry, answer, why) -> None:
    existing(sandbox, {"gitlab-example-com": entry})
    assert stale_for(ctx, "https://gitlab.example.com", answer).stale == [], why


def test_points_elsewhere_needs_an_address() -> None:
    bare = Candidate(kind="stdio", command=["npx", "-y", "some-server@1.0.0"])
    assert not points_elsewhere(bare, "https://gitlab.example.com")
    assert points_elsewhere(mcp_ai(), "https://gitlab.example.com")


def test_the_phase_reports_each_retirement(live, sandbox) -> None:
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    phase = McpPhase(live)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._retire_stale(result, McpWriter(live), outcome)
    [step] = [s for s in result.steps if "retire gitlab-example-com" in s.name]
    assert "not this address's server" in step.detail


def test_the_agent_is_told_an_existing_entry_is_not_this_addresss(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    existing(sandbox, {"gitlab-example-com": BAD})
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    pipeline = AgenticPipeline(ctx)
    target = TargetOutcome(target="https://gitlab.example.com")
    tried = ExistingRegistrations.for_run(ctx).for_target(target.target, Stage.ENDPOINT)
    for candidate in tried:
        candidate.handshake = mcp_ai_answer()
    pipeline.fallback._reused[(target.target, Stage.ENDPOINT)] = tried
    pipeline.fallback.note_stale(tried, target)

    brief = pipeline._existing_brief(Stage.ENDPOINT, target)
    assert "gitlab-example-com" in brief and "NOT THIS ADDRESS'S" in brief
    assert "Do not propose it" in brief


# -- 7. retirement reports what it did, and nothing it did not -----------------


def test_a_dry_run_keeps_the_entry_known_and_says_what_a_real_run_would_do(ctx, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(ctx, "https://gitlab.example.com")
    [done] = McpWriter(ctx).retire(outcome)
    assert done["result"] == "would be removed"
    assert "claude CLI too" in done["action"]
    assert "a real run removes it" in done["remediation"]
    names = {r.name for r in ExistingRegistrations.for_run(ctx).registrations()}
    assert "gitlab-example-com" in names, "a dry run must not change what later targets see"


def test_a_real_retirement_says_removed(live, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    [done] = McpWriter(live).retire(outcome)
    assert done["result"] == "removed"
    assert "was retired" in done["remediation"]


def test_an_entry_left_in_place_never_claims_it_was_retired(live, sandbox) -> None:
    existing(sandbox, {"my-gitlab": BAD})
    outcome = TargetOutcome(target="https://gitlab.example.com")
    outcome.stale.append({"name": "my-gitlab", "source": "project_file", "reason": "r"})
    [done] = McpWriter(live).retire(outcome)
    assert done["result"] == "kept"
    assert "retired" not in done["remediation"]


def test_a_name_another_address_in_the_run_gives_is_not_ours(live, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com-eu": BAD})
    outcome = TargetOutcome(target="https://gitlab.example.com")
    outcome.stale.append({"name": "gitlab-example-com-eu", "source": "project_file", "reason": "r"})
    [done] = McpWriter(live).retire(outcome, ["https://gitlab.example.com", "https://gitlab.example.com.eu"])
    assert done["result"] == "kept"
    assert "gitlab-example-com-eu" in servers(sandbox / ".mcp.json")


def test_what_the_run_found_later_is_weighed_before_removing(live, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.grounding import GROUNDED, Grounding

    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(live, "https://gitlab.example.com")
    monkeypatch.setattr(McpWriter, "grounding_of", lambda self, *a, **k: Grounding(GROUNDED, "found"))
    assert McpWriter(live).retire(outcome) == []
    assert outcome.stale == []
    assert "gitlab-example-com" in servers(sandbox / ".mcp.json")


def test_a_stale_row_names_hosts_never_the_command_line(ctx, sandbox) -> None:
    existing(sandbox, {"gitlab-example-com": BAD})
    [row] = stale_for(ctx, "https://gitlab.example.com").stale
    assert row["connects_to"] == "api.mcp.ai"


@pytest.mark.parametrize(("fixture", "expected"), [("ctx", "ok"), ("live", "changed")])
def test_the_phase_statuses_follow_what_was_done(request, sandbox, fixture, expected) -> None:
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    run = request.getfixturevalue(fixture)
    existing(sandbox, {"gitlab-example-com": BAD})
    outcome = stale_for(run, "https://gitlab.example.com")
    phase = McpPhase(run)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._retire_stale(result, McpWriter(run), outcome)
    [step] = [s for s in result.steps if s.name.endswith("retire gitlab-example-com")]
    assert step.status == expected


def test_reuse_matches_a_host_by_whole_labels(ctx, sandbox) -> None:
    existing(
        sandbox,
        {
            "lookalike": {"type": "http", "url": "https://gitlab.example.com.evil.net/mcp"},
            "prefixed": {"type": "http", "url": "https://notgitlab.example.com/mcp"},
            "real": {"type": "http", "url": "https://gitlab.example.com/mcp"},
        },
    )
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    found = ExistingRegistrations.for_run(ctx).for_target("https://gitlab.example.com", Stage.ENDPOINT)
    assert [c.url for c in found] == ["https://gitlab.example.com/mcp"]
