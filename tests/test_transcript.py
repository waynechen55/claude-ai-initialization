"""The run's log file.

What is being defended here is a single claim: the file is a record of the
*run*, not of the terminal. Everything that follows -- that ``--quiet`` does not
empty it, that a summary box reaches it even when nothing is on screen, that log
records and console lines land in one file in one order -- is that claim tested
from a different side.

The other half is that a record is worthless if it cannot be shown to anyone, so
colour is stripped and credentials are redacted on the way in.
"""

from __future__ import annotations

import io
import logging
import re

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.config import load_settings
from initialize_claude_ai.core.logging_setup import (
    LOGGER_NAME,
    BadgeFormatter,
    Console,
    Transcript,
    active_transcript,
    close_transcript,
    rotate,
    setup_logging,
    strip_ansi,
)


@pytest.fixture
def transcript(tmp_path) -> Transcript:
    opened = Transcript.open(tmp_path / "claude_ai_initialization.log")
    assert opened is not None
    return opened


def read(transcript: Transcript) -> str:
    if not transcript.stream.closed:
        transcript.stream.flush()
    return transcript.path.read_text(encoding="utf-8")


# -- what reaches the file ----------------------------------------------------


def test_console_output_and_log_records_share_one_file_in_one_order(transcript) -> None:
    """Two writers, one handle: the order on disk is the order things happened.

    Written to two files instead, the same run would have to be reassembled by
    timestamp afterwards -- and two lines in the same second would be a guess.
    """
    console = Console(stream=io.StringIO(), color=False, transcript=transcript)
    setup_logging(level="error", transcript=transcript, stream=io.StringIO())
    log = logging.getLogger(f"{LOGGER_NAME}.test")

    console.write("first, on screen")
    log.debug("second, in the log")
    console.write("third, on screen")

    body = read(transcript)
    assert body.index("first") < body.index("second") < body.index("third")
    assert "DEBUG" in body


def test_a_quiet_run_still_records_everything_it_did(transcript) -> None:
    """--quiet is about the terminal. A silent run is the one whose record is
    most likely to be wanted afterwards."""
    screen = io.StringIO()
    console = Console(stream=screen, color=False, quiet=True, transcript=transcript)
    console.write("a line")
    console.box(["in a box"], title="a box")

    assert screen.getvalue() == ""
    body = read(transcript)
    assert "a line" in body
    assert "in a box" in body and "a box" in body


def test_json_mode_records_the_report_rather_than_nothing(transcript) -> None:
    """In --json mode the report is the only output there is."""
    console = Console(stream=io.StringIO(), color=False, json_mode=True, transcript=transcript)
    console.write("suppressed on stdout")
    console.json({"verdict": "PASSED"})

    body = read(transcript)
    assert "suppressed on stdout" in body
    assert '"verdict": "PASSED"' in body


def test_colour_is_stripped_so_the_file_stays_readable(transcript) -> None:
    console = Console(stream=io.StringIO(), color=True, palette="contrast", transcript=transcript)
    console.status("failed", "something broke")

    body = read(transcript)
    assert "\033[" not in body
    assert "[  FAIL  ] something broke" in body


def test_a_credential_cannot_reach_the_file(transcript) -> None:
    """The console redacts; so does the record, at the same sink."""
    from initialize_claude_ai.core import redact

    redact.register("sk-ant-000111222333444555")
    console = Console(stream=io.StringIO(), color=False, transcript=transcript)
    console.write("token is sk-ant-000111222333444555")

    assert "sk-ant-000111222333444555" not in read(transcript)
    assert redact.MASK in read(transcript)


def test_a_console_without_a_transcript_is_unaffected() -> None:
    """Recording is optional; nothing else changes when it is off."""
    screen = io.StringIO()
    Console(stream=screen, color=False).write("still printed")
    assert "still printed" in screen.getvalue()


def test_strip_ansi_leaves_the_words_alone() -> None:
    assert strip_ansi("\033[1m\033[38;5;203mred bold\033[0m") == "red bold"


# -- bracketing and rotation --------------------------------------------------


def test_each_run_is_bracketed_so_an_appended_file_stays_readable(transcript) -> None:
    transcript.start("initialize-claude-ai", "9.9.9", ["run", "--dry-run"])
    transcript.close(exit_code=3)

    body = read(transcript)
    assert "initialize-claude-ai 9.9.9" in body
    assert "# command: initialize-claude-ai run --dry-run" in body
    assert "exit 3" in body


def test_the_recorded_command_line_is_the_redacted_one(transcript) -> None:
    """A run that put a credential in argv must not be the reason it lands on disk."""
    transcript.start("initialize-claude-ai", "9.9.9", ["run", "--auth-anthropic-key", "s3cr3t-value"])
    assert "s3cr3t-value" not in read(transcript)


def test_writing_to_a_closed_transcript_is_not_an_error(transcript) -> None:
    """The last thing a crashing run should do is crash again while reporting it."""
    transcript.close()
    transcript.write("after the end")  # must not raise


def test_a_missing_directory_is_created_rather_than_refused(tmp_path) -> None:
    opened = Transcript.open(tmp_path / "not" / "yet" / "there" / "run.log")
    assert opened is not None and opened.path.exists()
    opened.close()


def test_an_unwritable_path_costs_the_record_not_the_run(tmp_path) -> None:
    """A read-only state directory is a reason to run without a log, not a
    reason to refuse to initialize the machine."""
    blocked = tmp_path / "file-in-the-way"
    blocked.write_text("not a directory", encoding="utf-8")
    assert Transcript.open(blocked / "nested.log") is None


def test_the_file_rotates_rather_than_growing_without_bound(tmp_path) -> None:
    path = tmp_path / "run.log"
    path.write_text("x" * 100, encoding="utf-8")
    rotate(path, max_bytes=50, backups=2)

    assert not path.exists()
    assert (tmp_path / "run.log.1").read_text(encoding="utf-8") == "x" * 100


def test_rotation_keeps_only_the_configured_number_of_copies(tmp_path) -> None:
    path = tmp_path / "run.log"
    for round_ in range(4):
        path.write_text(f"run {round_}" + "x" * 100, encoding="utf-8")
        rotate(path, max_bytes=50, backups=2)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.log.1", "run.log.2"]
    # .1 is the most recent of the two kept, not the oldest.
    assert (tmp_path / "run.log.1").read_text(encoding="utf-8").startswith("run 3")


def test_rotation_is_off_when_either_limit_is_zero(tmp_path) -> None:
    path = tmp_path / "run.log"
    path.write_text("x" * 100, encoding="utf-8")
    rotate(path, max_bytes=0, backups=3)
    rotate(path, max_bytes=50, backups=0)
    assert path.exists()


# -- through the CLI ----------------------------------------------------------


def test_a_command_records_itself_where_the_settings_say(sandbox) -> None:
    assert cli.main(["phases"]) == 0
    recorded = sandbox / "claude_ai_initialization.log"
    assert recorded.exists()
    body = recorded.read_text(encoding="utf-8")
    assert "install" in body and "mcp" in body  # the table that was printed
    assert "# command: initialize-claude-ai phases" in body
    assert "# finished" in body


def test_the_record_lands_in_the_directory_the_command_was_run_from(sandbox, tmp_path, monkeypatch) -> None:
    """The default is relative on purpose: the log belongs where the person
    running the install already is, not under a state directory they must look
    up first."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert cli.main(["phases"]) == 0
    assert (elsewhere / "claude_ai_initialization.log").exists()
    assert not (sandbox / "claude_ai_initialization.log").exists()


def test_the_relative_default_is_anchored_before_anything_can_chdir(sandbox) -> None:
    """Resolved once at startup, so a phase that changes directory cannot leave
    half the run in one file and half in another."""
    settings = load_settings(overrides=["paths.log_file=claude_ai_initialization.log"])
    opened = cli._open_transcript(settings, "%H:%M:%S", ["run"])
    assert opened is not None
    assert opened.path.is_absolute()
    assert opened.path == sandbox / "claude_ai_initialization.log"
    close_transcript()


def test_what_a_command_prints_to_stdout_is_recorded_too(sandbox, capsys) -> None:
    """``config`` answers on stdout rather than through the console, because a
    shell is reading it. That is a reason to bypass --quiet, not the record."""
    assert cli.main(["config", "meta.name"]) == 0
    printed = capsys.readouterr().out.strip()
    body = (sandbox / "claude_ai_initialization.log").read_text(encoding="utf-8")
    assert printed and printed in body


def test_the_log_file_flag_redirects_the_record(sandbox, tmp_path) -> None:
    target = tmp_path / "elsewhere.log"
    assert cli.main(["phases", "--log-file", str(target)]) == 0
    assert target.exists()
    assert not (sandbox / "claude_ai_initialization.log").exists()


def test_no_log_file_records_nothing_at_all(sandbox) -> None:
    assert cli.main(["phases", "--no-log-file"]) == 0
    assert not (sandbox / "claude_ai_initialization.log").exists()


def test_the_two_log_file_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["run", "--log-file", "x.log", "--no-log-file"])
    assert excinfo.value.code == 2


def test_runs_accumulate_rather_than_overwriting_each_other(sandbox) -> None:
    """The question a log answers is usually about the run *before* this one."""
    cli.main(["phases"])
    cli.main(["config", "meta.name"])
    body = (sandbox / "claude_ai_initialization.log").read_text(encoding="utf-8")
    assert body.count("# command:") == 2


def test_recording_the_console_can_be_turned_off_without_losing_the_log(sandbox) -> None:
    """logging.record_output=false leaves an ordinary log of records behind."""
    assert cli.main(["phases", "--set", "logging.record_output=false"]) == 0
    body = (sandbox / "claude_ai_initialization.log").read_text(encoding="utf-8")
    assert "# command:" in body  # the header is the file's, not the console's
    assert "mcp" not in body  # the printed table is not


def test_the_run_knows_where_it_is_being_recorded(sandbox) -> None:
    """A record nobody can find is not much of a record."""
    cli.main(["phases"])
    assert active_transcript() is None  # closed on the way out of main()
    close_transcript()


# -- when each line happened ----------------------------------------------------
#
# The log records have always carried a timestamp; the console narration between
# them did not, so the transcript could not answer "how long did that take?"
# about its own contents. Now both halves are stamped from the same clock, in
# the same format -- except where a stamp would break the shape of the thing.


STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def rendered(**kwargs) -> list[str]:
    # The glyph set is pinned because a StringIO reports no encoding, so "auto"
    # would quietly fall back to ASCII and the box assertions would pass by
    # never finding a box.
    buf = io.StringIO()
    kwargs.setdefault("glyph_set", "unicode")
    console = Console(stream=buf, color=False, **kwargs)
    console.write("a plain line")
    console.write("")
    console.rule("a section")
    console.box(["inside a box"], title="TITLE")
    console.table([["a", "b"], ["c", "d"]], headers=["one", "two"])
    return buf.getvalue().splitlines()


def test_a_line_that_records_something_is_stamped() -> None:
    lines = rendered()
    assert STAMP.match(lines[0]), lines[0]
    assert lines[0].endswith("a plain line")


def test_a_blank_line_stays_blank() -> None:
    """A timestamp on its own records nothing, and the space is doing a job."""
    assert "" in rendered()


def test_a_section_heading_is_stamped_and_still_fits_the_width() -> None:
    line = next(ln for ln in rendered() if "a section" in ln)
    assert STAMP.match(line)
    assert len(line) == 80, "the rule is shortened by the stamp, not pushed past the edge"


def test_a_box_is_one_event_rather_than_a_line_each() -> None:
    """Stamping the frame would break its alignment and repeat one second."""
    box = [ln for ln in rendered() if set(ln) & set("╭╰│┌└")]
    assert box, "expected a framed box"
    assert not any(STAMP.match(ln) for ln in box)
    assert all(len(ln) == 80 for ln in box), "alignment intact"


def test_table_rows_are_not_stamped_so_the_columns_line_up() -> None:
    rows = [ln for ln in rendered() if ln.strip().startswith(("one", "a ", "c "))]
    assert rows
    assert not any(STAMP.match(ln) for ln in rows)


def test_stamping_can_be_turned_off_entirely() -> None:
    assert not any(STAMP.match(ln) for ln in rendered(timestamps=False))


def test_the_format_is_the_one_the_log_records_use() -> None:
    buf = io.StringIO()
    Console(stream=buf, color=False, date_format="%H:%M:%S").write("x")
    assert re.match(r"^\d{2}:\d{2}:\d{2} x$", buf.getvalue().strip())


def test_the_transcript_gets_the_same_stamp_as_the_screen(tmp_path) -> None:
    """Both halves of the file have to be readable as one timeline."""
    transcript = Transcript.open(tmp_path / "run.log")
    assert transcript is not None
    buf = io.StringIO()
    Console(stream=buf, color=False, transcript=transcript).write("recorded once")
    transcript.close()

    on_screen = buf.getvalue().strip()
    in_file = next(ln for ln in (tmp_path / "run.log").read_text().splitlines() if "recorded once" in ln)
    assert STAMP.match(in_file)
    assert in_file == on_screen


def test_a_log_record_on_screen_is_stamped_like_the_line_above_it() -> None:
    """The two producers share a screen; one stamped and one not is worse than
    neither, because the eye reads the unstamped one as belonging to the last
    stamp it saw."""
    console = Console(stream=io.StringIO(), color=False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(BadgeFormatter(console))
    logger = logging.getLogger(f"{LOGGER_NAME}.stamp_demo")
    logger.handlers, logger.propagate, logger.level = [handler], False, logging.INFO

    logger.warning("something worth noting")

    assert STAMP.match(stream.getvalue()), stream.getvalue()
    assert "WARN" in stream.getvalue()


def test_the_switch_governs_the_narration_and_the_records_together() -> None:
    console = Console(stream=io.StringIO(), color=False, timestamps=False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(BadgeFormatter(console))
    logger = logging.getLogger(f"{LOGGER_NAME}.stamp_off")
    logger.handlers, logger.propagate, logger.level = [handler], False, logging.INFO

    logger.warning("quiet please")

    assert not STAMP.match(stream.getvalue())


def test_the_file_copy_of_a_record_is_not_stamped_twice() -> None:
    """The file handler supplies its own %(asctime)s; the screen's does not."""
    from initialize_claude_ai.core.logging_setup import DEFAULT_DATE_FORMAT

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", DEFAULT_DATE_FORMAT)
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "once", None, None)
    rendered = fmt.format(record)
    assert STAMP.match(rendered)
    assert len(STAMP.findall(rendered)) == 1
