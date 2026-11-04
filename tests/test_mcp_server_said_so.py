"""Ask the server, not only the entry.

``build.opensuse.org`` finished a run with nothing::

    package  [fallback]  mcp verified (npm search hit (@ateam-ai/mcp))
    bridge               the package stage already answered; lower tiers not searched
    refused  build-opensuse-org  this entry names no address at all

Two separate faults met there, and only one of them was the refusal.

**The refusal was right.** ``@ateam-ai/mcp`` came from an npm keyword search and
names no address; nothing in that entry could reach Open Build Service, and
nothing in it tells an operator what it talks to.

**Settling the stage on it was wrong**, and it is what cost the address its
answer: the package stage reported success, the bridge and strategy stages were
skipped beneath it, and the candidate was then thrown away at registration. The
deterministic loop already guarded against exactly this -- its comment names
``@ateam-ai/mcp`` -- but a *fallback* stage calls ``fallback.stage_package``
directly, so it met neither that guard nor the agentic one.

**And the rule itself was reading the wrong thing.** Every test for "does this
server serve my target" read the *entry*: a command line, an environment value,
a URL. That is what somebody configured. Meanwhile the server had been running,
had completed a handshake, and had described itself -- ``instructions``, tool
and prompt descriptions, resource URIs, the defaults in its input schemas -- and
every word of it was discarded except the bare names.

That is where the difference between the two cases is plainly visible. An Open
Build Service server lists resource URIs under
``https://build.opensuse.org/...``; a package that matched a keyword lists
nothing of the kind. The entry cannot tell them apart. The handshake can, and it
is better evidence than anything in the entry, because it is the running server
saying what it does rather than a claim about what it was configured to do.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.mcp_auto_discovery.grounding import (
    GROUNDED,
    UNGROUNDED,
    assess,
    server_said_so,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport

TARGET = "https://build.opensuse.org"


def package(name: str, advertised: list[str] | None = None) -> Candidate:
    """A tier-2 package that handshaked and advertised what it says it does."""
    candidate = Candidate(
        kind="stdio",
        name="build-opensuse-org",
        transport=Transport.STDIO,
        command=["npx", "-y", name],
    )
    candidate.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        server_name=name,
        tools=["search"],
        round_trip=True,
        advertised=advertised or [],
    )
    return candidate


# --------------------------------------------------------------------------- #
# the two real candidates, told apart by what they say
# --------------------------------------------------------------------------- #


def test_a_keyword_search_hit_that_says_nothing_is_still_refused() -> None:
    """`@ateam-ai/mcp`. The new evidence must not become a way in for this."""
    verdict = assess(package("@ateam-ai/mcp"), TARGET)
    assert verdict.kind == UNGROUNDED
    assert "did not name it either" in verdict.reason, "say that the server was asked"


def test_a_server_that_lists_the_targets_own_resources_is_believed() -> None:
    """The case the entry-only rule could not see.

    Nothing in the command mentions Open Build Service, and it does not need
    to: the server published resource URIs on the target's own host.
    """
    candidate = package(
        "obs-mcp",
        ["https://build.opensuse.org/public/build/openSUSE:Factory", "Project metadata"],
    )
    verdict = assess(candidate, TARGET)
    assert verdict.kind == GROUNDED
    assert verdict.evidence == "the handshake"
    assert "the server itself names" in verdict.reason


def test_the_instructions_count_too() -> None:
    candidate = package("obs-mcp", ["This server queries https://build.opensuse.org over its API."])
    assert assess(candidate, TARGET).kind == GROUNDED


def test_a_schema_default_counts_too() -> None:
    """A generic server is configured through its own input schema."""
    candidate = package("generic-rest-mcp", ["https://build.opensuse.org"])
    assert assess(candidate, TARGET).kind == GROUNDED


def test_a_subdomain_of_the_target_counts() -> None:
    candidate = package("obs-mcp", ["https://api.build.opensuse.org/v1/projects"])
    assert assess(candidate, TARGET).kind == GROUNDED


# --------------------------------------------------------------------------- #
# and it must not be looser than the rule it joins
# --------------------------------------------------------------------------- #


def test_a_lookalike_hostname_does_not_count() -> None:
    """Matched by host through `same_service`, never as a bare substring."""
    candidate = package("evil-mcp", ["https://build.opensuse.org.evil.test/steal"])
    assert assess(candidate, TARGET).kind == UNGROUNDED


def test_another_service_entirely_does_not_count() -> None:
    candidate = package("jira-mcp", ["https://jira.example.test/rest/api/2/issue"])
    assert assess(candidate, TARGET).kind == UNGROUNDED


def test_a_server_that_never_handshaked_advertises_nothing() -> None:
    """Evidence from a session that did not happen is not evidence."""
    candidate = package("obs-mcp", ["https://build.opensuse.org/x"])
    assert candidate.handshake is not None
    candidate.handshake.ok = False
    assert server_said_so(candidate, "build.opensuse.org") == ""
    assert assess(candidate, TARGET).kind == UNGROUNDED


def test_the_entry_still_wins_when_it_names_the_target() -> None:
    """Cheaper, and readable without running anything -- so it is asked first."""
    candidate = package("generic-mcp", ["https://build.opensuse.org/x"])
    candidate.env = {"API_BASE_URL": "https://build.opensuse.org"}
    verdict = assess(candidate, TARGET)
    assert verdict.kind == GROUNDED
    assert verdict.evidence == "env API_BASE_URL", "the entry, not the handshake"


# --------------------------------------------------------------------------- #
# what the prober actually keeps
# --------------------------------------------------------------------------- #


def test_a_resource_uri_is_kept_even_when_the_entry_has_a_name(ctx) -> None:
    """`name or uri` discarded the URI, which is the field that matters.

    A resource with a human name *and* a URI is the ordinary shape, so the one
    piece of evidence that says which host the server talks to was being
    dropped in exactly the common case.
    """
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    shake = Handshake(ok=True, transport=Transport.STDIO)
    McpProber(ctx)._absorb_self_description(
        shake,
        {"name": "Factory", "uri": "https://build.opensuse.org/public/build/openSUSE:Factory"},
    )
    assert any("build.opensuse.org" in text for text in shake.advertised)


def test_a_tool_description_and_its_schema_are_kept(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    shake = Handshake(ok=True, transport=Transport.STDIO)
    McpProber(ctx)._absorb_self_description(
        shake,
        {
            "name": "search",
            "description": "Search packages on the build service",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "default": "https://build.opensuse.org",
                        "description": "API root",
                    }
                },
            },
        },
    )
    assert any("build.opensuse.org" in text for text in shake.advertised)


@pytest.mark.parametrize(
    "key",
    [
        "entries",
        "value_chars",
        "schema_depth",
        "schema_list_items",
        "instruction_chars",
        "describing_keys",
        "schema_keys",
        "schema_containers",
    ],
)
def test_every_knob_here_is_a_setting(ctx, key: str) -> None:
    """Nothing about which fields to read is written in Python.

    Pinned by name because these are read section-relative
    (``advertised.int_("entries")``), which the settings-reachability scan
    deliberately does not follow -- it tracks dotted literals. Without this,
    deleting one from the defaults would silently fall back to a literal in
    ``probes.py`` and the rule would stop being configuration.
    """
    assert ctx.settings.get(f"mcp.verification.advertised.{key}", None) is not None


def test_the_fields_read_are_the_fields_configured(ctx) -> None:
    """Change the config, change the behaviour -- the test that it is really wired."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.settings.set("mcp.verification.advertised.describing_keys", "uri")
    shake = Handshake(ok=True, transport=Transport.STDIO)
    McpProber(ctx)._absorb_self_description(
        shake, {"uri": "https://build.opensuse.org/x", "description": "https://elsewhere.test"}
    )
    assert any("build.opensuse.org" in t for t in shake.advertised)
    assert not any("elsewhere.test" in t for t in shake.advertised), "description was not asked for"


def test_collection_is_bounded(ctx) -> None:
    """This is third-party output and it is kept in memory and in the report."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.settings.set("mcp.verification.advertised.entries", 3)
    prober = McpProber(ctx)
    shake = Handshake(ok=True, transport=Transport.STDIO)
    for _ in range(50):
        prober._absorb_self_description(shake, {"description": "x" * 5000})
    assert len(shake.advertised) <= 8, len(shake.advertised)
    assert all(len(text) <= 500 for text in shake.advertised)


# --------------------------------------------------------------------------- #
# a stage may not settle on something registration will refuse -- in either mode
# --------------------------------------------------------------------------- #


def test_the_agentic_loop_drops_an_unregistrable_stage_result(ctx) -> None:
    """The guard existed, in the loop that did not run for a fallback stage.

    `_drop_ungrounded` filters what an *agent* proposes; `discard_unregistrable`
    filters what the deterministic loop produces. A stage that fell back called
    `fallback.stage_package` directly and met neither -- which is how one npm
    search hit closed the two tiers below it and left the address empty.
    """
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome

    outcome = TargetOutcome(target=TARGET)
    stage = StageOutcome(stage="package")
    stage.verified = [package("@ateam-ai/mcp")]
    stage.candidates = list(stage.verified)

    AgenticPipeline(ctx).fallback.discard_unregistrable(stage, outcome)

    assert stage.verified == [], "it would have been refused at registration"
    assert stage.findings["not_registrable"], "and the reason is recorded"


def test_a_grounded_stage_result_survives_the_same_guard(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome

    outcome = TargetOutcome(target=TARGET)
    stage = StageOutcome(stage="package")
    stage.verified = [package("obs-mcp", ["https://build.opensuse.org/public"])]
    stage.candidates = list(stage.verified)

    AgenticPipeline(ctx).fallback.discard_unregistrable(stage, outcome)
    assert len(stage.verified) == 1, "the server said it serves the target"


@pytest.mark.parametrize("pipeline", ["agentic", "traditional"])
def test_both_pipelines_use_the_same_guard(ctx, pipeline: str) -> None:
    """Not "the same wording" -- the same bound method."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    agentic = AgenticPipeline(ctx)
    assert agentic.fallback.discard_unregistrable.__func__ is (TraditionalPipeline.discard_unregistrable)
