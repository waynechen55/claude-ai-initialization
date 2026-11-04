"""The real hosts that broke this program, kept as tests.

Every serious defect found in phase 4 came from a live run against real
infrastructure, and every one of them was the same bug wearing a different hat:
**the recorded claim outran the evidence.** Each fix generalised, and each fix
was still incomplete until the next real host found the next gap.

So the hosts themselves are the corpus. The responses below are what those
services actually sent -- copied from the run, not invented -- and the
assertions are what the program must conclude from them. A change that re-breaks
any of these fails here, which is the only way this list stays useful.

    confluence.example.com   a login page wearing `application/json`, and
                          `confluence-datacenter-mcp`, which handshakes and
                          lists 115 tools with no credential at all
    jira.example.com         an HTML 401 on the front door, `OAuth` in the
                          challenge, and `?format=mcp` added by us
    smelt.example.net         `mcp-graphql` -- correct, and relevant only through
                          its configuration, so a name-only filter loses it
    openqa.*              a credential that is `username:key:secret`
    @kubb/mcp             a TypeScript code generator registered as the MCP
                          server for three unrelated services
    api.mcp.ai            a Brazilian regulator's tool, `examplep_administradores`,
                          registered for seven hosts because "examplep" contains
                          "example" and `io.github.mcp-dir` contains "github"
    confluence.example.com   (again) two servers waiting for a credential, neither
                          launched as written until publish, where the chosen
                          one died and the address ended empty -- see
                          test_mcp_pending_entry_starts.py
    build.opensuse.org    openSUSE's osc-mcp, found four times and refused for a
                          PATH list read as a file and a user+password trialled
                          as one secret -- see test_mcp_credential_in_parts.py
    osc-mcp               a tool property named "path_repository jsonschema:",
                          which made the API refuse every request of the session
                          -- see test_mcp_tool_schema_names.py
"""

from __future__ import annotations

import pytest
from fake_secrets import fake

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.registry_client import (
    GENERIC_HOST_LABELS,
    product_tokens,
    relevance_of,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

# --------------------------------------------------------------------------- #
# what the program must search for
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target, expected_first",
    [
        # The product is the leftmost meaningful label. Taking the last one
        # asked npm about "example" for both Atlassian hosts, so the packages that
        # exist for them -- confluence-datacenter-mcp, jira-datacenter-mcp --
        # were never findable.
        ("https://confluence.example.com", "confluence"),
        ("https://jira.example.com", "jira"),
        ("https://bugzilla.example.com", "bugzilla"),
        # ".de" missing from the generic labels made this target's search term
        # the single word "de", which is Spanish, and npm answered with a
        # Colombian compliance server.
        ("https://smelt.example.net", "smelt"),
        ("https://openqa.example.net", "openqa"),
        ("https://gitlab.example.net", "gitlab"),
        # ...without breaking the ordinary shapes.
        ("https://api.example.com/v1", "example"),
        ("https://mcp.deepwiki.com", "deepwiki"),
        ("https://github.com", "github"),
    ],
)
def test_the_search_term_names_the_product(target: str, expected_first: str) -> None:
    assert product_tokens(target)[0] == expected_first


def test_the_organisation_is_kept_as_a_second_question() -> None:
    """A vendor may publish one server for its whole estate."""
    assert product_tokens("https://confluence.example.com") == ["confluence", "example"]


def test_the_label_list_is_configuration() -> None:
    assert "de" in GENERIC_HOST_LABELS, "the omission that produced 'de mcp server'"
    assert product_tokens("https://smelt.example.net", generic=["com"]) == ["smelt", "example", "net"]


# --------------------------------------------------------------------------- #
# what may be registered for a target
# --------------------------------------------------------------------------- #


def package(
    name: str, *, args: list[str] | None = None, env: dict[str, str] | None = None, source: str = ""
) -> Candidate:
    return Candidate(
        kind="stdio",
        name=name,
        command=["npx", "-y", name],
        args=args or [],
        env=env or {},
        source=source,
        transport=Transport.STDIO,
    )


def test_a_package_that_names_the_product_is_relevant() -> None:
    hit = package("confluence-datacenter-mcp")
    assert "names the product" in relevance_of(hit, ["confluence", "example"], "confluence.example.com")


def test_a_generic_server_configured_for_the_target_is_relevant() -> None:
    """The SMELT case, and the reason a name-only filter would be wrong.

    `mcp-graphql` mentions SMELT nowhere. It is the correct answer because it
    is pointed at SMELT's own GraphQL endpoint, and that entry works today.
    """
    hit = package("mcp-graphql", env={"ENDPOINT": "https://smelt.example.net/graphql/"})
    why = relevance_of(hit, ["smelt", "example"], "smelt.example.net")
    assert "generic server configured for smelt.example.net" in why


def test_a_code_generator_is_not_an_mcp_server_for_confluence() -> None:
    """@kubb/mcp was registered for bugzilla, confluence AND build.opensuse.org."""
    hit = package("@kubb/mcp", source="npm search hit (@kubb/mcp)")
    assert relevance_of(hit, ["confluence", "example"], "confluence.example.com") == ""
    assert relevance_of(hit, ["bugzilla", "example"], "bugzilla.example.com") == ""
    assert relevance_of(hit, ["build", "opensuse"], "build.opensuse.org") == ""


def test_a_colombian_compliance_server_is_not_smelt() -> None:
    hit = package("complai-mcp", source="npm search hit (complai-mcp)")
    assert relevance_of(hit, ["smelt", "example"], "smelt.example.net") == ""


@pytest.mark.parametrize(
    "host",
    [
        "gitlab.example.net",
        "jira.example.com",
        "confluence.example.com",
        "openqa.example.net",
        "sd.example.com",
        "build.example.net",
        "github.com",
    ],
)
def test_a_brazilian_regulators_tool_is_none_of_seven_services(host: str) -> None:
    """The registry's answer to "example", registered as all seven. See
    tests/test_mcp_substring_is_not_a_name.py for the grounding and writer halves."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    hit = Candidate(
        kind="url",
        name="examplep_administradores-mcp",
        url="https://api.mcp.ai/p_examplep_administradores",
        transport=Transport.STREAMABLE_HTTP,
        source="MCP registry remote (io.github.mcp-dir/examplep_administradores-mcp)",
    )
    hit.handshake = Handshake(ok=True, server_name="mcp-ai", tools=["connect"], round_trip=True)
    assert relevance_of(hit, product_tokens(host), host) == ""
    assert assess(hit, f"https://{host}").kind == UNGROUNDED


# --------------------------------------------------------------------------- #
# a credential must not decide which candidate wins
# --------------------------------------------------------------------------- #


def confluence_server() -> Candidate:
    """What `confluence-datacenter-mcp` really does with no token set.

    Measured, not imagined: it completes the handshake and lists 115
    `confluence_*` tools. The token is read when a tool is *called*.
    """
    return Candidate(
        kind="stdio",
        name="confluence-example-com",
        command=["npx", "-y", "confluence-datacenter-mcp@0.5.0"],
        env={"CONFLUENCE_HOST": "confluence.example.com"},
        secret_env={"CONFLUENCE_API_TOKEN": "env:CONFLUENCE_API_TOKEN"},
        transport=Transport.STDIO,
        handshake=Handshake(
            ok=True,
            transport=Transport.STDIO,
            server_name="atlassian-confluence-mcp",
            server_version="0.5.0",
            protocol_version="2025-06-18",
            tools=[f"confluence_tool_{i}" for i in range(115)],
            round_trip=True,
            round_trip_method="tools/list",
            credential_pending=["CONFLUENCE_API_TOKEN"],
        ),
    )


def test_a_handshake_without_a_credential_is_still_a_handshake() -> None:
    shake = confluence_server().handshake
    assert shake is not None and shake.ok, "the exchange really did complete"
    assert len(shake.tools) == 115


def test_but_it_is_not_promised_as_working() -> None:
    """`verified` is a promise the entry works as written. It does not."""
    candidate = confluence_server()
    assert candidate.verified is False
    assert candidate.needs_credential is True, "registered provisionally, slot empty"


def test_the_report_shows_what_it_proved_rather_than_a_row_of_dashes() -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    checks = {c.name: c for c in CandidateVerifier.audit(confluence_server())}
    assert checks["connected"].ok is True
    assert checks["tools"].ok is True and "115" in checks["tools"].detail
    assert checks["two-way"].ok is True
    assert checks["usable"].ok is None
    assert "CONFLUENCE_API_TOKEN" in checks["usable"].detail


def test_the_slot_the_proven_launch_did_not_have_is_empty_while_unset(ctx) -> None:
    """The variable is named in the entry, in the one form that changes nothing.

    This asserted ``"${CONFLUENCE_API_TOKEN}"`` once, and then its absence.
    The bare form is how two servers died on start-up: Claude Code passes an
    unset ``${NAME}`` through as those literal characters, so ``mcp-bugzilla``
    was handed an auth mode called ``${BUGZILLA_AUTH_MODE}``. Leaving it out
    fixed that and hid the variable from whoever copies the file.

    ``${NAME:-}`` expands to an empty string while unset -- the launch this
    candidate was proven with -- and to the token the moment it is exported.
    The entry is then launched as written before it is accepted, and a server
    that minds an empty value has the slot withdrawn by a repair.
    """
    from initialize_claude_ai.mcp_auto_discovery.portability import expand

    candidate = confluence_server()
    writer = McpWriter(ctx)
    needs = writer.prepare_provisional(candidate, "confluence-example-com")
    entry = writer.entry_for(candidate)

    assert "CONFLUENCE_API_TOKEN" in needs, "the operator is still told to export it"
    assert entry["env"]["CONFLUENCE_API_TOKEN"] == "${CONFLUENCE_API_TOKEN:-}"
    assert expand(entry["env"]["CONFLUENCE_API_TOKEN"], {}) == "", "unset means empty"
    assert "npx" in entry["command"]
    assert any("proven to start without it" in n for n in candidate.notes), "say why"


def test_a_credential_the_proven_launch_did_have_is_written(ctx) -> None:
    """The other half: a slot that was part of what was tested is kept.

    Without this the fix above would read as "never write a credential", which
    would break every server whose variable the operator has already exported.
    """
    candidate = confluence_server()
    assert candidate.handshake is not None
    candidate.handshake.credential_pending = []  # it was trialled *with* the token

    writer = McpWriter(ctx)
    writer.prepare_provisional(candidate, "confluence-example-com")
    entry = writer.entry_for(candidate)

    assert entry["env"]["CONFLUENCE_API_TOKEN"] == "${CONFLUENCE_API_TOKEN}"


def test_needing_a_credential_never_loses_to_needing_none() -> None:
    """The invariant behind the whole incident.

    `@kubb/mcp` beat `confluence-datacenter-mcp` because it declared no secrets
    and so could be launched. The correct candidate was punished for describing
    its own requirements.
    """
    correct = confluence_server()
    irrelevant = package("@kubb/mcp")
    irrelevant.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        server_name="Kubb",
        server_version="5.3.1",
        tools=["generate", "init", "validate"],
        round_trip=True,
    )

    # The irrelevant one is "verified" and the correct one is not -- which is
    # exactly why relevance has to be decided before verification gets a vote.
    assert irrelevant.verified is True
    assert relevance_of(irrelevant, ["confluence", "example"], "confluence.example.com") == ""
    assert relevance_of(correct, ["confluence", "example"], "confluence.example.com") != ""


# --------------------------------------------------------------------------- #
# the variable name has to say what to put in it
# --------------------------------------------------------------------------- #


def refused(url: str, **kw) -> Candidate:
    return Candidate(
        kind="url",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(
            ok=False, status=403, requires_auth=True, auth_evidence=["it answered application/json, not a web page"]
        ),
        **kw,
    )


def test_openqa_is_named_after_its_three_part_credential(ctx) -> None:
    """`..._API_KEY` read as "set the key". Setting just the key returns 403."""
    name = McpWriter(ctx).credential_env_name(refused("https://openqa.opensuse.org/mcp"), "openqa-opensuse-org")
    assert name == "OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET"
    assert "API_KEY" not in name


def test_a_shape_learned_at_discovery_needs_no_table_entry(ctx) -> None:
    """The mechanism must work for a product nobody has written a profile for."""
    candidate = refused(
        "https://jira.example.com/mcp",
        secret_env={"JIRA_EXAMPLE_COM_API_KEY": "env:JIRA_EXAMPLE_COM_API_KEY"},
        secret_formats={"JIRA_EXAMPLE_COM_API_KEY": "email:api_token"},
    )
    assert McpWriter(ctx).credential_env_name(candidate, "jira-example-com") == ("JIRA_EXAMPLE_COM_EMAIL_API_TOKEN")


def test_a_single_opaque_token_keeps_the_short_name(ctx) -> None:
    """Renaming everything would be noise; only a composite value needs it.

    A shape with no separator describes one value, and `..._TOKEN` is both
    accurate and shorter than anything derived from prose.
    """
    candidate = refused(
        "https://vendor.example.com/mcp",
        secret_formats={"VENDOR_EXAMPLE_COM_TOKEN": "an opaque bearer token"},
    )
    assert McpWriter(ctx).credential_env_name(candidate, "vendor-example-com") == ("VENDOR_EXAMPLE_COM_TOKEN")


def test_a_name_the_agent_chose_survives_when_no_shape_contradicts_it(ctx) -> None:
    """The agent may know the product's own documented variable."""
    candidate = refused(
        "https://vendor.example.com/mcp",
        secret_env={"VENDOR_PERSONAL_ACCESS_TOKEN": "env:VENDOR_PERSONAL_ACCESS_TOKEN"},
    )
    assert McpWriter(ctx).credential_env_name(candidate, "vendor-example-com") == ("VENDOR_PERSONAL_ACCESS_TOKEN")


def test_the_operators_own_variable_is_never_renamed(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.bearer_token_ref", "env:MY_OWN_VAR")
    assert (
        McpWriter(ctx).credential_env_name(refused("https://openqa.opensuse.org/mcp"), "openqa-opensuse-org")
        == "MY_OWN_VAR"
    )


def test_a_shape_that_is_really_a_value_is_discarded(ctx) -> None:
    """A shape is a description. Anything that looks like a secret is dropped."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    _plain, secrets, notes, shapes, _src = AgenticPipeline(ctx)._split_env(
        {},
        {"TOK": {"reference": "env:TOK", "shape": fake("glpat-", "AbCdEf01", "23456789", "XyZw")}},
    )
    assert secrets == {"TOK": "env:TOK"}
    assert shapes == {}, "that was a credential, not a description of one"
    assert any("looked like a value" in n for n in notes)


def test_the_old_name_is_replaced_rather_than_kept_alongside(ctx) -> None:
    """Both names in the entry would let the *worse* one win.

    `${OPENQA_..._API_KEY}` sorts before `${OPENQA_..._USERNAME_KEY_SECRET}`,
    so the operator would be told to export the accurate variable while the
    file quietly read the misleading one. One credential, one name.
    """
    candidate = refused(
        "https://openqa.opensuse.org/mcp",
        secret_env={"OPENQA_OPENSUSE_ORG_API_KEY": "env:OPENQA_OPENSUSE_ORG_API_KEY"},
        secret_formats={"OPENQA_OPENSUSE_ORG_API_KEY": "username:key:secret"},
    )
    writer = McpWriter(ctx)
    needs = writer.prepare_provisional(candidate, "openqa-opensuse-org")

    assert needs == ["OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET"]
    assert "OPENQA_OPENSUSE_ORG_API_KEY" not in candidate.secret_env
    header = writer.entry_for(candidate)["headers"]["Authorization"]
    assert header == "Bearer ${OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET}"


def test_the_rename_is_announced_rather_than_silent(ctx) -> None:
    """An export already in someone's shell goes dead; they have to be told."""
    candidate = refused(
        "https://openqa.opensuse.org/mcp",
        secret_env={"OPENQA_OPENSUSE_ORG_API_KEY": "env:OPENQA_OPENSUSE_ORG_API_KEY"},
        secret_formats={"OPENQA_OPENSUSE_ORG_API_KEY": "username:key:secret"},
    )
    McpWriter(ctx).prepare_provisional(candidate, "openqa-opensuse-org")
    notice = " ".join(candidate.notes)
    assert "not OPENQA_OPENSUSE_ORG_API_KEY" in notice
    assert "Export the new name" in notice


# --------------------------------------------------------------------------- #
# an entry has to survive the machine that made it
# --------------------------------------------------------------------------- #


def test_the_openqa_venv_entry_would_now_be_refused(ctx, tmp_path) -> None:
    """The real entry, verbatim, that failed with ENOENT on another host.

        "command": ".../artifacts/openqa-mcp-bridge/venv/bin/python3",
        "args": ["-m", "openqa_mcp", "--readonly"]

    It handshaked when it was verified. `.mcp.json` is meant to be copied, and
    a venv this program built in its own scratch space travels nowhere.
    """
    from initialize_claude_ai.mcp_auto_discovery.portability import DISPOSABLE

    venv = tmp_path / "work" / "agent-workspace" / "artifacts" / "openqa-mcp-bridge" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python3").write_text("")

    candidate = Candidate(
        kind="stdio",
        name="openqa-example-net",
        transport=Transport.STDIO,
        command=[str(venv / "bin" / "python3"), "-m", "openqa_mcp", "--readonly"],
        env={"OPENQA_SERVER": "openqa.example.net", "OPENQA_READONLY": "true"},
    )
    verdict = McpWriter(ctx).portability_of(candidate)
    assert verdict.kind == DISPOSABLE
    assert verdict.registrable is False


def test_the_launcher_form_of_the_same_server_is_portable(ctx) -> None:
    """`uv tool run openqa-mcp` is the same server with nothing to rot.

    It is what the operator's own working configuration uses.
    """
    from initialize_claude_ai.mcp_auto_discovery.portability import PORTABLE

    candidate = Candidate(
        kind="stdio",
        name="openqa-example-net",
        transport=Transport.STDIO,
        command=["uv", "tool", "run", "openqa-mcp", "--readonly"],
        env={"OPENQA_SERVER": "openqa.example.net"},
    )
    assert McpWriter(ctx).portability_of(candidate).kind == PORTABLE


# --------------------------------------------------------------------------- #
# one target, one answer -- jira.example.com came back with two entries
# --------------------------------------------------------------------------- #
#
# From an ordinary run: no --the-best-mcp, no --exhaustive-mcp, no --concurrency.
#
#     "jira-example-com-jira-example-com-atlassian-dc-mcp-b1ff"  npx @atlassian-dc-mcp/jira
#     "jira-example-com-jira-example-com-bridge"                 uvx mcp-atlassian
#
# Three separate defects in two entries: the package stage found a server that
# wanted a credential, which since `trial_without_secrets` is *provisional* and
# not *verified*, so the descent carried on to the bridge stage and found a
# second one; the cap that should have stopped that was spent per stage, so each
# stage kept its own; and both names say the address twice.


def jira_walled(origin: str, name: str) -> Candidate:
    """A jira.example.com candidate that handshaked but wants a token."""
    return Candidate(
        kind="stdio",
        name=name,
        origin=origin,
        transport=Transport.STDIO,
        command=["npx", "-y", name],
        confidence=0.6,
        handshake=Handshake(ok=True, status=200, credential_pending=True),
    )


def test_a_credential_walled_find_settles_the_target_not_just_the_stage(ctx) -> None:
    """The package stage answered. The bridge stage must not be asked again.

    `verified` was the only thing that ended the descent, and a package trialled
    without its secret never reaches `verified` -- so every credentialled target
    went on to be searched at tier 2 *and* tier 3, at minutes of agent time each.
    """
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://jira.example.com")
    package = StageOutcome(stage=Stage.PACKAGE)
    package.candidates = [jira_walled(Stage.PACKAGE, "@atlassian-dc-mcp/jira")]
    outcome.absorb(package, pipeline.provisional_limit(exhaustive=False))

    skip = pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False)
    assert skip is not None, "the bridge stage ran and produced the second entry"
    assert "package stage" in skip.detail, skip.detail
    assert "credential" in skip.detail


def test_but_exhaustive_still_asks_every_tier(ctx) -> None:
    """--exhaustive-mcp wants the inventory, and is never settled early."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://jira.example.com")
    package = StageOutcome(stage=Stage.PACKAGE)
    package.candidates = [jira_walled(Stage.PACKAGE, "@atlassian-dc-mcp/jira")]
    outcome.absorb(package, pipeline.provisional_limit(exhaustive=True))

    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=True) is None


def test_the_cap_is_a_budget_for_the_target_not_for_each_stage(ctx) -> None:
    """max_provisional = 1 has to mean one entry, not one entry per stage.

    Both stages here keep their own best candidate, exactly as they did on the
    real run; the target accepts the first and refuses the second, and the
    first is the higher tier because stage order is tier order.
    """
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    limit = pipeline.provisional_limit(exhaustive=False)
    outcome = TargetOutcome(target="https://jira.example.com")
    for stage_name, package_name in (
        (Stage.PACKAGE, "@atlassian-dc-mcp/jira"),
        (Stage.BRIDGE, "mcp-atlassian"),
    ):
        stage = StageOutcome(stage=stage_name)
        stage.candidates = [jira_walled(stage_name, package_name)]
        outcome.absorb(stage, limit)

    assert [c.name for c in outcome.provisional] == ["@atlassian-dc-mcp/jira"]


def test_a_provisional_that_later_verifies_gives_its_slot_back(ctx) -> None:
    """A budget that a no-longer-waiting candidate keeps holding is a leak."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    limit = pipeline.provisional_limit(exhaustive=False)
    outcome = TargetOutcome(target="https://jira.example.com")

    walled_one = jira_walled(Stage.PACKAGE, "@atlassian-dc-mcp/jira")
    first = StageOutcome(stage=Stage.PACKAGE)
    first.candidates = [walled_one]
    outcome.absorb(first, limit)
    assert outcome.provisional == [walled_one]

    # The operator's token turned up and the same candidate verified.
    second = StageOutcome(stage=Stage.PACKAGE)
    second.verified = [walled_one]
    outcome.absorb(second, limit)
    assert outcome.provisional == []
    assert outcome.verified == [walled_one]


@pytest.mark.parametrize(
    ("target", "candidate_name", "expected"),
    [
        # What the run actually wrote, and what it should have written.
        (
            "https://jira.example.com",
            "jira-example-com-atlassian-dc-mcp-b1ff",
            "jira-example-com-atlassian-dc-mcp-b1ff",
        ),
        # The agent naming its find after the address and nothing else.
        ("https://jira.example.com", "jira-example-com", "jira-example-com"),
        # Twice over, which is what happens when two namers both prefix.
        (
            "https://jira.example.com",
            "jira-example-com-jira-example-com-bridge",
            "jira-example-com-bridge",
        ),
        # A suffix owing the address nothing keeps every word it has.
        ("https://jira.example.com", "bridge", "jira-example-com-bridge"),
        # And a partial overlap is left alone: mcp.devin.ai is not devin.ai.
        (
            "https://mcp.deepwiki.com",
            "mcp-devin-ai",
            "mcp-deepwiki-com-mcp-devin-ai",
        ),
    ],
)
def test_the_address_is_not_said_twice(ctx, target, candidate_name, expected) -> None:
    """`jira-example-com-jira-example-com-atlassian-dc-mcp-b1ff` is a name nobody can read.

    A discoverer asked about jira.example.com names what it finds after
    jira.example.com, and then the writer prefixes the address again. Trimming the
    one string would leave the next host to be found by whoever reads the next
    `.mcp.json`, so the rule is general -- and deliberately conservative: only a
    whole repeated address is dropped, never merely the labels two hosts happen
    to share.
    """
    writer = McpWriter(ctx)
    candidate = Candidate(
        kind="stdio",
        name=candidate_name,
        transport=Transport.STDIO,
        command=["npx", "-y", candidate_name],
    )
    base = writer.base_name(target)
    assert writer._compose(base, writer._distinguisher(candidate, target)) == expected


# --------------------------------------------------------------------------- #
# an entry has to say which server it talks to
# --------------------------------------------------------------------------- #
#
# Three entries from the same file, none of which an operator could check:
#
#   "confluence-example-com": npx -y @kubb/mcp
#   "confluence-example-com": {"type":"http","url":"https://confluence.example.com"}
#   "smelt-example-net-...":   env NODE_TLS_REJECT_UNAUTHORIZED = "${NODE_TLS_REJECT_UNAUTHORIZED}"


def test_the_kubb_entry_is_refused_for_not_naming_confluence(ctx) -> None:
    """The relevance gate should have caught this; grounding catches it anyway.

    Two independent checks for one failure is the point. Relevance is a
    judgement made while searching; grounding is an invariant checked while
    writing, and it needs no intelligence at all -- the address is in the entry
    or it is not.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = Candidate(
        kind="stdio",
        name="confluence-example-com",
        transport=Transport.STDIO,
        command=["npx", "-y", "@kubb/mcp"],
    )
    assert assess(candidate, "https://confluence.example.com").kind == UNGROUNDED


def test_the_confluence_front_door_is_refused(ctx) -> None:
    """`https://confluence.example.com` with no path, and it never handshaked.

    It reached the file because the probe URL was
    `https://confluence.example.com?format=mcp` -- a query this program invented --
    and the query was stripped before writing, inventing a URL nobody tested.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = Candidate(
        kind="url",
        name="confluence-example-com",
        url="https://confluence.example.com",
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(ok=False, status=401, requires_auth=True),
    )
    verdict = assess(candidate, "https://confluence.example.com")
    assert verdict.kind == UNGROUNDED
    assert "front door" in verdict.reason


def test_the_probe_query_is_no_longer_stripped(ctx) -> None:
    """Register what was probed. Tidying the URL invented a second one."""
    candidate = Candidate(
        kind="url",
        name="confluence-example-com",
        url="https://confluence.example.com?format=mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(ok=False, status=401, requires_auth=True),
    )
    assert McpWriter(ctx).registered_url(candidate) == "https://confluence.example.com?format=mcp"


def test_confluences_json_login_page_is_not_an_mcp_endpoint(ctx) -> None:
    """What the host actually answers to an anonymous POST, verbatim."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    class Reply:
        headers = {"content-type": "application/json;charset=UTF-8"}
        content_type = "application/json"
        text = (
            '{"statusCode":401,"message":"You are not logged in. <a href=\\"/login.action\\">Log in</a> to see more."}'
        )

    result = Handshake(status=401, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, Reply())
    assert result.auth_evidence == [], "a login page is a login page in any content type"


def test_openqas_plain_json_refusal_is_still_an_endpoint(ctx) -> None:
    """The other side of the same rule -- and a working tier-1 server.

    `{"error":"no api key"}` carries no MCP marker and no challenge header. If
    the evidence test were tightened to demand one, this endpoint would be lost
    and the run would descend to tier 3 and build a bridge for a host that
    already speaks MCP.
    """
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    class Reply:
        headers = {"content-type": "application/json"}
        content_type = "application/json"
        text = '{"error":"no api key","error_status":403}'

    result = Handshake(status=403, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, Reply())
    assert result.auth_evidence


def test_the_smelt_tls_switch_never_reaches_the_entry(ctx) -> None:
    """`"NODE_TLS_REJECT_UNAUTHORIZED": "${NODE_TLS_REJECT_UNAUTHORIZED}"`.

    Not a key, not a secret, not a token. It became a placeholder because
    `auth` matched inside `UNAUTHORIZED`; and it should not have been in the
    entry under any name, because `.mcp.json` is copied between machines.
    """
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-net",
        transport=Transport.STDIO,
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={
            "ENDPOINT": "https://smelt.example.net/graphql/",
            "ALLOW_MUTATIONS": "false",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        },
    )
    entry = McpWriter(ctx).entry_for(candidate)
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in entry["env"]
    assert entry["env"]["ALLOW_MUTATIONS"] == "false"
    assert entry["env"]["ENDPOINT"] == "https://smelt.example.net/graphql/"
    # And the entry now answers the need that variable was reached for: an
    # internal CA. NODE_EXTRA_CA_CERTS adds authorities and removes none, which
    # is the opposite of switching verification off. See test_mcp_trust_store.py.
    # Read through the one per-machine trust name (own_reference_names): a
    # NODE_EXTRA_CA_CERTS a login shell exports never replaces it.
    assert entry["env"]["NODE_EXTRA_CA_CERTS"].startswith("${ICA_MCP_CA_BUNDLE")


# --------------------------------------------------------------------------- #
# a vendor's MCP endpoint is often not on the vendor's own hostname
# --------------------------------------------------------------------------- #
#
# From a live run, both refused by the first version of the grounding rule:
#
#   refused developers-google-com  nothing in this entry mentions developers.google.com
#   refused github-com             the entry points at api.githubcopilot.com,
#                                  which is not github.com nor part of it
#
# Both were correct servers. github.com is served from api.githubcopilot.com and
# developers.google.com from developerknowledge.googleapis.com -- the ordinary
# shape of a hosted MCP offering. The log shows the program contradicting itself
# a few lines above each refusal:
#
#   tier policy blocked the package stage: a native MCP endpoint (behind
#   authentication) exists at https://api.githubcopilot.com/mcp/
#
# It believed the endpoint enough to forbid tiers 2, 3 and 4 for the whole
# address, and then would not write it down.


def served_elsewhere(url: str, *, answered: bool = True) -> Candidate:
    """A vendor endpoint on a sibling domain, as the endpoint stage found it."""
    handshake = (
        Handshake(
            ok=False, status=401, requires_auth=True, auth_evidence=["it answered application/json, not a web page"]
        )
        if answered
        else Handshake(ok=False, status=404)
    )
    return Candidate(
        kind="url",
        name="vendor",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        handshake=handshake,
        # How both were really found: the vendor's own documentation and the
        # official registry. That provenance is what distinguishes them from a
        # multi-tenant host a keyword search turned up.
        source="MCP registry remote (com.googleapis.developerknowledge/mcp)",
    )


@pytest.mark.parametrize(
    ("target", "endpoint"),
    [
        ("https://github.com", "https://api.githubcopilot.com/mcp/"),
        (
            "https://developers.google.com/knowledge/api",
            "https://developerknowledge.googleapis.com/mcp",
        ),
    ],
)
def test_a_vendor_endpoint_on_a_sibling_domain_is_believed(target, endpoint) -> None:
    """Both of the real entries the first version of the rule threw away."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import SERVED_ELSEWHERE, assess

    verdict = assess(served_elsewhere(endpoint), target)
    assert verdict.kind == SERVED_ELSEWHERE
    assert verdict.registrable is True


def test_but_only_because_that_address_answered() -> None:
    """The reply is the whole evidence. Without it, it is a URL elsewhere."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    verdict = assess(
        served_elsewhere("https://api.githubcopilot.com/mcp/", answered=False),
        "https://github.com",
    )
    assert verdict.kind == UNGROUNDED


def test_a_launcher_naming_no_address_is_still_refused() -> None:
    """The floor, and the reason @kubb/mcp was refused in the first place.

    Loosening the URL rule must not loosen this one: a launcher is never probed
    at an address, so nothing about running it shows where it points.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = Candidate(
        kind="stdio",
        name="c",
        transport=Transport.STDIO,
        command=["npx", "-y", "@kubb/mcp"],
    )
    assert assess(candidate, "https://confluence.example.com").kind == UNGROUNDED


# --------------------------------------------------------------------------- #
# the endpoint the run believed in and never wrote down
# --------------------------------------------------------------------------- #
#
# developers.google.com/knowledge/api, from a live run. Three lines of one log,
# minutes apart, disagreeing with each other:
#
#   SKIP  endpoint       no MCP endpoint answered on 166 probed path(s);
#                        no native endpoint answered
#   SKIP  bridge         a native MCP endpoint exists at
#                        https://developerknowledge.googleapis.com/mcp
#   OK    due diligence  4/4 checks; native endpoint found at
#                        https://developerknowledge.googleapis.com/mcp
#
# The registry publishes com.googleapis.developerknowledge/mcp with remotes[]
# only. Its URL is on a sibling domain, so the host filter dropped it before it
# could be probed -- while step D recorded it anyway, which closed tiers 2, 3
# and 4. The address finished with nothing registered, having refused every
# alternative on the strength of an endpoint nobody wrote down.


def test_a_registry_remote_on_another_host_is_probed_not_dropped(ctx) -> None:
    """Google's own MCP endpoint for developers.google.com."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    remote = Candidate(
        kind="url",
        name="com.googleapis.developerknowledge/mcp",
        url="https://developerknowledge.googleapis.com/mcp",
        transport=Transport.STREAMABLE_HTTP,
    )
    pipeline.registry.queries_for = lambda target: ["developerknowledge"]  # type: ignore[assignment]
    pipeline.registry.search_official = lambda query: [  # type: ignore[assignment]
        type("Hit", (), {"name": "com.googleapis.developerknowledge/mcp"})()
    ]
    pipeline.registry.to_candidates = lambda hit: [remote]  # type: ignore[assignment]

    found, note = pipeline._registry_remotes("https://developers.google.com/knowledge/api")

    assert [c.url for c in found] == ["https://developerknowledge.googleapis.com/mcp"]
    assert "on another host" in note
    assert found[0].rank == 3, "probed, but after the target's own paths"


def test_an_on_host_remote_still_outranks_one_elsewhere(ctx) -> None:
    """Better evidence wins when both answer."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    here = Candidate(kind="url", name="a", url="https://mcp.example.test/mcp", transport=Transport.STREAMABLE_HTTP)
    there = Candidate(kind="url", name="b", url="https://api.elsewhere.test/mcp", transport=Transport.STREAMABLE_HTTP)
    pipeline.registry.queries_for = lambda target: ["example"]  # type: ignore[assignment]
    pipeline.registry.search_official = lambda query: [type("Hit", (), {"name": "h"})()]  # type: ignore[assignment]
    pipeline.registry.to_candidates = lambda hit: [there, here]  # type: ignore[assignment]

    found, _ = pipeline._registry_remotes("https://example.test")
    ranks = {c.url: c.rank for c in found}
    assert ranks["https://mcp.example.test/mcp"] < ranks["https://api.elsewhere.test/mcp"]


def test_how_many_other_hosts_one_registry_answer_may_send_us_to_is_capped(ctx) -> None:
    """Somebody else's data must not be able to aim unbounded requests."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.settings.set("mcp.discovery.registry_remote_other_hosts", 2)
    pipeline = TraditionalPipeline(ctx)
    many = [
        Candidate(kind="url", name=f"n{i}", url=f"https://other{i}.test/mcp", transport=Transport.STREAMABLE_HTTP)
        for i in range(6)
    ]
    pipeline.registry.queries_for = lambda target: ["example"]  # type: ignore[assignment]
    pipeline.registry.search_official = lambda query: [type("Hit", (), {"name": "h"})()]  # type: ignore[assignment]
    pipeline.registry.to_candidates = lambda hit: many  # type: ignore[assignment]

    found, _ = pipeline._registry_remotes("https://example.test")
    assert len(found) == 2


def test_an_endpoint_the_checklist_believes_in_is_probed_before_giving_up(ctx) -> None:
    """The contradiction is now the trigger, whatever route the URL arrived by.

    Closing tiers 2-4 on the strength of an endpoint is only defensible if the
    endpoint then becomes something. If nothing registered it, the run probes it
    once more rather than finishing empty and silent.
    """
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://developers.google.com/knowledge/api")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    outcome.due_diligence.native_endpoints.append("https://developerknowledge.googleapis.com/mcp")

    seen: list[str] = []

    def answer(candidates, **kw):
        for candidate in candidates:
            seen.append(candidate.url)
            # What makes it this target's, and so registrable: the server names
            # the target in what it advertises. Without that the writer would
            # refuse it -- see the next test but one.
            candidate.handshake = Handshake(
                ok=True,
                tools=["a"],
                round_trip=True,
                advertised=["search https://developers.google.com/knowledge documents"],
            )
        return list(candidates)

    pipeline.verifier.verify_all = answer  # type: ignore[assignment]
    pipeline.reconcile(outcome)

    assert seen == ["https://developerknowledge.googleapis.com/mcp"]
    assert [c.url for c in outcome.verified] == ["https://developerknowledge.googleapis.com/mcp"]


def test_an_endpoint_it_recovers_must_be_registrable_to_count(ctx) -> None:
    """ "Recovered" used to mean "answered", and the writer then refused it.

    mcp.0dteconfluence.com was reported as recovered for confluence.example.com and
    refused at registration a moment later. It is now judged before it is
    absorbed, and ruled out of the checklist with the reason.
    """
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Status, TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://confluence.example.com")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    outcome.due_diligence.note("native_endpoints", ["https://mcp.0dteconfluence.com/mcp"])

    def answer(candidates, **kw):
        for candidate in candidates:
            candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
        return list(candidates)

    pipeline.verifier.verify_all = answer  # type: ignore[assignment]
    pipeline.reconcile(outcome)

    assert outcome.verified == []
    assert outcome.due_diligence.native_endpoints == []
    assert "https://mcp.0dteconfluence.com/mcp" in outcome.due_diligence.ruled_out
    stage = outcome.stages[-1]
    assert stage.status == Status.PARTIAL
    assert "recovered" not in stage.detail


def test_and_if_it_still_does_not_answer_the_contradiction_is_reported(ctx) -> None:
    """ "Found nothing" is not the whole story when every tier was refused."""
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://x.example.test")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    outcome.due_diligence.native_endpoints.append("https://api.example.test/mcp")
    pipeline.verifier.verify_all = lambda candidates, **kw: list(candidates)  # type: ignore[assignment]

    pipeline.reconcile(outcome)

    stage = outcome.stages[-1]
    assert "tiers 2-4 were refused on that basis" in stage.detail
    assert any("forbid_lower_tiers_when_native=false" in r for r in stage.remediation)


def test_a_target_that_found_something_is_left_alone(ctx) -> None:
    """The check only fires on the disagreement, not on every target."""
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target="https://x.example.test")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    outcome.due_diligence.native_endpoints.append("https://x.example.test/mcp")
    outcome.verified.append(
        Candidate(
            kind="url",
            name="x",
            url="https://x.example.test/mcp",
            transport=Transport.STREAMABLE_HTTP,
            handshake=Handshake(ok=True, tools=["a"], round_trip=True),
        )
    )
    before = len(outcome.stages)
    pipeline.verifier.verify_all = lambda *a, **k: pytest.fail("must not probe")  # type: ignore[assignment]

    pipeline.reconcile(outcome)
    assert len(outcome.stages) == before


# --------------------------------------------------------------------------- #
# the working server refused at the last step
# --------------------------------------------------------------------------- #
#
# developers.google.com/knowledge/api, third failed run. This time discovery
# succeeded and registration threw the result away:
#
#   OK    bridge   developerknowledge-googleapis-com-gcloud-adc --
#                  StatelessServer ESF via stdio, protocol 2026-07-28, 3 tool(s)
#   SKIP  refused  not registered -- this entry names bridge.js rather than
#                  developers.google.com
#
# A verified server with three tools, refused for carrying no address -- which
# is one environment variable's worth of repair that the agent, with two
# attempts left, was never told about. The grounding verdict was computed after
# every stage had finished, so it could only ever veto.


def google_bridge(env: dict[str, str] | None = None) -> Candidate:
    """The agent's bridge, as the run built it."""
    return Candidate(
        kind="stdio",
        name="developerknowledge-googleapis-com-gcloud-adc",
        transport=Transport.STDIO,
        command=["/usr/bin/node", "/root/.local/state/ica/artifacts/bridge.js"],
        env=env or {},
        handshake=Handshake(ok=True, tools=["a", "b", "c"], round_trip=True),
    )


def google_outcome():
    """A target whose endpoint stage recorded the vendor host answering 401."""
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    target = "https://developers.google.com/knowledge/api"
    outcome = TargetOutcome(target=target)
    outcome.due_diligence = DueDiligence(target=target)
    outcome.due_diligence.auth_walls.append("https://developerknowledge.googleapis.com/mcp")
    return outcome


def test_a_bridge_relaying_to_the_vendor_host_is_believed() -> None:
    """The run's own finding is the evidence, not the spelling of a hostname.

    Requiring the *target's* hostname from a launcher refused a bridge that
    relays to the host the same run had just found answering as that target's
    MCP endpoint.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import (
        SERVED_ELSEWHERE,
        assess,
        endpoint_hosts,
    )

    outcome = google_outcome()
    candidate = google_bridge({"MCP_BRIDGE_BASE_URL": "https://developerknowledge.googleapis.com"})
    verdict = assess(candidate, outcome.target, served_by=endpoint_hosts(outcome))
    assert verdict.kind == SERVED_ELSEWHERE
    assert verdict.served_by == "developerknowledge.googleapis.com"


def test_but_a_bridge_naming_nothing_is_still_refused() -> None:
    """The floor holds: an entry nobody can check is not registrable."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess, endpoint_hosts

    outcome = google_outcome()
    verdict = assess(google_bridge(), outcome.target, served_by=endpoint_hosts(outcome))
    assert verdict.kind == UNGROUNDED
    assert "no address at all" in verdict.reason, "and it says which refusal this is"


def test_a_filename_is_not_an_address() -> None:
    """The refusal said "names bridge.js rather than developers.google.com".

    That describes an entry pointing somewhere else, when it pointed nowhere --
    and the two need different remedies.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import addresses_in

    assert addresses_in(google_bridge()) == []
    assert addresses_in(google_bridge({"MCP_BRIDGE_BASE_URL": "https://developerknowledge.googleapis.com"})) == [
        "https://developerknowledge.googleapis.com"
    ]


def test_an_unregistrable_candidate_does_not_settle_the_stage(ctx) -> None:
    """The fix that generalises: the verdict becomes feedback, not a veto.

    Checked at ingest, so a stage cannot be settled by something that will be
    thrown away later -- and the agent still has attempts left to fix it.
    """
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome

    stage = StageOutcome(stage=Stage.BRIDGE)
    kept = AgenticPipeline(ctx)._drop_ungrounded(stage, [google_bridge()], google_outcome())

    assert kept == [], "a candidate that cannot be registered is not a result"
    assert stage.findings["not_registrable"], "and the reason is recorded"
    assert "no address at all" in stage.findings["not_registrable"][0]


def test_and_the_reason_reaches_the_next_attempt(ctx) -> None:
    """Told where a retry reads, which is the whole point of moving the check."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import _correction
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome

    stage = StageOutcome(stage=Stage.BRIDGE)
    stage.candidates = [google_bridge()]
    stage.findings["not_registrable"] = ["the bridge: this entry names no address at all"]

    text = _correction(stage, attempt=1)
    assert "could not be registered" in text
    assert "Put the target's address in the entry" in text


def test_reconcile_fires_when_everything_found_was_refused(ctx) -> None:
    """ "Found something" has to mean "found something that can be written".

    The bridge verified, so the old guard treated the address as settled -- and
    the check meant to catch an address ending up with nothing never ran.
    """
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    outcome = google_outcome()
    outcome.verified.append(google_bridge())  # verified, but unregistrable
    probed: list[str] = []

    def answer(candidates, **kw):
        probed.extend(c.url for c in candidates)
        return []

    pipeline.verifier.verify_all = answer  # type: ignore[assignment]
    pipeline.reconcile(outcome)
    assert probed == ["https://developerknowledge.googleapis.com/mcp"]


def test_a_link_in_an_api_error_is_not_a_login_page(ctx) -> None:
    """`<form` and `<a href` were markers of HTML, not of a login page.

    Any API whose error body carries a link would have had its evidence
    cleared -- a silent way to lose a real endpoint, which is the failure this
    list exists to prevent in the other direction.
    """
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    class Reply:
        headers = {"content-type": "application/json"}
        content_type = "application/json"
        text = '{"error":"no api key"} <a href="/docs">docs</a>'

    result = Handshake(status=401, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, Reply())
    assert result.auth_evidence, "a JSON refusal with a documentation link is still an endpoint"


# --------------------------------------------------------------------------- #
# build.opensuse.org and build.example.net: two tiers spent on the wrong package
# --------------------------------------------------------------------------- #
#
#   OK    package  mcp verified (npm search hit (@ateam-ai/mcp))
#   SKIP  bridge   the package stage already answered; lower tiers are not searched
#   SKIP  strategy the package stage already answered; lower tiers are not searched
#   SKIP  refused  build-opensuse-org  not registered -- names no address at all
#
# `@ateam-ai/mcp` is an "AI Team Solution Platform" with nothing to do with Open
# Build Service, and it names no address -- so the refusal was right. What was
# wrong is that it was allowed to *settle* tier 2 first, which closed the bridge
# stage that was the real answer for build.example.net. And the correct server for
# build.opensuse.org, openSUSE's own `osc-mcp`, had already been thrown away for
# pointing at api.opensuse.org rather than build.opensuse.org.


def obs_target(host: str):
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    outcome = TargetOutcome(target=f"https://{host}")
    outcome.due_diligence = DueDiligence(target=outcome.target)
    return outcome


def test_openSUSEs_own_osc_mcp_is_believed_for_build_opensuse_org() -> None:
    """`api.opensuse.org` is OBS's API host -- the same organisation, not a stranger.

    Refused twice in one run for not being spelled `build.opensuse.org`, which
    is a claim about hostnames rather than about who operates the machine.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import SERVED_ELSEWHERE, assess

    candidate = Candidate(
        kind="stdio",
        name="osc-mcp",
        transport=Transport.STDIO,
        command=["osc-mcp"],
        env={"OBS_API_URL": "https://api.opensuse.org"},
        handshake=Handshake(ok=True, tools=["a"], round_trip=True),
    )
    verdict = assess(candidate, "https://build.opensuse.org")
    assert verdict.kind == SERVED_ELSEWHERE
    assert verdict.served_by == "api.opensuse.org"


def test_a_stranger_on_an_unrelated_domain_is_not() -> None:
    """The same shape, and the reason "same organisation" is not "any host"."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = Candidate(
        kind="stdio",
        name="mcp-atlassian",
        transport=Transport.STDIO,
        command=["uvx", "mcp-atlassian"],
        env={"JIRA_URL": "https://jira.elsewhere.test"},
        source="npm search hit",
        handshake=Handshake(ok=True, tools=["a"], round_trip=True),
    )
    assert assess(candidate, "https://jira.example.com").kind == UNGROUNDED


def test_a_multi_tenant_host_has_to_be_told_which_instance() -> None:
    """`bugzilla.fastmcp.app` will serve anybody's Bugzilla. Which one?

    It answers, it is a real MCP server, and a keyword search is all that ever
    connected it to this target. Without `bugzilla_url` the entry says nothing
    about whose bugs it returns.
    """
    from initialize_claude_ai.mcp_auto_discovery.grounding import (
        SERVED_ELSEWHERE,
        UNGROUNDED,
        assess,
    )

    works = Handshake(ok=True, tools=["a"], round_trip=True)
    anonymous = Candidate(
        kind="url",
        name="bugzilla-fastmcp-app",
        url="https://bugzilla.fastmcp.app/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=works,
        source="npm search hit",
    )
    assert assess(anonymous, "https://bugzilla.opensuse.org").kind == UNGROUNDED

    configured = Candidate(
        kind="url",
        name="bugzilla-fastmcp-app",
        url="https://bugzilla.fastmcp.app/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=works,
        source="npm search hit",
        headers={"bugzilla_url": "https://bugzilla.opensuse.org"},
    )
    assert assess(configured, "https://bugzilla.opensuse.org").kind == SERVED_ELSEWHERE


def test_a_stage_does_not_settle_on_something_that_cannot_be_registered(ctx) -> None:
    """The fix that saves the lower tiers, in the deterministic pipeline.

    `@ateam-ai/mcp` verified, so tier 2 reported OK and tiers 3 and 4 were
    skipped -- and then it was refused at publish. One unchecked candidate cost
    two tiers and the whole address.
    """
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = obs_target("build.opensuse.org")
    ateam = Candidate(
        kind="stdio",
        name="mcp",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ateam-ai/mcp"],
        source="npm search hit (@ateam-ai/mcp)",
        handshake=Handshake(ok=True, tools=["a"], round_trip=True),
    )
    stage = StageOutcome(stage=Stage.PACKAGE, status=Status.OK)
    stage.candidates, stage.verified = [ateam], [ateam]

    pipeline.discard_unregistrable(stage, outcome)

    assert stage.verified == [], "an unregistrable candidate is not an answer"
    assert stage.status == Status.PARTIAL, "so the stage did not settle, and tier 3 still runs"
    assert "names no address at all" in stage.findings["not_registrable"][0]


def test_and_the_tiers_below_are_then_not_closed(ctx) -> None:
    """The consequence that matters: build.example.net needed the bridge stage."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome

    pipeline = TraditionalPipeline(ctx)
    outcome = obs_target("build.example.net")
    ateam = Candidate(
        kind="stdio",
        name="mcp",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ateam-ai/mcp"],
        handshake=Handshake(ok=True, tools=["a"], round_trip=True),
    )
    stage = StageOutcome(stage=Stage.PACKAGE, status=Status.OK)
    stage.candidates, stage.verified = [ateam], [ateam]
    pipeline.discard_unregistrable(stage, outcome)
    outcome.stages.append(stage)
    outcome.absorb(stage, pipeline.provisional_limit(exhaustive=False))

    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is None


def test_an_agents_package_is_checked_for_relevance_too(ctx) -> None:
    """The gap that let `@ateam-ai/mcp` become tier 2's answer at all.

    `_relevant_only` lived in the deterministic pipeline, so a package an agent
    proposed was never asked whether it had anything to do with the target --
    one rule, enforced on one path.
    """
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome

    pipeline = AgenticPipeline(ctx)
    outcome = obs_target("build.opensuse.org")
    stage = StageOutcome(stage=Stage.PACKAGE)
    ateam = Candidate(
        kind="stdio",
        name="@ateam-ai/mcp",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ateam-ai/mcp"],
        source="npm search hit (@ateam-ai/mcp)",
    )
    kept = pipeline.fallback._relevant_only([ateam], outcome, stage)
    assert kept == []
    assert stage.findings["rejected_as_unrelated"] == ["@ateam-ai/mcp"]


# --------------------------------------------------------------------------- #
# a crash is not a credential wall
# --------------------------------------------------------------------------- #
#
#   ▲ PARTIAL build.example.net :: package  openapi-bridge -- process exited with
#             rc=1: at async ToolsManager.initialize (file:///root/.npm/_npx/1…
#   ▲ PARTIAL build.example.net :: verification  1 endpoint(s) answered only to say
#             a credential is required, and is written out with the slot empty
#   ↻ CHANGED register build-example-net  recorded in ./.mcp.json
#
# A Node stack trace became "needs a token". `credential_pending` was set
# whenever a secret was missing, whether or not the handshake succeeded -- so a
# process that died on startup became provisional, settled the stage, closed
# tiers 3 and 4, and wrote `"BUILD_EXAMPLE_NET_API_HEADERS": "${...}"` into the file.
# Exporting that variable cannot help: the process dies before reading it.
# smelt.example.net went the same way, and both entries were disconnected.


def crashing_package(name: str) -> Candidate:
    return Candidate(
        kind="stdio",
        name=name,
        transport=Transport.STDIO,
        command=["npx", "-y", "@ivotoby/openapi-mcp-server@1.16.1"],
        env={"API_BASE_URL": "https://build.example.net"},
        secret_env={"API_HEADERS": "env:API_HEADERS"},
    )


def launchable(ctx):
    """A verifier allowed to launch, with no secret resolvable.

    The fixture context is a dry run and has no `npx`, so `_verify_stdio` would
    return before it ever computed `pending` -- and a test that never reaches
    the code it names proves nothing.
    """
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    ctx.runner.which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    ctx.secrets.resolve = lambda *a, **k: None  # type: ignore[assignment]
    return CandidateVerifier(ctx)


def test_a_process_that_died_is_not_an_endpoint_waiting_for_a_token(ctx) -> None:
    verifier = launchable(ctx)
    verifier.prober.probe_stdio = lambda *a, **k: Handshake(  # type: ignore[assignment]
        ok=False,
        transport=Transport.STDIO,
        error="process exited with rc=1: at async ToolsManager.initialize",
    )
    shake = verifier._verify_stdio(crashing_package("openapi-bridge"))
    assert shake.ok is False
    assert not shake.credential_pending, "a crash was recorded as a credential wall"
    assert any("not a credential wall" in h for h in shake.protocol_hints)


def test_but_one_that_died_saying_which_variable_is(ctx) -> None:
    """gitlab.example.net: a crash that names the withheld variable is a wall.

    The test above is right that a crash is not evidence of a credential wall,
    but it cannot be the whole rule, because a server is also entitled to read
    its configuration, find the variable absent and decline to start. That is
    the *strongest* statement that the credential is the missing piece, and
    filing it as breakage cost gitlab.example.net a bridge that had already been
    built, pinned at the target and verified end-to-end.

    The line between them is what the server said -- so both of these tests
    feed the same code path and differ only in the message.
    """
    verifier = launchable(ctx)
    verifier.prober.probe_stdio = lambda *a, **k: Handshake(  # type: ignore[assignment]
        ok=False,
        transport=Transport.STDIO,
        error="process exited with rc=1: RuntimeError: API_HEADERS is required",
    )
    shake = verifier._verify_stdio(crashing_package("gitlab-bridge"))
    assert shake.ok is False, "it did not handshake and must not be said to have"
    assert shake.credential_pending == ["API_HEADERS"]
    assert any("refused to start" in h for h in shake.protocol_hints)


def test_but_one_that_handshaked_without_its_token_still_is(ctx) -> None:
    """confluence-datacenter-mcp: the case trial_without_secrets exists for."""
    verifier = launchable(ctx)
    verifier.prober.probe_stdio = lambda *a, **k: Handshake(  # type: ignore[assignment]
        ok=True,
        transport=Transport.STDIO,
        tools=["a", "b"],
        round_trip=True,
    )
    shake = verifier._verify_stdio(crashing_package("confluence-datacenter-mcp"))
    assert shake.credential_pending == ["API_HEADERS"]


def test_reject_unauthorized_is_stripped_like_its_longer_cousin(ctx) -> None:
    """`REJECT_UNAUTHORIZED: "false"` reached a real entry; the list knew only
    the NODE_TLS_ spelling. A pattern catches the next one nobody has met."""
    candidate = Candidate(
        kind="stdio",
        name="build-example-net",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ivotoby/openapi-mcp-server@1.16.1"],
        env={"API_BASE_URL": "https://build.example.net", "REJECT_UNAUTHORIZED": "false"},
    )
    entry = McpWriter(ctx).entry_for(candidate, "build-example-net")
    assert "REJECT_UNAUTHORIZED" not in entry.get("env", {})


def test_a_generic_credential_variable_is_namespaced(ctx) -> None:
    """`"HEADERS": "${HEADERS}"` collides with every other server in the file.

    The key stays what the server reads; only the variable an operator exports
    is ours to name.
    """
    candidate = Candidate(
        kind="stdio",
        name="smelt",
        transport=Transport.STDIO,
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"ENDPOINT": "https://smelt.example.net/graphql/"},
        secret_env={"HEADERS": "env:HEADERS"},
    )
    entry = McpWriter(ctx).entry_for(candidate, "smelt-example-net")
    # Namespaced -- and, mcp-graphql taking its credential inside a JSON
    # object (credential_env), written in that form around the credential
    # itself, never as a variable the operator would fill with the whole object.
    assert entry["env"]["HEADERS"] == '{"Authorization": "Bearer ${SMELT_EXAMPLE_NET_TOKEN}"}'


def test_but_a_documented_vendor_variable_keeps_its_name(ctx) -> None:
    """Renaming GITHUB_PERSONAL_ACCESS_TOKEN would surprise everyone who exports it."""
    candidate = Candidate(
        kind="stdio",
        name="github",
        transport=Transport.STDIO,
        command=["npx", "-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_HOST": "https://github.com"},
        secret_env={"GITHUB_PERSONAL_ACCESS_TOKEN": "env:GITHUB_PERSONAL_ACCESS_TOKEN"},
    )
    entry = McpWriter(ctx).entry_for(candidate, "github-com")
    assert entry["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"


# --------------------------------------------------------------------------- #
# a question asked before its answer can exist
# --------------------------------------------------------------------------- #
#
#   developers.google.com: developerknowledge-googleapis-com cannot be registered
#   -- the entry points at developerknowledge.googleapis.com, which is not
#   developers.google.com, and which never answered as an MCP endpoint
#
# Three times in one run for Google's own endpoint, once for GitHub's. None of
# them had been probed: `_drop_ungrounded` runs at agentic.py:819 and
# `verify_all` at :837, so `answered_as_mcp` was false for *everything* at
# ingest, and every off-host URL was refused before it could be dialled. The
# provenance and same-organisation clauses that exist to save exactly these sit
# below that early return and were never reached.
#
# Every existing test attached a handshake before calling `assess`, which is why
# none of them caught it. These go through the real ingest path.


def unprobed_vendor_endpoint(url: str, source: str = "") -> Candidate:
    """As the endpoint agent hands it over: no handshake yet."""
    return Candidate(
        kind="url",
        name="vendor",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        origin=Stage.ENDPOINT,
        source=source,
    )


@pytest.mark.parametrize(
    ("target", "endpoint"),
    [
        ("https://developers.google.com/knowledge/api", "https://developerknowledge.googleapis.com/mcp"),
        ("https://github.com", "https://api.githubcopilot.com/mcp/"),
    ],
)
def test_an_unprobed_endpoint_survives_ingest(ctx, target, endpoint) -> None:
    """It has not answered because nothing has asked it yet."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome

    outcome = TargetOutcome(target=target)
    outcome.due_diligence = DueDiligence(target=target)
    stage = StageOutcome(stage=Stage.ENDPOINT)
    candidate = unprobed_vendor_endpoint(endpoint)

    kept = AgenticPipeline(ctx)._drop_ungrounded(stage, [candidate], outcome)
    assert kept == [candidate], "refused before it was ever dialled"
    assert "not_registrable" not in stage.findings


def test_and_is_judged_properly_once_it_has(ctx) -> None:
    """The real verdict is taken at publish, where the handshake exists."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import SERVED_ELSEWHERE, UNGROUNDED, assess

    endpoint = "https://api.githubcopilot.com/mcp/"
    silent = unprobed_vendor_endpoint(endpoint)
    silent.handshake = Handshake(ok=False, status=404)
    assert assess(silent, "https://github.com").kind == UNGROUNDED

    answered = unprobed_vendor_endpoint(endpoint, source="named in the target's own documentation")
    answered.handshake = Handshake(ok=False, status=401, requires_auth=True, auth_evidence=["json, not a web page"])
    assert assess(answered, "https://github.com").kind == SERVED_ELSEWHERE


def test_a_launcher_is_judged_the_same_before_and_after_probing(ctx) -> None:
    """Nothing about running a process says where it connects, so probing
    changes nothing -- and the useful ingest feedback stays available."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = Candidate(
        kind="stdio",
        name="c",
        transport=Transport.STDIO,
        command=["npx", "-y", "@kubb/mcp"],
    )
    before = assess(candidate, "https://confluence.example.com", probed=False)
    candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    after = assess(candidate, "https://confluence.example.com", probed=True)
    assert before.kind == after.kind == UNGROUNDED
    assert "no address at all" in before.reason


def test_an_unprobed_bare_origin_is_not_refused_as_a_front_door(ctx) -> None:
    """ "It never handshaked" is not known before anything tried to."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import UNGROUNDED, assess

    candidate = unprobed_vendor_endpoint("https://confluence.example.com")
    assert assess(candidate, "https://confluence.example.com", probed=False).registrable
    candidate.handshake = Handshake(ok=False, status=401, requires_auth=True)
    assert assess(candidate, "https://confluence.example.com").kind == UNGROUNDED
