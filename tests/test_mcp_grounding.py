"""An entry has to say which server it talks to.

The operator's question about any line of ``.mcp.json`` is "is this pointed at
my server?". Three real entries could not answer it, each for a different
reason, and all three were registered:

    "confluence-example-com": npx -y @kubb/mcp
        a TypeScript code generator. Nothing in the entry mentions
        confluence.example.com and nothing in it could reach it.

    "confluence-example-com": https://confluence.example.com
        the right host and still wrong: the site's front door, which answers
        401 to an anonymous POST because it is a login page. It got there
        because the probe URL carried ?format=mcp -- a query this program
        invented -- which was then tidied away, leaving a URL never tested.

    "smelt-example-net-...": npx -y mcp-graphql, ENDPOINT=https://smelt.example.net/graphql/
        the good case: a generic server whose configuration names the target.

See :mod:`initialize_claude_ai.mcp_auto_discovery.grounding`.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.mcp_auto_discovery.grounding import (
    GROUNDED,
    SERVED_ELSEWHERE,
    UNGROUNDED,
    addresses_in,
    assess,
    same_service,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

WORKS = Handshake(ok=True, server_name="Example", tools=["a"], round_trip=True)


def stdio(command: list[str], env: dict[str, str] | None = None, name: str = "srv") -> Candidate:
    return Candidate(kind="stdio", name=name, transport=Transport.STDIO, command=command, env=env or {})


def url(address: str, *, verified: bool = False, source: str = "") -> Candidate:
    candidate = Candidate(kind="url", name="srv", url=address, transport=Transport.STREAMABLE_HTTP, source=source)
    if verified:
        candidate.handshake = WORKS
    return candidate


# --------------------------------------------------------------------------- #
# the launcher that never mentions the target
# --------------------------------------------------------------------------- #


def test_a_code_generator_is_not_grounded_in_confluence() -> None:
    """The real entry. `@kubb/mcp` handshakes -- it is simply someone else's server."""
    verdict = assess(stdio(["npx", "-y", "@kubb/mcp"]), "https://confluence.example.com")
    assert verdict.kind == UNGROUNDED
    assert verdict.registrable is False
    assert "confluence.example.com" in verdict.reason


def test_a_generic_server_configured_for_the_target_is_grounded() -> None:
    """The good case: the package says nothing about SMELT and does not need to."""
    candidate = stdio(["npx", "-y", "mcp-graphql@2.0.4"], {"ENDPOINT": "https://smelt.example.net/graphql/"})
    verdict = assess(candidate, "https://smelt.example.net")
    assert verdict.kind == GROUNDED
    assert verdict.evidence == "env ENDPOINT"


def test_the_address_may_be_an_argument_rather_than_an_environment_value() -> None:
    candidate = stdio(["uvx", "some-mcp", "--base-url", "https://openqa.opensuse.org"])
    verdict = assess(candidate, "https://openqa.opensuse.org")
    assert verdict.kind == GROUNDED
    assert verdict.evidence == "the command"


def test_a_generated_bridge_is_grounded_by_the_url_it_relays_to() -> None:
    """BridgeBuilder always sets MCP_BRIDGE_BASE_URL, which is what saves it."""
    candidate = stdio(
        ["/usr/bin/python3", "/opt/bridge.py"],
        {"MCP_BRIDGE_BASE_URL": "https://smelt.example.net", "MCP_BRIDGE_TIMEOUT": "30"},
    )
    assert assess(candidate, "https://smelt.example.net").kind == GROUNDED


# --------------------------------------------------------------------------- #
# the one honest exception: the host *is* the product
# --------------------------------------------------------------------------- #


def test_a_launcher_with_no_address_at_all_is_refused_however_apt_its_name() -> None:
    """Even a vendor's own package for the vendor's own host.

    An earlier version let this through: `github.com` reduces to the single
    label `github`, the launcher names `github`, so the address was treated as
    implied. That was a floor with a hole in it -- the entry still contained no
    address, so there was nothing for anybody to check. Setting the host
    variable is one line and makes the entry self-describing.
    """
    verdict = assess(stdio(["npx", "-y", "@modelcontextprotocol/server-github"]), "https://github.com")
    assert verdict.kind == UNGROUNDED
    assert "no address at all" in verdict.reason


def test_the_same_launcher_told_its_host_is_believed() -> None:
    candidate = stdio(
        ["npx", "-y", "@modelcontextprotocol/server-github"],
        {"GITHUB_HOST": "https://github.com"},
    )
    assert assess(candidate, "https://github.com").kind == GROUNDED


def test_a_private_instance_of_a_product_must_name_itself() -> None:
    """confluence.example.com is somebody's Confluence, not Confluence."""
    verdict = assess(stdio(["npx", "-y", "confluence-datacenter-mcp"]), "https://confluence.example.com")
    assert verdict.kind == UNGROUNDED, "a package for Confluence is not a package for *this* one"


def test_a_launcher_pointed_at_some_other_host_is_refused() -> None:
    """It has an address; it is not this service's, and nothing probed it.

    The asymmetry with a URL is the point: a URL that answered *is* the
    service, while a launcher is only ever spoken to over its own pipes, so a
    handshake says nothing about where it connects.
    """
    candidate = stdio(["uvx", "mcp-atlassian"], {"JIRA_URL": "https://jira.elsewhere.test"})
    verdict = assess(candidate, "https://jira.example.com")
    assert verdict.kind == UNGROUNDED
    assert "jira.elsewhere.test" in verdict.reason
    assert "was not published by it" in verdict.reason


def test_the_same_package_told_where_to_go_is_grounded() -> None:
    candidate = stdio(
        ["npx", "-y", "confluence-datacenter-mcp"],
        {"CONFLUENCE_URL": "https://confluence.example.com"},
    )
    assert assess(candidate, "https://confluence.example.com").kind == GROUNDED


# --------------------------------------------------------------------------- #
# URL entries: the right host, and a path
# --------------------------------------------------------------------------- #


def test_the_sites_front_door_is_not_an_endpoint() -> None:
    """The real confluence.example.com entry."""
    verdict = assess(url("https://confluence.example.com"), "https://confluence.example.com")
    assert verdict.kind == UNGROUNDED
    assert "front door" in verdict.reason


def test_a_bare_origin_that_actually_handshaked_is_kept() -> None:
    """Rare, but it is a fact rather than a guess, and facts win."""
    candidate = url("https://mcp.example.test", verified=True)
    assert assess(candidate, "https://mcp.example.test").kind == GROUNDED


def test_an_endpoint_with_a_path_is_fine_without_a_handshake() -> None:
    """A credential-walled /mcp is the ordinary provisional case."""
    assert assess(url("https://openqa.opensuse.org/mcp"), "https://openqa.opensuse.org").kind == (GROUNDED)


def test_a_url_on_a_different_host_that_answered_is_believed() -> None:
    """github.com is served from api.githubcopilot.com. That is normal.

    A vendor hosting MCP on a sibling domain is the ordinary shape of the
    thing, and the proof is that we contacted *that* address and it replied as
    an endpoint. An earlier version required the hostnames to match and refused
    GitHub's own MCP server for github.com.
    """
    # Provenance, because that is what separates GitHub's own endpoint from a
    # stranger's multi-tenant host: this one is in GitHub's documentation.
    candidate = url(
        "https://api.githubcopilot.com/mcp/",
        source="named in the target's own documentation (https://docs.github.com/…)",
    )
    candidate.handshake = Handshake(
        ok=False,
        status=401,
        requires_auth=True,
        auth_evidence=["it answered application/json, not a web page"],
    )
    verdict = assess(candidate, "https://github.com")
    assert verdict.kind == SERVED_ELSEWHERE
    assert verdict.registrable is True
    assert verdict.served_by == "api.githubcopilot.com"


def test_a_credential_wall_counts_as_answering(ctx) -> None:
    """The GitHub case never handshaked -- it returned 401 with evidence.

    Requiring a completed handshake would discard every endpoint whose token
    the operator has not exported yet, which is most of them on a first run.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import answered_as_mcp

    walled = url("https://api.githubcopilot.com/mcp/")
    walled.handshake = Handshake(
        ok=False,
        status=401,
        requires_auth=True,
        auth_evidence=["evidence"],
    )
    assert answered_as_mcp(walled)

    silent = url("https://api.githubcopilot.com/mcp/")
    silent.handshake = Handshake(ok=False, status=404)
    assert not answered_as_mcp(silent)


def test_a_url_on_a_different_host_that_never_answered_is_refused() -> None:
    """Without a reply it is just a URL pointing somewhere else."""
    verdict = assess(url("https://mcp.elsewhere.test/mcp"), "https://jira.example.com")
    assert verdict.kind == UNGROUNDED
    assert "not jira.example.com" in verdict.reason
    assert "never answered" in verdict.reason


def test_a_subdomain_of_the_target_counts() -> None:
    assert assess(url("https://mcp.example.com/mcp"), "https://example.com").kind == GROUNDED


@pytest.mark.parametrize(
    ("host", "other", "expected"),
    [
        ("example.com", "example.com", True),
        ("example.com", "mcp.example.com", True),
        ("example.com", "example.com.evil.net", False),
        ("example.com", "notexample.com", False),
    ],
)
def test_same_service_compares_labels_not_substrings(host, other, expected) -> None:
    assert same_service(host, other) is expected


# --------------------------------------------------------------------------- #
# and what the writer does with the verdict
# --------------------------------------------------------------------------- #


def test_an_ungrounded_entry_is_refused_with_its_reason(ctx) -> None:
    candidate = stdio(["npx", "-y", "@kubb/mcp"])
    candidate.handshake = WORKS  # it really does handshake; that was never the issue

    registrations = McpWriter(ctx).publish(TargetOutcome(target="https://confluence.example.com", verified=[candidate]))

    assert registrations[0].refused
    assert "confluence.example.com" in registrations[0].refused
    assert any("name the service it talks to" in r for r in registrations[0].remediation)


def test_an_entry_served_from_elsewhere_is_registered_and_said_out_loud(ctx) -> None:
    """The real github.com entry: believed, and the hop made visible.

    The ``source`` is load-bearing and was not always given here. Without it
    this candidate passed only because ``endpoint_hosts`` had put its own URL
    into ``served_by`` and the rule then read that back as corroboration --
    "api.githubcopilot.com is served from api.githubcopilot.com, which this run
    found answering". A claim cannot be its own evidence, and while that circle
    stood, ``https://mcp.atlassian.com/v2/mcp`` was registered for
    confluence.example.com on exactly the same reasoning.

    What is left once the circle is gone is what was always doing the work:
    GitHub documented this address, and the address is GitHub's own.
    """
    candidate = url(
        "https://api.githubcopilot.com/mcp/",
        source="GitHub's own documentation for the remote GitHub MCP server",
    )
    candidate.handshake = Handshake(
        ok=False,
        status=401,
        requires_auth=True,
        auth_evidence=["evidence"],
    )
    writer = McpWriter(ctx)

    registrations = writer.publish(TargetOutcome(target="https://github.com", provisional=[candidate]))
    briefing = writer.briefing(registrations)

    assert not registrations[0].refused
    assert "these_entries_are_served_from_another_host" in briefing
    shown = briefing["these_entries_are_served_from_another_host"]["github-com"]
    assert "api.githubcopilot.com" in shown
    assert "why_that_is_expected" in briefing


def test_the_check_is_configuration(ctx) -> None:
    """An operator who disagrees can turn it off; the default is on."""
    ctx.settings.set("mcp.registration.require_address_in_entry", False)
    candidate = stdio(["npx", "-y", "@kubb/mcp"])
    candidate.handshake = WORKS

    registrations = McpWriter(ctx).publish(TargetOutcome(target="https://confluence.example.com", verified=[candidate]))
    assert not registrations[0].refused


# --------------------------------------------------------------------------- #
# the floor: a believed entry always carries an address
# --------------------------------------------------------------------------- #


def test_every_way_of_passing_puts_an_address_in_the_entry() -> None:
    """Not checked separately -- it falls out of the two ways to pass.

    Naming the target means its hostname is in the entry; being served
    elsewhere means a URL is. So "there is always an address to look at" holds
    by construction rather than by a rule somebody could forget.
    """
    served = url(
        "https://api.githubcopilot.com/mcp/",
        # As the program writes it: the documentation page it read, on the target's side.
        source="named in the target's own documentation (https://docs.github.com/en/copilot/mcp)",
    )
    served.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    passing = [
        (stdio(["npx", "-y", "x"], {"ENDPOINT": "https://smelt.example.net/g"}), "https://smelt.example.net"),
        (url("https://openqa.opensuse.org/mcp"), "https://openqa.opensuse.org"),
        (served, "https://github.com"),
    ]
    for candidate, target in passing:
        verdict = assess(candidate, target)
        assert verdict.registrable, verdict.reason
        assert addresses_in(candidate) or candidate.url, "believed, yet names no address"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # A version is not an address; this is what keeps the floor honest.
        (["npx", "-y", "mcp-graphql@2.0.4"], []),
        (["npx", "-y", "@atlassian-dc-mcp/jira@0.34.0"], []),
        (["npx", "-y", "@kubb/mcp"], []),
        (["uvx", "mcp-atlassian", "--url", "https://jira.example.com"], ["https://jira.example.com"]),
        (["srv", "--host", "openqa.opensuse.org"], ["openqa.opensuse.org"]),
    ],
)
def test_what_counts_as_an_address(command, expected) -> None:
    assert addresses_in(stdio(command)) == expected
