"""The opening banner and the closing summary every phase prints.

The rule these tests defend is narrow and important: a failure has to be
*named*, with its reason, in the block an operator reads at the end. Counting
it ("1 failed") is not enough -- on a seventeen-component phase 1 run, a count
sends someone scrolling back through the log to find out which one it was.

The same goes for the parts that were skipped, and for the closing assertion:
the report has to say, in words, whether it is safe to carry on -- that is the
decision the operator is actually standing there trying to make.

They also pin the portability properties of the frame: it never runs wider than
it was told to, it degrades to ASCII on a stream that cannot encode a
box-drawing character (the state of any host still in the C locale), and it only
reaches for 256-colour tones on a terminal that can render them.
"""

from __future__ import annotations

import io
import re

import pytest

from initialize_claude_ai.core.logging_setup import (
    DEFAULT_PALETTE,
    GLYPH_SETS,
    LIGHT_PALETTE,
    PALETTES,
    Console,
    background_is_light,
)
from initialize_claude_ai.core.report import (
    SAFETY_LABEL,
    PhaseResult,
    RunReport,
    Safety,
    Status,
    format_duration,
    roll_up,
    verdict_of,
)
from initialize_claude_ai.phases import all_phases
from initialize_claude_ai.phases.base import Phase


class DemoPhase(Phase):
    """A phase with one of everything, so the summary has something to say."""

    slug = "demo"
    number = 99
    title = "Demo"
    description = "A phase that exists only to be summarised."
    enable_key = ""

    def execute(self, result: PhaseResult) -> None:
        result.add("installed thing", Status.CHANGED, "put it there")
        result.add("checked thing", Status.OK, "already fine")
        result.add("absent thing", Status.SKIPPED, "not applicable here")
        result.add(
            "broken thing",
            Status.FAILED,
            "the remote refused the handshake",
            remediation=["Try it again with --force-auth."],
        )


@pytest.fixture
def loud(ctx):
    """The shared ctx, but with a console that actually writes, into a buffer."""
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    return ctx


def run_demo(ctx) -> tuple[PhaseResult, str]:
    phase = DemoPhase(ctx)
    result = phase.run()
    return result, ctx.console._out.getvalue()


# -- what the summary has to contain ----------------------------------------


def test_the_failed_step_is_named_not_just_counted(loud) -> None:
    ctx = loud
    _result, output = run_demo(ctx)
    summary = output.split("Phase 99 summary")[-1]
    assert "broken thing" in summary
    assert "the remote refused the handshake" in summary


def test_the_summary_names_what_passed_and_what_was_skipped(loud) -> None:
    ctx = loud
    _result, output = run_demo(ctx)
    summary = output.split("Phase 99 summary")[-1]
    for name in ("installed thing", "checked thing", "absent thing"):
        assert name in summary


def test_the_phase_states_its_own_overall_result_in_words(loud) -> None:
    """'PARTIAL' is this program's vocabulary; 'PARTIALLY PASSED' is everyone's."""
    ctx = loud
    result, output = run_demo(ctx)
    # Two did work, one was skipped, one failed -- so the phase partially passed.
    assert result.status == Status.PARTIAL
    assert "PHASE 99 RESULT: PARTIALLY PASSED" in output
    assert "2 of 4 step(s) passed" in output
    assert "1 skipped" in output and "1 failed" in output


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.OK, "PASSED"),
        (Status.CHANGED, "PASSED WITH CHANGES"),
        (Status.SKIPPED, "SKIPPED"),
        (Status.PARTIAL, "PARTIALLY PASSED"),
        (Status.FAILED, "FAILED"),
    ],
)
def test_every_status_has_a_verdict_anyone_can_read(status: str, expected: str) -> None:
    assert verdict_of(status) == expected


def test_the_verdict_travels_in_the_json_report_too(loud) -> None:
    """The word in the box and the word in the report are the same word."""
    ctx = loud
    result, output = run_demo(ctx)
    assert result.to_dict()["verdict"] == "PARTIALLY PASSED"
    assert result.to_dict()["verdict"] in output


def test_remediation_is_repeated_where_it_can_be_acted_on(loud) -> None:
    ctx = loud
    _result, output = run_demo(ctx)
    assert "Next steps:" in output.split("Phase 99 summary")[-1]
    assert "Try it again with --force-auth." in output


def test_the_banner_says_which_phase_and_what_it_is_for(loud) -> None:
    ctx = loud
    _result, output = run_demo(ctx)
    banner = output.split("Phase 99 summary")[0]
    assert f"PHASE 99 OF {len(all_phases())}" in banner
    assert "DEMO" in banner
    assert DemoPhase.description in banner.replace("\n", " ").replace("|", "")


def test_a_disabled_phase_still_gets_a_banner_and_a_verdict(loud, monkeypatch) -> None:
    """The silent skip is the one that wastes an afternoon."""
    ctx = loud
    monkeypatch.setattr(DemoPhase, "is_enabled", lambda self: (False, "switched off for the test"))
    result, output = run_demo(ctx)
    assert result.status == Status.SKIPPED
    assert "PHASE 99" in output
    assert "switched off for the test" in output
    assert "PHASE 99 RESULT: SKIPPED" in output
    # A scoreboard of three zeroes would say nothing; the reason says everything.
    assert "0 of 0 step(s) passed" not in output


# -- why the non-passing parts did not pass ----------------------------------


def test_every_non_passing_step_gets_a_reason_not_just_a_name(loud) -> None:
    """A count says something is wrong; only the reason says whether it matters."""
    ctx = loud
    _result, output = run_demo(ctx)
    why = output.split("Why these did not pass:")[-1]
    # The failure, and the skip -- 'skipped' covers both "nothing to do" and
    # "could not be attempted", and the operator cannot tell which without this.
    assert "the remote refused the handshake" in why
    assert "not applicable here" in why


def test_the_reason_block_is_absent_when_everything_passed(ctx) -> None:
    class CleanPhase(DemoPhase):
        def execute(self, result: PhaseResult) -> None:
            result.add("fine", Status.OK, "nothing to say")

    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii")
    CleanPhase(ctx).run()
    assert "Why these did not pass" not in buffer.getvalue()


def test_a_long_tail_of_reasons_is_elided_rather_than_burying_the_verdict(ctx) -> None:
    class NoisyPhase(DemoPhase):
        def execute(self, result: PhaseResult) -> None:
            for index in range(30):
                result.add(f"thing {index}", Status.SKIPPED, "not applicable")

    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii")
    ctx.settings.set("console.max_named_steps", 5)
    NoisyPhase(ctx).run()
    output = buffer.getvalue()
    assert "and 25 more" in output
    assert "PHASE 99 RESULT" in output


# -- is it safe to continue --------------------------------------------------


def test_the_summary_asserts_whether_it_is_safe_to_go_on(loud) -> None:
    """The question the report exists to answer, answered in words."""
    ctx = loud
    result, output = run_demo(ctx)
    assert result.safety == Safety.CAUTION
    assert SAFETY_LABEL[Safety.CAUTION] in output
    # And with a reason attached, not just a label.
    assert result.safety_reason.strip()
    assert result.safety_reason.split(".")[0] in output.replace("\n", " ").replace("|", "")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.OK, Safety.SAFE),
        (Status.CHANGED, Safety.SAFE),
        (Status.SKIPPED, Safety.CAUTION),
        (Status.PARTIAL, Safety.CAUTION),
        (Status.FAILED, Safety.BLOCKED),
    ],
)
def test_the_default_assertion_follows_the_status(ctx, status: str, expected: str) -> None:
    phase = DemoPhase(ctx)
    result = PhaseResult(phase="demo", status=status)
    assert phase.safety(result)[0] == expected


def test_the_assertion_travels_in_the_json_report_too(loud) -> None:
    ctx = loud
    result, _output = run_demo(ctx)
    payload = result.to_dict()
    assert payload["safety"] == Safety.CAUTION
    assert payload["safety_label"] == SAFETY_LABEL[Safety.CAUTION]
    assert payload["safety_reason"]


def test_a_dry_run_says_so_in_the_assertion(ctx, monkeypatch) -> None:
    """'settings.json was written' must not be asserted about an untouched host."""
    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii")
    monkeypatch.setattr(type(ctx), "dry_run", True, raising=False)
    result = DemoPhase(ctx).run()
    assert "nothing changed" in result.safety_reason


def test_the_run_is_only_as_safe_as_its_least_safe_phase() -> None:
    report = RunReport()
    report.add(PhaseResult(phase="a", safety=Safety.SAFE))
    report.add(PhaseResult(phase="b", safety=Safety.CAUTION))
    assert report.safety == Safety.CAUTION
    report.add(PhaseResult(phase="c", safety=Safety.BLOCKED))
    assert report.safety == Safety.BLOCKED
    assert report.to_dict()["safety_label"] == SAFETY_LABEL[Safety.BLOCKED]


# -- how it is drawn ---------------------------------------------------------


@pytest.mark.parametrize("width", [40, 64, 80, 120])
def test_no_line_is_wider_than_the_configured_width(ctx, width: int) -> None:
    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii", width=width)
    _result, output = run_demo(ctx)
    assert output.strip(), "the phase printed nothing at all"
    assert max(len(line) for line in output.splitlines()) <= width


def test_the_frame_falls_back_to_ascii_when_the_stream_cannot_encode_it() -> None:
    """A C-locale host gets a plain frame rather than a UnicodeEncodeError."""
    ascii_only = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    assert Console(stream=ascii_only).glyphs is GLYPH_SETS["ascii"]

    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert Console(stream=utf8).glyphs is GLYPH_SETS["unicode"]


def test_a_256_colour_terminal_gets_the_256_colour_palette(monkeypatch) -> None:
    """Full-saturation ANSI primaries are the glare; the hue still carries meaning."""
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert Console(stream=io.StringIO()).codes is PALETTES[DEFAULT_PALETTE]

    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm")
    assert Console(stream=io.StringIO()).codes is PALETTES[DEFAULT_PALETTE]


# ---------------------------------------------------------------------------
# How loud and how colourful, in numbers rather than in adjectives
# ---------------------------------------------------------------------------
#
# Two separate measurements, because they are two separate complaints.
#
# "Too bright" and "too dark" are about *contrast*: the WCAG ratio against the
# background the palette was drawn for. "Drab", "washed out", "not eye-catching"
# are about *saturation*: how far a colour is from the grey of the same
# brightness. A palette can be perfectly readable and still look like a
# black-and-white photograph, which is exactly the failure these tests exist to
# catch -- nudging one green toward comfort, then another, until nothing has any
# colour left in it.
#
# So both are pinned here, and the tension between them is pinned too: the most
# saturated colours in the cube sit at its corners, and the corners are not
# equally bright, so asking for chroma necessarily widens the brightness spread.
# The bands below are where that argument was settled, not a rounding of it.

CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
HUES = ("red", "green", "yellow", "blue", "magenta", "cyan")


def rgb_of(code: str) -> tuple[int, int, int]:
    """The sRGB value behind a ``\\033[38;5;Nm`` escape."""
    index = int(re.search(r"38;5;(\d+)m", code).group(1))
    if index >= 232:  # the 24-step grey ramp
        value = 8 + (index - 232) * 10
        return value, value, value
    index -= 16
    return (
        CUBE_LEVELS[index // 36],
        CUBE_LEVELS[(index % 36) // 6],
        CUBE_LEVELS[index % 6],
    )


def contrast(code: str, background: tuple[int, int, int]) -> float:
    """WCAG 2.1 contrast ratio, the number both "glaring" and "washed out" are."""

    def luminance(rgb: tuple[int, int, int]) -> float:
        channels = []
        for raw in rgb:
            c = raw / 255
            channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = luminance(rgb_of(code)), luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def saturation(code: str) -> float:
    """How colourful, on 0 (grey) to 1 (a corner of the cube).

    HSV saturation rather than anything cleverer, because the question being
    asked is the crude one: is there any colour in this at all.
    """
    red, green, blue = rgb_of(code)
    brightest = max(red, green, blue)
    return 0.0 if brightest == 0 else (brightest - min(red, green, blue)) / brightest


@pytest.mark.parametrize("background", [(0, 0, 0), (30, 30, 30), (40, 44, 52)])
def test_the_default_palette_is_readable_without_glaring(background) -> None:
    """4.5:1 is the line at which text counts as readable. The ceiling is the
    neon corner of the cube -- #00ff00 is 15:1 on black -- which is what people
    turn colour off to get away from."""
    codes = PALETTES[DEFAULT_PALETTE]
    for hue in HUES:
        ratio = contrast(codes[hue], background)
        assert 4.5 <= ratio <= 12.0, f"{hue} is {ratio:.1f}:1 on {background}"


def test_the_default_palette_is_actually_colourful() -> None:
    """The point of the default. Every hue has to carry real chroma, and the set
    as a whole has to average high, so no single "let us soften that one" can
    drain the palette a colour at a time."""
    codes = PALETTES[DEFAULT_PALETTE]
    chroma = {hue: saturation(codes[hue]) for hue in HUES}
    for hue, value in chroma.items():
        assert value >= 0.6, f"{hue} is only {value:.2f} saturated"
    assert sum(chroma.values()) / len(chroma) >= 0.75
    # ...and at least half of them at the full chroma the cube can give.
    assert sum(1 for value in chroma.values() if value == 1.0) >= 3


def test_the_calm_palette_trades_that_chroma_for_an_even_weight() -> None:
    """``contrast`` exists for the other preference, and has to keep earning it:
    a tight brightness band, which is what stops the eye being pulled to
    whichever line happens to be green rather than to the failure."""
    codes = PALETTES["contrast"]
    ratios = [contrast(codes[hue], (0, 0, 0)) for hue in HUES]
    assert max(ratios) / min(ratios) <= 1.5
    chroma = [saturation(codes[hue]) for hue in HUES]
    assert sum(chroma) / len(chroma) < sum(saturation(PALETTES[DEFAULT_PALETTE][hue]) for hue in HUES) / len(HUES)


def test_even_the_colourful_default_keeps_its_brightness_spread_in_hand() -> None:
    """Chroma costs some evenness -- a full-chroma emerald is brighter than any
    red that is still red -- but only some. Past about 2x the brightest hue
    starts reading as the important one whatever it says."""
    codes = PALETTES[DEFAULT_PALETTE]
    ratios = [contrast(codes[hue], (0, 0, 0)) for hue in HUES]
    assert max(ratios) / min(ratios) <= 1.8


def test_grey_is_quieter_than_every_colour_it_sits_beside() -> None:
    """Skipped is not a warning. It has to recede in both palettes, or the
    reader learns to ignore the colour that does mean something."""
    for name in (DEFAULT_PALETTE, LIGHT_PALETTE):
        codes = PALETTES[name]
        background = (0, 0, 0) if name == DEFAULT_PALETTE else (255, 255, 255)
        quietest = min(contrast(codes[hue], background) for hue in HUES)
        assert contrast(codes["dim"], background) < quietest, name


@pytest.mark.parametrize("background", [(255, 255, 255), (253, 246, 227)])
def test_the_light_palette_is_dark_enough_to_read_on_paper(background) -> None:
    """The same design upside down: on white it is being too *pale* that makes a
    colour unreadable, and going all the way to near-black makes it pointless.
    Every hue here is at full chroma, which a light background allows and a dark
    one does not."""
    codes = PALETTES[LIGHT_PALETTE]
    for hue in HUES:
        ratio = contrast(codes[hue], background)
        assert 3.9 <= ratio <= 7.0, f"{hue} is {ratio:.1f}:1 on {background}"
        assert saturation(codes[hue]) == 1.0, hue


def test_the_summary_frame_is_coloured_by_the_verdict_it_frames(ctx) -> None:
    """The box is the biggest thing on the screen, so it is the thing worth
    colouring: on a four-phase run the phase that went wrong is a differently
    coloured frame, findable by scrolling instead of by reading every verdict."""
    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=True, glyph_set="ascii")
    result = DemoPhase(ctx).run()
    codes = PALETTES[DEFAULT_PALETTE]
    output = buffer.getvalue()
    assert result.status == Status.PARTIAL
    assert f"{codes['yellow']}+-" in output  # the summary, drawn as PARTIAL
    assert f"{codes['blue']}+-" in output  # the banner, which has no verdict yet
    assert f"{codes['green']}+-" not in output


def test_the_frame_colour_is_dropped_with_every_other_colour(ctx) -> None:
    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii")
    DemoPhase(ctx).run()
    assert "\033[" not in buffer.getvalue()


def test_every_palette_keeps_the_same_meaning_for_every_hue() -> None:
    """Switching palettes changes how loud the report is, never what it says."""
    for name, codes in PALETTES.items():
        assert set(codes) == set(PALETTES[DEFAULT_PALETTE]), name


# ---------------------------------------------------------------------------
# ...and which one is picked for you
# ---------------------------------------------------------------------------


def test_a_light_terminal_is_given_the_light_palette(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Console(stream=io.StringIO()).codes is PALETTES[LIGHT_PALETTE]


@pytest.mark.parametrize("value", ["15;0", "15;default", "", "nonsense", "7"])
def test_anything_that_is_not_plainly_a_light_background_is_treated_as_dark(monkeypatch, value: str) -> None:
    """Guessing light on a dark terminal is the worse mistake -- dark text on a
    dark screen is unreadable, where the reverse is merely a bit bright. Note
    that a bare "7" is a foreground, not a background: half an answer is no
    answer."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORFGBG", value)
    assert Console(stream=io.StringIO()).codes is PALETTES[DEFAULT_PALETTE]


def test_the_background_is_read_from_the_variable_the_terminal_sets() -> None:
    assert background_is_light({"COLORFGBG": "0;15"}) is True
    assert background_is_light({"COLORFGBG": "0;7"}) is True
    assert background_is_light({"COLORFGBG": "15;0"}) is False
    assert background_is_light({}) is False


def test_a_named_palette_is_never_second_guessed(monkeypatch) -> None:
    """Somebody who has said which one they want has settled the question."""
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Console(stream=io.StringIO(), palette="soft").codes is PALETTES["soft"]


def test_an_eight_colour_terminal_keeps_the_basic_palette(monkeypatch) -> None:
    """A 256-colour escape on an 8-colour terminal prints as literal text."""
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert Console(stream=io.StringIO()).codes is PALETTES["basic"]


@pytest.mark.parametrize("name", sorted(PALETTES))
def test_every_palette_can_be_asked_for_by_name_and_defines_every_style(name: str) -> None:
    """Taste is not something this program gets to settle for the reader."""
    console = Console(stream=io.StringIO(), palette=name)
    assert console.codes is PALETTES[name]
    # A palette missing a key would silently drop that colour, not fail loudly.
    assert set(console.codes) == set(PALETTES[DEFAULT_PALETTE])


def test_colour_is_dropped_entirely_rather_than_softened(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console = Console(stream=io.StringIO())
    assert console.color is False
    assert console._style("x", "red") == "x"


def test_undecorated_output_keeps_the_content_and_drops_the_frame(ctx) -> None:
    buffer = io.StringIO()
    ctx.console = Console(stream=buffer, color=False, glyph_set="ascii", decorate=False)
    _result, output = run_demo(ctx)
    assert "broken thing" in output and "PHASE 99 RESULT: PARTIALLY PASSED" in output
    assert "+---" not in output


def test_quiet_and_json_modes_print_no_frame_at_all(ctx) -> None:
    """--json reserves stdout for the report; --quiet asked for silence."""
    for kwargs in ({"quiet": True}, {"json_mode": True}):
        buffer = io.StringIO()
        ctx.console = Console(stream=buffer, color=False, **kwargs)
        run_demo(ctx)
        assert buffer.getvalue() == ""


# -- a phase judged on its subjects, not on its steps ------------------------
#
# A phase that repeats itself over a list -- one server per --mcp-server -- is
# answering a question per subject, not per step. Eight probes against a host
# that ends up with a working server is one success, and reporting it as four
# failures and four skips buries the answer under the method.


class GroupedDemoPhase(DemoPhase):
    """Two servers: one found on the second stage, one that yielded nothing."""

    slug = "grouped"
    number = 98
    title = "Grouped demo"
    group_noun = "server"

    ALPHA = "https://alpha.test"
    BETA = "https://beta.test"

    def execute(self, result: PhaseResult) -> None:
        # A step about the run as a whole: it belongs to no server.
        result.add("discovery mode", Status.OK, "deterministic probing")

        result.add(
            f"{self.ALPHA} :: endpoint",
            Status.FAILED,
            "nothing answered on 166 probed path(s)",
            group=self.ALPHA,
        )
        result.add(f"{self.ALPHA} :: package", Status.OK, "alpha-mcp 1.2.0 over stdio", group=self.ALPHA)
        result.add(
            f"{self.ALPHA} :: register alpha",
            Status.CHANGED,
            "written to ./.mcp.json",
            group=self.ALPHA,
        )
        result.declare_group(self.ALPHA, Status.OK, "working MCP server: alpha -- alpha-mcp 1.2.0")

        result.add(
            f"{self.BETA} :: endpoint",
            Status.FAILED,
            "6 endpoint(s) answered but require authentication",
            group=self.BETA,
        )
        result.add(
            f"{self.BETA} :: package",
            Status.SKIPPED,
            "no published server targets this product",
            group=self.BETA,
        )
        result.declare_group(self.BETA, Status.FAILED, "no working MCP server was found")


def run_grouped(ctx) -> tuple[PhaseResult, str]:
    result = GroupedDemoPhase(ctx).run()
    return result, ctx.console._out.getvalue()


def test_the_scoreboard_counts_servers_and_names_them(loud) -> None:
    """Six steps, two answers. The scoreboard reports the two."""
    result, output = run_grouped(loud)
    assert result.group_tally() == {"failed": 1, "skipped": 0, "passed": 1}
    summary = output.split("Phase 98 summary")[-1]
    assert re.search(r"passed\s+1\s+https://alpha\.test", summary)
    assert re.search(r"failed\s+1\s+https://beta\.test", summary)


def test_the_phase_verdict_is_the_count_of_servers_not_of_steps(loud) -> None:
    result, output = run_grouped(loud)
    assert result.status == Status.PARTIAL
    assert "PHASE 98 RESULT: PARTIALLY PASSED" in output
    assert "1 of 2 server(s) passed" in output
    # The steps are still counted, but as the working, not as the result.
    assert "6 step(s) in total" in output
    assert "of 6 step(s) passed" not in output


def test_a_server_that_worked_says_so_however_many_probes_it_took(loud) -> None:
    """The failed first stage is listed, and the server still reads as PASSED."""
    _result, output = run_grouped(loud)
    alpha = output.split("https://alpha.test")[-1].split("https://beta.test")[0]
    assert "PASSED" in alpha
    assert "working MCP server: alpha -- alpha-mcp 1.2.0" in alpha
    assert "nothing answered on 166 probed path(s)" in alpha


def test_each_server_carries_its_own_arithmetic(loud) -> None:
    """Per-subject counts, so 'which of my servers is short?' is answerable."""
    _result, output = run_grouped(loud)
    assert "2 of 3 step(s) passed" in output, "alpha: a package and a registration"
    assert "0 of 2 step(s) passed" in output, "beta: a failure and a skip"


def test_every_step_is_listed_under_its_own_server_with_its_reason(loud) -> None:
    _result, output = run_grouped(loud)
    assert f"Every {GroupedDemoPhase.group_noun}, and what happened to it:" in output
    beta = output.split("https://beta.test")[-1]
    assert "6 endpoint(s) answered but require authentication" in beta
    assert "no published server targets this product" in beta


def test_a_step_reads_short_inside_the_section_it_is_already_filed_under(loud) -> None:
    """Repeating the address on every row pushes the part that differs off-box."""
    _result, output = run_grouped(loud)
    assert output.count("https://alpha.test :: endpoint") == 0
    assert "register alpha" in output


def test_a_phase_with_no_subjects_is_summarised_exactly_as_before(loud) -> None:
    """Grouping is opt-in: a phase that declares none renders the old way."""
    _result, output = run_demo(loud)
    assert "2 of 4 step(s) passed" in output
    assert "and what happened to it:" not in output


# -- the data behind it ------------------------------------------------------


def test_outcomes_bucket_every_status_and_report_the_empty_ones() -> None:
    result = PhaseResult(phase="x")
    for status in (Status.OK, Status.CHANGED, Status.SKIPPED, Status.PARTIAL, Status.FAILED):
        result.add(status, status)
    assert result.tally() == {"failed": 2, "skipped": 1, "passed": 2}
    # Every bucket is present even when empty, so a report can state "0 failed".
    assert set(PhaseResult(phase="x").outcomes()) == {"failed", "skipped", "passed"}


def test_the_tally_travels_in_the_json_report_too() -> None:
    """Human and machine reports are derived from one object, and stay in step."""
    result = PhaseResult(phase="x")
    result.add("a", Status.FAILED)
    assert result.to_dict()["tally"]["failed"] == 1


def grouped_result(noun: str = "server") -> PhaseResult:
    """One subject that worked, declared by the phase rather than rolled up."""
    result = PhaseResult(phase="x", group_noun=noun)
    result.add("a :: endpoint", Status.FAILED, "nothing answered", group="a")
    result.add("a :: package", Status.OK, "found a package", group="a")
    result.declare_group("a", Status.OK, "working server: a-mcp")
    return result


def test_a_subject_is_judged_by_the_phase_not_by_its_steps() -> None:
    """Only the phase knows a server found on the second stage passed."""
    result = grouped_result()
    assert roll_up(s.status for s in result.steps) == Status.PARTIAL
    group = result.groups()[0]
    assert group.status == Status.OK
    assert group.outcome == "passed"
    assert group.tally() == {"failed": 1, "skipped": 0, "passed": 1}
    assert result.finalize().status == Status.OK


def test_an_undeclared_subject_still_counts_and_still_says_why() -> None:
    """An address that never parsed never reached a verdict; it is still a
    server that produced nothing, and leaving it out would quietly shrink the
    denominator the operator typed."""
    result = grouped_result()
    result.add("address http://", Status.FAILED, "no host in address", group="http://")
    unparsed = result.groups()[-1]
    assert unparsed.key == "http://"
    assert unparsed.status == Status.FAILED
    assert unparsed.detail == "no host in address", "the worst step speaks for it"
    assert result.group_tally() == {"failed": 1, "skipped": 0, "passed": 1}


def test_declaring_the_same_subject_twice_replaces_rather_than_repeats() -> None:
    result = grouped_result()
    result.declare_group("a", Status.FAILED, "thought better of it")
    assert [g.status for g in result.groups()] == [Status.FAILED]


def test_the_subjects_decide_the_phase_not_the_bookkeeping_around_them() -> None:
    """Every server came back empty, so the phase failed -- however well the
    steps that organised the search went."""
    result = PhaseResult(phase="x", group_noun="server")
    for name in ("servers", "discovery mode", "selection"):
        result.add(name, Status.OK, "fine")
    result.add("a :: endpoint", Status.FAILED, "nothing answered", group="a")
    result.declare_group("a", Status.FAILED, "no working server was found")
    assert result.finalize().status == Status.FAILED


def test_a_run_level_failure_is_never_hidden_by_passing_subjects() -> None:
    result = grouped_result()
    result.add("mcp configuration", Status.FAILED, "the file could not be written")
    assert result.finalize().status == Status.PARTIAL


def test_a_run_level_change_promotes_an_otherwise_clean_pass() -> None:
    """The one thing the subjects cannot say for themselves: something changed."""
    result = grouped_result()
    result.add("mcp configuration", Status.CHANGED, "written to ./.mcp.json")
    assert result.finalize().status == Status.CHANGED


def test_ungrouped_steps_are_the_ones_about_the_run_itself() -> None:
    result = grouped_result()
    result.add("mcp configuration", Status.OK, "written")
    assert [s.name for s in result.ungrouped()] == ["mcp configuration"]


def test_the_subjects_travel_in_the_json_report_too() -> None:
    """--json is the other way this run gets read, and it reads the same."""
    payload = grouped_result().finalize().to_dict()
    assert payload["group_noun"] == "server"
    assert payload["group_tally"] == {"failed": 0, "skipped": 0, "passed": 1}
    assert [g["group"] for g in payload["groups"]] == ["a"]
    assert payload["groups"][0]["verdict"] == "PASSED"
    assert payload["groups"][0]["tally"]["failed"] == 1
    assert {s["group"] for s in payload["steps"]} == {"a"}


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0004, "<1ms"), (0.042, "42ms"), (1.24, "1.2s"), (38.68, "38.7s"), (125.0, "2m05.0s")],
)
def test_durations_read_as_durations(seconds: float, expected: str) -> None:
    """'0.0s' for a 40 ms phase reads as a bug in the timer."""
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([Status.OK, Status.SKIPPED], Status.OK),
        ([Status.CHANGED, Status.SKIPPED], Status.CHANGED),
        ([Status.SKIPPED, Status.SKIPPED], Status.SKIPPED),
        ([Status.OK, Status.FAILED], Status.PARTIAL),
        ([Status.FAILED, Status.FAILED], Status.FAILED),
        ([], Status.SKIPPED),
    ],
)
def test_one_idle_phase_does_not_make_the_whole_run_skipped(statuses: list[str], expected: str) -> None:
    """Three phases of work plus one with nothing to do is not a skipped run."""
    assert roll_up(statuses) == expected


def test_every_registered_phase_asserts_safety_for_itself() -> None:
    """The base class's generic wording is a floor, not the intended answer."""
    generic = Phase.safety
    lazy = [cls.slug for cls in all_phases() if cls.safety is generic]
    assert lazy == [], "these phases should say what depends on them"


def test_no_phase_calls_a_fatal_result_safe_to_continue_from(ctx) -> None:
    """``require`` is on the base class, so every phase has to render its result.

    Each override answers a question of its own -- phase 1 about components,
    phase 4 about how much was discovered -- and none of those questions is the
    one a missing setting asks. Phase 4's was the dangerous one: additive work
    that "never blocks" would have called a phase SAFE that never got to start.
    """
    for cls in all_phases():
        result = PhaseResult(phase=cls.slug, title=cls.title)
        result.add("some-required-value", Status.FAILED, error="missing_setting")
        result.fatal = True
        result.finalize()
        level, reason = cls(ctx).safety(result)
        assert level == Safety.BLOCKED, f"{cls.slug} does not block on a fatal result"
        assert reason.strip()


def test_every_registered_phase_describes_itself() -> None:
    """The banner is only worth drawing if it says something."""
    missing = [cls.slug for cls in all_phases() if not cls.description.strip()]
    assert missing == [], "add a `description` to these phase classes"


def test_the_demo_phase_is_not_registered() -> None:
    """Guard on this module's own fixture: it must never reach --phases."""
    assert DemoPhase not in all_phases()
