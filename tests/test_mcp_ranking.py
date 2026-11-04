"""What phase 4 comes back with: one endpoint, the best one, or all of them.

``--the-best-mcp`` turns "every server that answered is registered" into "one of
them is", which is only defensible if "best" is measured rather than asserted.
Most of these tests pin each aspect to the evidence it claims to come from, pin
the trade-off to the weights in ``[mcp.ranking]`` (an operator who retunes them
must get a different winner, or the settings are decoration), and pin the two
things the phase does around the ranking: widening the search first, because
there is no best of one, and keeping the losers in the report afterwards,
because a choice nobody can inspect is not one anyone can argue with.

The last section covers the opposite flag, ``--exhaustive-mcp``, which is here
rather than in a file of its own because it widens the search through the same
code path and is only meaningful next to what it is the opposite of.
"""

from __future__ import annotations

from typing import Mapping

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.report import PhaseResult
from initialize_claude_ai.mcp_auto_discovery import pathgen
from initialize_claude_ai.mcp_auto_discovery.address import configure_local_zones
from initialize_claude_ai.mcp_auto_discovery.ranking import ASPECTS, CandidateRanker
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase


def handshake(**overrides) -> Handshake:
    """A healthy initialize reply, so a test only states what it is about."""
    fields = {
        "ok": True,
        "transport": Transport.STREAMABLE_HTTP,
        "protocol_version": "2026-07-28",
        "server_name": "example",
        "server_version": "1.0",
        "instructions": "call the tools",
        "capabilities": {"tools": {}, "resources": {}, "prompts": {}, "logging": {}},
        "tools": [f"tool{i}" for i in range(12)],
        "elapsed": 0.2,
    }
    fields.update(overrides)
    return Handshake(**fields)


def url_candidate(url: str, **overrides) -> Candidate:
    fields = {
        "kind": "url",
        "url": url,
        "transport": Transport.STREAMABLE_HTTP,
        "origin": Stage.ENDPOINT,
        "handshake": handshake(url=url),
    }
    fields.update(overrides)
    return Candidate(**fields)


def ranker(ctx, monkeypatch, prober=None) -> CandidateRanker:
    """A ranker that never resolves a credential to score something."""
    monkeypatch.setattr(CandidateRanker, "_auth", lambda self: {})
    return CandidateRanker(ctx, prober=prober)


class FakeProber:
    """Handshakes on tap, plus a record of what was asked for."""

    def __init__(self, replies: Mapping[str, Handshake] | None = None) -> None:
        self.replies = dict(replies or {})
        self.calls: list[str] = []

    def probe_http(self, url: str, *, headers=None, timeout=None) -> Handshake:
        self.calls.append(url)
        return self.replies.get(url, handshake(url=url))

    probe_sse = probe_http

    def accepts(self, hs: Handshake) -> tuple[bool, str]:
        return hs.ok, "looks like MCP"


def order(scores) -> list[str]:
    return [score.label for score in scores]


# ----------------------------------------------------------------------------
# the aspects, one at a time
# ----------------------------------------------------------------------------


def test_https_beats_cleartext(ctx, monkeypatch) -> None:
    secure = url_candidate("https://example.com/mcp", name="tls")
    plain = url_candidate("http://example.com/mcp", name="cleartext")
    scores = ranker(ctx, monkeypatch).rank([plain, secure])
    assert order(scores) == ["tls", "cleartext"]
    by_label = {s.label: s for s in scores}
    assert by_label["tls"].aspects["security"] > by_label["cleartext"].aspects["security"]
    assert "cleartext HTTP" in by_label["cleartext"].reasons["security"]


def test_an_endpoint_found_only_with_verification_off_ranks_below_a_trusted_one(ctx, monkeypatch) -> None:
    """Step B retries without certificate verification, which is how a
    self-signed corporate host gets found at all. Finding one that way is a
    result and not a recommendation: the connection is encrypted but the peer
    was never authenticated, so it must not outrank a certificate that checks
    out."""
    trusted = url_candidate("https://example.com/mcp", name="trusted")
    bypassed = url_candidate(
        "https://internal.example.com/mcp",
        name="unverified",
        handshake=handshake(url="https://internal.example.com/mcp", insecure_tls=True),
    )
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([bypassed, trusted])}
    assert scores["trusted"].aspects["security"] > scores["unverified"].aspects["security"]
    assert "TLS certificate not verified" in scores["unverified"].reasons["security"]
    assert "TLS" in scores["trusted"].reasons["security"]


def test_loopback_without_tls_is_not_marked_down(ctx, monkeypatch) -> None:
    """Cleartext is penalised because of the wire, and there is no wire here."""
    local = url_candidate("http://127.0.0.1:8080/mcp", name="local")
    score = ranker(ctx, monkeypatch).rank([local])[0]
    assert score.aspects["security"] == 1.0
    assert "loopback" in score.reasons["security"]


def test_the_current_transport_beats_the_deprecated_one(ctx, monkeypatch) -> None:
    modern = url_candidate("https://example.com/mcp", name="streamable")
    legacy = url_candidate(
        "https://example.com/sse",
        name="legacy",
        transport=Transport.SSE,
        handshake=handshake(transport=Transport.SSE, url="https://example.com/sse"),
    )
    scores = ranker(ctx, monkeypatch).rank([legacy, modern])
    assert order(scores) == ["streamable", "legacy"]
    by_label = {s.label: s for s in scores}
    assert by_label["streamable"].aspects["protocol"] > by_label["legacy"].aspects["protocol"]


def test_an_older_protocol_version_scores_below_the_newest(ctx, monkeypatch) -> None:
    versions = ctx.settings.str_list("mcp.protocol.versions")
    newest = url_candidate("https://example.com/new", name="new")
    oldest = url_candidate(
        "https://example.com/old",
        name="old",
        handshake=handshake(protocol_version=versions[-1]),
    )
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([oldest, newest])}
    assert scores["new"].aspects["protocol"] > scores["old"].aspects["protocol"]
    assert versions[-1] in scores["old"].reasons["protocol"]


def test_a_stdio_server_with_installs_loses_to_a_plain_url(ctx, monkeypatch) -> None:
    remote = url_candidate("https://example.com/mcp", name="hosted")
    local = Candidate(
        kind="stdio",
        name="packaged",
        command=["npx", "-y", "example-mcp"],
        install=[["npm", "install", "-g", "example-mcp"]],
        secret_env={"EXAMPLE_TOKEN": "env:EXAMPLE_TOKEN"},
        transport=Transport.STDIO,
        origin=Stage.PACKAGE,
        handshake=handshake(transport=Transport.STDIO),
    )
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([local, remote])}
    assert order(ranker(ctx, monkeypatch).rank([local, remote])) == ["hosted", "packaged"]
    assert scores["packaged"].aspects["usability"] < scores["hosted"].aspects["usability"]
    assert scores["packaged"].aspects["configuration"] < scores["hosted"].aspects["configuration"]
    assert scores["packaged"].aspects["security"] < scores["hosted"].aspects["security"]
    assert "install step" in scores["packaged"].reasons["configuration"]
    assert "variable(s) to export" in scores["packaged"].reasons["configuration"]


def test_a_server_on_somebody_elses_host_is_marked_down(ctx, monkeypatch) -> None:
    own = url_candidate("https://example.com/mcp", name="first-party")
    other = url_candidate("https://gateway.vendor.net/mcp", name="third-party")
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([other, own], target="https://example.com")}
    assert scores["first-party"].aspects["security"] > scores["third-party"].aspects["security"]
    assert "not example.com" in scores["third-party"].reasons["security"]


def test_more_tools_score_higher(ctx, monkeypatch) -> None:
    rich = url_candidate("https://example.com/mcp", name="rich")
    sparse = url_candidate(
        "https://example.com/thin",
        name="sparse",
        handshake=handshake(tools=["only"], capabilities={"tools": {}}),
    )
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([sparse, rich])}
    assert scores["rich"].aspects["functionality"] > scores["sparse"].aspects["functionality"]
    assert "1 tool(s)" in scores["sparse"].reasons["functionality"]


def test_the_stage_that_found_it_is_part_of_the_score(ctx, monkeypatch) -> None:
    """A vendor's own endpoint outranks a bridge this tool wrote a minute ago."""
    official = url_candidate("https://example.com/mcp", name="official")
    improvised = url_candidate("https://example.com/bridge", name="bridge", origin=Stage.BRIDGE)
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([improvised, official])}
    assert scores["official"].aspects["provenance"] > scores["bridge"].aspects["provenance"]
    assert order(ranker(ctx, monkeypatch).rank([improvised, official])) == ["official", "bridge"]


# ----------------------------------------------------------------------------
# stability is measured, not assumed
# ----------------------------------------------------------------------------


def test_a_server_that_fails_the_repeat_handshake_drops(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    flaky_url = "https://example.com/flaky"
    prober = FakeProber({flaky_url: handshake(ok=False, status=503, error="temporarily down")})
    steady = url_candidate("https://example.com/mcp", name="steady")
    flaky = url_candidate(flaky_url, name="flaky")
    scores = {s.label: s for s in ranker(ctx, monkeypatch, prober).rank([flaky, steady])}
    assert scores["steady"].aspects["stability"] > scores["flaky"].aspects["stability"]
    assert "1/3 handshake(s)" in scores["flaky"].reasons["stability"]
    assert "3/3 handshake(s)" in scores["steady"].reasons["stability"]


def test_a_slow_server_scores_below_a_quick_one(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    slow_url = "https://example.com/slow"
    ceiling = ctx.settings.float_("mcp.ranking.latency_ceiling_seconds", 8.0)
    prober = FakeProber({slow_url: handshake(elapsed=ceiling)})
    quick = url_candidate("https://example.com/mcp", name="quick")
    slow = url_candidate(slow_url, name="slow", handshake=handshake(elapsed=ceiling))
    scores = {s.label: s for s in ranker(ctx, monkeypatch, prober).rank([slow, quick])}
    assert scores["quick"].aspects["stability"] > scores["slow"].aspects["stability"]


def test_a_local_server_is_never_relaunched_to_collect_a_statistic(ctx, monkeypatch) -> None:
    """Starting third-party code is a mutating act; twice more is not a measurement
    worth making on somebody's machine."""
    ctx.dry_run = False
    prober = FakeProber()
    local = Candidate(
        kind="stdio",
        name="packaged",
        command=["npx", "-y", "example-mcp"],
        transport=Transport.STDIO,
        origin=Stage.PACKAGE,
        handshake=handshake(transport=Transport.STDIO),
    )
    score = ranker(ctx, monkeypatch, prober).rank([local])[0]
    assert prober.calls == []
    assert "measured once" in score.reasons["stability"]


def test_a_dry_run_measures_nothing(ctx, monkeypatch) -> None:
    prober = FakeProber()
    ranker(ctx, monkeypatch, prober).rank([url_candidate("https://example.com/mcp")])
    assert prober.calls == []


# ----------------------------------------------------------------------------
# the weights are the trade-off, and they are the operator's
# ----------------------------------------------------------------------------


def test_the_weights_decide_which_aspect_wins(ctx, monkeypatch) -> None:
    """The same two servers, two settings files, two winners."""
    plain_but_rich = url_candidate("http://example.com/mcp", name="rich-cleartext")
    secure_but_thin = url_candidate(
        "https://example.com/thin",
        name="thin-tls",
        handshake=handshake(tools=["only"], capabilities={"tools": {}}),
    )
    both = [plain_but_rich, secure_but_thin]

    ctx.settings.set("mcp.ranking.weights.security", 0.1)
    ctx.settings.set("mcp.ranking.weights.functionality", 20)
    assert order(ranker(ctx, monkeypatch).rank(both))[0] == "rich-cleartext"

    # A ranker snapshots the section when it is built, so the second one has to
    # be built after the change -- which is exactly how the phase uses it.
    ctx.settings.set("mcp.ranking.weights.security", 50)
    ctx.settings.set("mcp.ranking.weights.functionality", 0.1)
    assert order(ranker(ctx, monkeypatch).rank(both))[0] == "thin-tls"


def test_a_negative_weight_is_read_as_zero_not_as_an_inversion(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.ranking.weights.security", -5)
    plain = url_candidate("http://example.com/mcp", name="cleartext")
    secure = url_candidate("https://example.com/mcp", name="tls")
    scores = {s.label: s for s in ranker(ctx, monkeypatch).rank([plain, secure])}
    # Security still differs between them; it just no longer counts.
    assert scores["tls"].aspects["security"] > scores["cleartext"].aspects["security"]
    assert scores["tls"].total == pytest.approx(scores["cleartext"].total)


def test_a_weight_that_is_not_a_number_does_not_stop_the_ranking(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.ranking.weights.security", "very")
    score = ranker(ctx, monkeypatch).rank([url_candidate("https://example.com/mcp")])[0]
    assert 0.0 < score.total <= 1.0


# ----------------------------------------------------------------------------
# the result has to be reproducible and inspectable
# ----------------------------------------------------------------------------


def test_the_same_servers_always_produce_the_same_winner(ctx, monkeypatch) -> None:
    twins = [
        url_candidate("https://example.com/a", name="alpha"),
        url_candidate("https://example.com/b", name="beta"),
    ]
    forwards = order(ranker(ctx, monkeypatch).rank(twins))
    backwards = order(ranker(ctx, monkeypatch).rank(list(reversed(twins))))
    assert forwards == backwards == ["alpha", "beta"]


def test_a_runner_up_is_explained_by_where_it_lost(ctx, monkeypatch) -> None:
    """ "A URL, nothing to run locally" is true of the winner too, so it is no
    reason for setting this one aside."""
    thin = url_candidate(
        "https://example.com/thin",
        name="thin",
        handshake=handshake(tools=["only"], capabilities={"tools": {}}),
    )
    score = ranker(ctx, monkeypatch).rank([thin])[0]
    assert score.shortfall().startswith("functionality 0.")
    assert "1 tool(s)" in score.shortfall()
    assert "nothing to install or export" not in score.shortfall()


def test_a_flawless_candidate_has_nothing_to_confess(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.ranking.functionality_target", 1)
    ctx.settings.set("mcp.ranking.capability_target", 1)
    ctx.settings.set("mcp.ranking.stability_probes", 0)
    ctx.settings.set("mcp.ranking.latency_target_seconds", 5.0)
    perfect = url_candidate("https://example.com/mcp", name="perfect")
    score = ranker(ctx, monkeypatch).rank([perfect])[0]
    assert score.total == pytest.approx(1.0)
    assert score.shortfall() == ""


def test_the_headline_leads_with_what_the_weights_care_about(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.ranking.weights.security", 50)
    ctx.settings.set("mcp.ranking.weights.functionality", 0.1)
    score = ranker(ctx, monkeypatch).rank([url_candidate("https://example.com/mcp")])[0]
    assert score.headline(1) == score.reasons["security"]


def test_every_aspect_reaches_the_report(ctx, monkeypatch) -> None:
    score = ranker(ctx, monkeypatch).rank([url_candidate("https://example.com/mcp")])[0]
    payload = score.to_dict()
    assert set(payload["aspects"]) == set(ASPECTS)
    assert all(0.0 <= value <= 1.0 for value in payload["aspects"].values())
    assert payload["total"] == pytest.approx(round(score.total, 3))
    assert score.headline()


# ----------------------------------------------------------------------------
# what the phase does with it
# ----------------------------------------------------------------------------


def test_the_flag_widens_the_search_before_anything_reads_it(ctx) -> None:
    """Both stop-early settings are on by default for a good reason, and both
    make "the best" mean "the first", so the flag turns them off and says so."""
    ctx.settings.set("mcp.pick_best", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._widen_search(result)

    assert ctx.settings.bool_("mcp.exhaustive") is True
    assert ctx.settings.bool_("mcp.discovery.stop_at_first") is False
    step = next(s for s in result.steps if s.name == "selection")
    assert step.data["pick_best"] is True
    assert step.data["widened"]


def test_the_best_one_is_registered_and_the_rest_are_kept_on_the_record(ctx) -> None:
    ctx.settings.set("mcp.pick_best", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    outcome = TargetOutcome(
        target="https://example.com",
        base_url="https://example.com",
        verified=[
            url_candidate("http://example.com/mcp", name="cleartext"),
            url_candidate("https://example.com/mcp", name="tls"),
        ],
    )
    phase._choose_best(result, outcome)

    assert [c.label for c in outcome.verified] == ["tls"]
    assert [entry["label"] for entry in outcome.ranking] == ["tls", "cleartext"]
    assert all(set(entry["aspects"]) == set(ASPECTS) for entry in outcome.ranking)
    step = next(s for s in result.steps if "best of" in s.name)
    assert step.data["winner"]["label"] == "tls"
    assert [r["label"] for r in step.data["runners_up"]] == ["cleartext"]


def test_the_same_server_found_twice_is_one_server(ctx) -> None:
    """Widening the search means the same endpoint can come back twice; it must
    not be registered twice, nor occupy two rows of a comparison."""
    first = url_candidate("https://example.com/mcp", name="", origin=Stage.ENDPOINT)
    first.source = "path template /mcp"
    again = url_candidate("https://example.com/mcp/", name="", origin=Stage.BRIDGE)
    again.source = "generated bridge"
    kept = McpPhase(ctx)._deduplicate([first, again])

    assert kept == [first]
    assert any("found again by the bridge stage" in note for note in first.notes)
    assert any("generated bridge" in note for note in first.notes)


def test_two_different_servers_are_not_collapsed(ctx) -> None:
    one = url_candidate("https://example.com/mcp", name="one")
    two = url_candidate("https://example.com/other", name="two")
    assert McpPhase(ctx)._deduplicate([one, two]) == [one, two]


def test_one_server_is_not_dressed_up_as_a_decision(ctx) -> None:
    ctx.settings.set("mcp.pick_best", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    only = url_candidate("https://example.com/mcp", name="tls")
    outcome = TargetOutcome(target="https://example.com", verified=[only])
    phase._choose_best(result, outcome)

    assert outcome.verified == [only]
    assert outcome.ranking == []
    step = next(s for s in result.steps if "best of 1" in s.name)
    assert "only server that answered" in step.detail


def test_without_the_flag_nothing_is_set_aside(ctx) -> None:
    phase = McpPhase(ctx)
    assert phase._pick_best() is False
    assert phase._record_all() is False
    assert "the first server that completes a handshake" in phase._selection_fact()


def test_the_flag_asks_for_it_by_setting_and_not_by_flag_check(ctx) -> None:
    """The CLI states the intent; the phase decides what that costs."""
    args = cli.build_parser().parse_args(["run", "--phases", "4", "--the-best-mcp"])
    overrides = cli._overrides_from_args(args)
    assert "mcp.pick_best=true" in overrides
    # The widening is deliberately *not* a CLI override: it is phase 4's business.
    assert not any(o.startswith("mcp.exhaustive") for o in overrides)


def test_the_selection_is_announced_in_the_banner(ctx) -> None:
    ctx.settings.set("mcp.pick_best", True)
    facts = dict(McpPhase(ctx).banner_facts())
    assert "--the-best-mcp" in facts["Selection"]
    for aspect in ASPECTS:
        assert aspect in facts["Selection"]


# ----------------------------------------------------------------------------
# the search really does keep looking
# ----------------------------------------------------------------------------


def test_stop_at_first_off_keeps_probing_after_the_first_hit(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.discovery.stop_at_first", False)
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "prober", FakeProber())
    monkeypatch.setattr(pipeline.verifier, "auth_headers", lambda: {})
    entries = [
        pathgen.UrlCandidate(
            url=f"https://example.com/mcp{i}", transport=Transport.STREAMABLE_HTTP, reason="path template", rank=i
        )
        for i in range(4)
    ]
    run = pipeline._probe_concurrently(entries)
    verified = run.verified
    assert len(verified) == 4
    # Completion order is the network's; the report shows the likeliest path first.
    assert [c.confidence for c in verified] == sorted((c.confidence for c in verified), reverse=True)


def test_stop_at_first_on_is_still_the_default(ctx, monkeypatch) -> None:
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "prober", FakeProber())
    monkeypatch.setattr(pipeline.verifier, "auth_headers", lambda: {})
    entries = [
        pathgen.UrlCandidate(
            url=f"https://example.com/mcp{i}", transport=Transport.STREAMABLE_HTTP, reason="path template", rank=i
        )
        for i in range(4)
    ]
    run = pipeline._probe_concurrently(entries)
    assert len(run.verified) == 1


# ----------------------------------------------------------------------------
# --exhaustive-mcp: the other answer to the same question
# ----------------------------------------------------------------------------


def test_exhaustive_widens_the_search_too(ctx) -> None:
    """Recording every endpoint needs exactly the same widening as comparing
    them: both are asking the run not to stop at its first success."""
    ctx.settings.set("mcp.exhaustive", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    assert phase._record_all() is True
    phase._widen_search(result)

    assert ctx.settings.bool_("mcp.discovery.stop_at_first") is False
    step = next(s for s in result.steps if s.name == "selection")
    assert step.data["pick_best"] is False
    assert step.data["exhaustive"] is True
    assert "recording every working endpoint" in step.detail
    # Only the path setting had to change; the stage setting was already on.
    assert step.data["widened"] == ["every candidate path is probed, not just up to the first endpoint"]


def test_exhaustive_reports_what_it_found_before_registering_it(ctx) -> None:
    ctx.settings.set("mcp.exhaustive", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    outcome = TargetOutcome(
        target="https://example.com",
        verified=[
            url_candidate("https://example.com/mcp", name="modern"),
            url_candidate("https://example.com/sse", name="legacy", origin=Stage.BRIDGE),
        ],
    )
    phase._report_inventory(result, outcome)

    step = next(s for s in result.steps if "working endpoint" in s.name)
    assert step.name.endswith(":: 2 working endpoint(s)")
    assert step.data["endpoints"] == ["modern", "legacy"]
    assert step.data["origins"] == [Stage.ENDPOINT, Stage.BRIDGE]
    # Nothing is set aside: every one of them goes on to be registered.
    assert len(outcome.verified) == 2
    assert outcome.ranking == []


def test_exhaustive_says_so_even_when_one_endpoint_answered(ctx) -> None:
    """ "The search ran to the end" is the finding; a silent single line would be
    indistinguishable from an ordinary run that stopped at the first hit."""
    ctx.settings.set("mcp.exhaustive", True)
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    outcome = TargetOutcome(target="https://example.com", verified=[url_candidate("https://example.com/mcp")])
    phase._report_inventory(result, outcome)

    step = next(s for s in result.steps if "working endpoint" in s.name)
    assert "ran to the end anyway" in step.detail


def test_the_banner_says_which_of_the_three_this_run_is(ctx) -> None:
    default = McpPhase(ctx)._selection_fact()
    assert "the first server that completes a handshake" in default

    ctx.settings.set("mcp.exhaustive", True)
    assert "every working endpoint per address (--exhaustive-mcp)" in McpPhase(ctx)._selection_fact()

    ctx.settings.set("mcp.pick_best", True)
    assert "--the-best-mcp" in McpPhase(ctx)._selection_fact()


def test_exhaustive_mcp_sets_the_setting_it_names(ctx) -> None:
    args = cli.build_parser().parse_args(["run", "--phases", "4", "--exhaustive-mcp"])
    overrides = cli._overrides_from_args(args)
    assert "mcp.exhaustive=true" in overrides
    assert not any(o.startswith("mcp.pick_best") for o in overrides)
    # The path setting is phase 4's business, exactly as for --the-best-mcp.
    assert not any(o.startswith("mcp.discovery.stop_at_first") for o in overrides)


def test_recording_them_all_and_keeping_one_cannot_both_be_asked_for(ctx, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--phases", "4", "--exhaustive-mcp", "--the-best-mcp"])
    assert "--the-best-mcp" in capsys.readouterr().err


def test_the_old_spelling_still_lands_on_the_same_setting(ctx) -> None:
    """The flag is named --exhaustive-mcp everywhere now -- help, epilog, README
    -- because "run every stage" was a smaller promise than it keeps. The former
    spelling is not special-cased; it survives only as argparse's unambiguous
    prefix, which is why nobody's script breaks over the rename."""
    args = cli.build_parser().parse_args(["run", "--phases", "4", "--exhaustive"])
    assert "mcp.exhaustive=true" in cli._overrides_from_args(args)
    assert not hasattr(args, "exhaustive")


# ----------------------------------------------------------------------------
# cleartext on a network the operator says is theirs
# ----------------------------------------------------------------------------


def test_cleartext_on_a_declared_zone_is_not_marked_down(ctx, monkeypatch) -> None:
    """Penalising an internal appliance for not speaking TLS to its own subnet
    is this program inventing a finding out of its own ignorance of the zone."""
    plain = url_candidate("http://mcp.corp/mcp", name="internal")
    public = url_candidate("http://mcp.example.com/mcp", name="public")
    before = {s.label: s for s in ranker(ctx, monkeypatch).rank([plain, public])}
    assert "cleartext HTTP" in before["internal"].reasons["security"]

    configure_local_zones([".corp"])
    after = {s.label: s for s in ranker(ctx, monkeypatch).rank([plain, public])}
    assert "cleartext HTTP" not in after["internal"].reasons["security"]
    assert after["internal"].aspects["security"] > before["internal"].aspects["security"]
    assert after["public"].aspects["security"] == before["public"].aspects["security"]


def test_a_declared_zone_is_not_called_loopback(ctx, monkeypatch) -> None:
    """It is trusted, not local to this machine; the report should not overstate."""
    configure_local_zones([".corp"])
    scores = ranker(ctx, monkeypatch).rank([url_candidate("http://mcp.corp/mcp", name="internal")])
    assert "local network" in scores[0].reasons["security"]
    assert "loopback" not in scores[0].reasons["security"]


def test_an_endpoint_that_moved_inside_the_declared_zone_is_not_a_third_party(ctx, monkeypatch) -> None:
    configure_local_zones([".corp"])
    moved = url_candidate("https://gateway.corp/mcp", name="moved")
    scores = ranker(ctx, monkeypatch).rank([moved], target="https://mcp.corp")
    assert "not mcp.corp" not in scores[0].reasons["security"]
