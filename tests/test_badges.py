"""One table decides what every state looks like, on both output streams.

A run writes to two file descriptors at once -- the report to stdout, the log to
stderr -- and on an interactive terminal they arrive interleaved in a single
scrollback. Two renderings of "this went wrong" a few lines apart is a small
cost paid on every single run, so both come out of :data:`BADGES`, and these
tests are what stops them drifting apart again.

The property underneath all of them is that **nothing is said in colour alone**.
Every badge carries three things -- a sign, a word and a colour -- because the
colour is the first of the three to disappear: into a pipe, into a greyscale
screenshot, into a reader who does not separate red from green. So the tests
assert the word and the sign survive ``--no-color``, and that the colours, when
present, are the ones the scheme promises.
"""

from __future__ import annotations

import io
import logging

import pytest

from initialize_claude_ai.core.logging_setup import (
    BADGE_ROWS,
    BADGE_WIDTH,
    BADGES,
    GLYPH_SETS,
    HEADING_STYLE,
    LEVEL_ROLE,
    PALETTE_ALIASES,
    PALETTES,
    RUNNING,
    BadgeFormatter,
    Console,
    Transcript,
    glyph_for,
    setup_logging,
    styles_for,
)
from initialize_claude_ai.core.report import Status


@pytest.fixture
def transcript(tmp_path) -> Transcript:
    opened = Transcript.open(tmp_path / "claude_ai_initialization.log")
    assert opened is not None
    return opened


def plain(**kwargs) -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(stream=buffer, color=False, glyph_set="ascii", **kwargs), buffer


def coloured(palette: str = "basic", **kwargs) -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(stream=buffer, color=True, palette=palette, **kwargs), buffer


# ----------------------------------------------------------------------------
# the table itself
# ----------------------------------------------------------------------------


def test_every_status_a_step_can_end_in_has_a_badge() -> None:
    """A verdict with no entry would render as a bare word with no sign, which
    is exactly the line a reader would skim past."""
    for status in (Status.OK, Status.CHANGED, Status.SKIPPED, Status.PARTIAL, Status.FAILED):
        assert status in BADGES, f"{status} has no badge"


def test_every_log_level_maps_onto_a_badge() -> None:
    for level, role in LEVEL_ROLE.items():
        assert role in BADGES, f"{level} maps to {role!r}, which is not a badge"


def test_every_badge_names_a_glyph_both_sets_can_draw() -> None:
    """The ASCII set is not a subset with holes in it: a C-locale host gets the
    same report, in characters it can encode."""
    for role, mark in BADGES.items():
        for name, glyphs in GLYPH_SETS.items():
            assert mark.glyph in glyphs, f"{role} wants {mark.glyph!r}, missing from {name}"


def test_the_two_glyph_sets_carry_exactly_the_same_keys() -> None:
    assert set(GLYPH_SETS["unicode"]) == set(GLYPH_SETS["ascii"])


def test_every_badge_style_exists_in_every_palette() -> None:
    """Switching palette changes how loud a state is, never whether it has a
    colour at all."""
    for role, mark in BADGES.items():
        for name, codes in PALETTES.items():
            for style in mark.styles:
                assert style in codes, f"{role} wants {style!r}, missing from {name}"


def test_no_label_is_wider_than_the_column_it_is_centred_in() -> None:
    for role, mark in BADGES.items():
        assert len(mark.label) <= BADGE_WIDTH, f"{role} would push the message column"


def test_the_five_outcomes_are_told_apart_by_sign_as_well_as_by_colour() -> None:
    """Grey and dim-grey are one colour to a reader; a dot and a cross are not.

    OK/CHANGED and PARTIAL/WARN deliberately share a shape -- they are the same
    kind of news at different volumes -- but no two *outcomes* may collide.
    """
    outcomes = ("ok", "changed", "skipped", "partial", "failed")
    signs = {GLYPH_SETS["unicode"][BADGES[role].glyph] for role in outcomes}
    assert len(signs) == len(outcomes)
    words = {BADGES[role].label for role in outcomes}
    assert len(words) == len(outcomes)


def test_the_scheme_is_the_published_one_where_it_claims_to_be() -> None:
    """Green pass, red fail, amber caveat, cyan in flight, bold red error."""
    assert BADGES["ok"].styles == ("green",)
    assert BADGES["running"].styles == ("cyan",)
    assert BADGES["warning"].styles == ("yellow",)
    assert BADGES["error"].styles == ("red", "bold")
    assert BADGES["info"].styles == (), "information is the default case"
    assert BADGES["debug"].styles == ("dim",)
    assert HEADING_STYLE == ("blue", "bold")


def test_the_two_departures_from_it_are_the_documented_ones(ctx) -> None:
    """Skipped is grey rather than amber, and a failure is bold. Both are
    argued for in the code and in the README; pinning them here is what makes
    changing one of them a decision rather than an accident."""
    assert BADGES["skipped"].styles == ("dim",), "a skip is routinely the good answer"
    assert BADGES["partial"].styles == ("yellow",), "PARTIAL is this program's amber"
    assert "bold" in BADGES["failed"].styles


def test_styles_for_reads_the_same_table_rather_than_a_second_one() -> None:
    assert styles_for(Status.FAILED) == BADGES["failed"].styles
    assert styles_for("nothing like a status") == ()
    assert styles_for("nothing like a status", ("bold",)) == ("bold",)


# ----------------------------------------------------------------------------
# what a badge looks like
# ----------------------------------------------------------------------------


def test_a_badge_is_a_sign_then_a_word_in_a_fixed_column() -> None:
    console, _ = plain()
    assert console.badge("ok") == "+ [   OK   ]"
    assert console.badge("failed") == "x [  FAIL  ]"
    assert len(console.badge("ok")) == len(console.badge("partial"))


def test_the_messages_after_two_different_badges_start_in_one_column() -> None:
    console, buffer = plain()
    console.status(Status.OK, "first")
    console.status(Status.PARTIAL, "second")
    starts = {line.index(line.split("] ")[-1]) for line in buffer.getvalue().splitlines()}
    assert len(starts) == 1


def test_the_sign_and_the_word_survive_with_colour_off() -> None:
    """The case that matters most: a log file, a CI transcript, a pipe."""
    console, buffer = plain()
    console.status(Status.FAILED, "something broke")
    line = buffer.getvalue()
    assert "\033[" not in line
    assert "FAIL" in line and "x " in line


def test_the_colour_is_added_when_there_is_one_to_add() -> None:
    console, buffer = coloured()
    console.status(Status.OK, "fine")
    assert PALETTES["basic"]["green"] in buffer.getvalue()


def test_an_unknown_state_still_prints_rather_than_vanishing() -> None:
    """A status this table has never heard of is a bug somewhere, and hiding
    the line is the one response that makes the bug harder to find."""
    console, _ = plain()
    assert "SURPRISE" in console.badge("surprise")


def test_a_style_is_not_emitted_twice_when_a_caller_adds_its_own() -> None:
    console, _ = coloured()
    once = console.style("x", "bold")
    assert console.style("x", "bold", "bold") == once


# ----------------------------------------------------------------------------
# headings, and work in flight
# ----------------------------------------------------------------------------


def test_a_heading_is_bold_blue_and_blue_is_never_a_verdict() -> None:
    console, buffer = coloured()
    console.header("Phase 1")
    rendered = buffer.getvalue()
    assert PALETTES["basic"]["blue"] in rendered and PALETTES["basic"]["bold"] in rendered
    # Blue would be ambiguous if any outcome claimed it.
    assert "blue" not in {s for mark in BADGES.values() for s in mark.styles}


def test_a_rule_with_a_title_is_a_heading_too() -> None:
    console, buffer = coloured()
    console.rule("summary")
    assert PALETTES["basic"]["blue"] in buffer.getvalue()


def test_progress_announces_work_before_the_terminal_goes_quiet() -> None:
    console, buffer = plain()
    console.progress("gcloud", "installing Google Cloud CLI")
    line = buffer.getvalue()
    assert "RUNNING" in line
    assert "installing Google Cloud CLI" in line
    assert BADGES[RUNNING].styles == ("cyan",)


def test_progress_can_be_turned_off_without_touching_the_verdicts() -> None:
    console, buffer = plain(show_progress=False)
    console.progress("gcloud", "installing")
    console.status(Status.OK, "gcloud")
    assert "RUNNING" not in buffer.getvalue()
    assert "OK" in buffer.getvalue()


def test_a_progress_line_reaches_the_transcript_too(transcript) -> None:
    """A record of a run that took four minutes is more use with the four
    minutes marked in it than without."""
    console = Console(stream=io.StringIO(), color=False, transcript=transcript)
    console.progress("phase 4", "probing https://example.test")
    transcript.stream.flush()
    assert "RUNNING" in transcript.path.read_text()


# ----------------------------------------------------------------------------
# a line too long for the box it is in
# ----------------------------------------------------------------------------


def test_a_path_too_long_for_the_box_is_wrapped_and_not_cut_short() -> None:
    """Phase 4 prints paths and install commands the operator has to type. One
    silently missing its last characters is worse than one that wraps: the
    reader cannot tell it happened."""
    path = "/home/operator/.local/share/initialize-claude-ai/bridges/mcp_bridge_example_service.py"
    console, buffer = plain()
    console.box([path])
    rendered = buffer.getvalue()
    assert path not in rendered, "too long to be on one line -- this is the wrapping case"
    assert "".join(line.strip("|+- ") for line in rendered.splitlines()) == path


def test_words_stay_whole_while_they_fit() -> None:
    """Breaking every line at the width would make prose unreadable, so the
    hard break is only for the token that has no other way to fit."""
    console, buffer = plain()
    console.box(["the quick brown fox " * 8])
    assert "quick-" not in buffer.getvalue()
    assert all(chunk in buffer.getvalue() for chunk in ("the quick brown", "fox"))


# ----------------------------------------------------------------------------
# the log, wearing the same badges
# ----------------------------------------------------------------------------


@pytest.fixture
def logstream():
    """A logger on a buffer, torn down so the next test starts clean."""
    buffer = io.StringIO()
    yield buffer
    logger = logging.getLogger("initialize_claude_ai")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def test_a_warning_from_the_logger_looks_like_a_warning_from_the_report(logstream) -> None:
    style, _ = plain()
    logger = setup_logging(level="debug", stream=logstream, style=style)
    logger.warning("memory usage approaching limit")
    assert style.badge("warning") in logstream.getvalue()


@pytest.mark.parametrize(
    ("level", "word"),
    [("debug", "DEBUG"), ("info", "INFO"), ("warning", "WARN"), ("error", "ERROR")],
)
def test_each_level_gets_its_own_word(logstream, level: str, word: str) -> None:
    style, _ = plain()
    logger = setup_logging(level="debug", stream=logstream, style=style)
    getattr(logger, level)("a message")
    assert word in logstream.getvalue()
    assert "a message" in logstream.getvalue()


def test_a_level_nobody_registered_still_reaches_the_screen() -> None:
    """Swallowing a record because its level is unfamiliar is the worst
    available trade: the unfamiliar ones are the interesting ones."""
    style, _ = plain()
    record = logging.LogRecord("x", 25, __file__, 1, "an odd one", None, None)
    record.levelname = "NOTICE"
    rendered = BadgeFormatter(style).format(record)
    assert "NOTICE" in rendered and "an odd one" in rendered


def test_the_log_file_keeps_the_plain_format(transcript, logstream) -> None:
    """Colour and signs are for a terminal. A record that has to be grepped in
    six months should start with a timestamp and a level, nothing else."""
    style, _ = plain()
    logger = setup_logging(level="debug", transcript=transcript, stream=logstream, style=style)
    logger.error("connection timeout on port 5432")
    transcript.stream.flush()
    body = transcript.path.read_text()
    assert "ERROR" in body and "connection timeout on port 5432" in body
    assert "x [" not in body, "the sign belongs on the terminal, not in the file"


def test_without_a_style_the_log_falls_back_to_plain_words(logstream) -> None:
    logger = setup_logging(level="debug", stream=logstream)
    logger.warning("plain please")
    assert "WARNING plain please" in logstream.getvalue()
    assert "[" not in logstream.getvalue()


# ----------------------------------------------------------------------------
# the published scheme, under the names people know it by
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("alias", sorted(PALETTE_ALIASES))
def test_the_classic_scheme_answers_to_the_names_it_is_published_under(alias: str) -> None:
    """Somebody who read the eight-colour scheme somewhere else and asked for it
    by that name should get it, not a "no such palette" and a silent default."""
    console = Console(stream=io.StringIO(), color=True, palette=alias)
    assert console.codes is PALETTES[PALETTE_ALIASES[alias]]


def test_the_aliases_are_the_plain_ansi_codes_and_nothing_else() -> None:
    """The point of asking for 'classic' is to get \\033[32m rather than a
    256-colour approximation of it."""
    console = Console(stream=io.StringIO(), color=True, palette="classic")
    assert console.codes["green"] == "\033[32m"
    assert console.codes["red"] == "\033[31m"
    assert console.codes["yellow"] == "\033[33m"
    assert console.codes["cyan"] == "\033[36m"
    assert console.codes["blue"] == "\033[34m"
    assert console.codes["dim"] == "\033[2m"
    assert console.codes["reset"] == "\033[0m"


def test_every_alias_points_at_a_palette_that_exists() -> None:
    for alias, target in PALETTE_ALIASES.items():
        assert target in PALETTES, f"{alias} points at {target!r}"
        assert alias not in PALETTES, f"{alias} would shadow a real palette"


def test_the_sample_rows_name_only_badges_that_exist() -> None:
    for row in BADGE_ROWS:
        for role in row:
            assert role in BADGES


def test_a_summary_row_is_signed_the_same_as_the_badge_that_made_it() -> None:
    """A phase that came out PARTIAL is a triangle on the step and a triangle on
    the summary row; reading one table should teach you the other."""
    for status in (Status.OK, Status.CHANGED, Status.SKIPPED, Status.PARTIAL, Status.FAILED):
        assert glyph_for(status) == BADGES[status].glyph


def test_an_unknown_state_still_gets_a_sign() -> None:
    assert glyph_for("invented") in GLYPH_SETS["unicode"]
