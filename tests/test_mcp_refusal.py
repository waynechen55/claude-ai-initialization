"""A refused entry does not reach the file, the CLI or the briefing.

Both registration gates -- grounding and portability -- decide that an entry is
not worth having, set a reason, and skip the rest of their loop iteration. For
some time neither decision had any effect on the file. ``publish`` handed
*every* registration to ``_write_json``, which looped over all of them and wrote
``servers[name] = entry``; then ``_register`` ran on them too and overwrote the
refusal reason with something about the CLI. The result:

    skipped reason : the claude CLI is not installed
    written flag   : True
    ON DISK        : {"confluence-example-com": {"command": "npx",
                                              "args": ["-y", "@kubb/mcp"]}}

So the gates announced decisions they did not carry out, and the operator was
told neither the refusal nor the reason. Every test in this file asserts on the
bytes on disk rather than on a flag, because a flag was exactly what was right
while the file was wrong.

The two meanings are now separate fields: ``refused`` (no entry was written)
and ``skipped`` (the entry is in the file; the CLI step did not run).
"""

from __future__ import annotations

import json

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

WORKS = Handshake(ok=True, server_name="Example", tools=["a"], round_trip=True)


@pytest.fixture
def live(ctx, monkeypatch):
    """Really writes the file; runs no commands, as on a host without `claude`."""
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)
    ctx.dry_run = False
    return ctx


def servers_on_disk(sandbox) -> dict:
    path = sandbox / ".mcp.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("mcpServers", {})


def ungrounded() -> Candidate:
    """`npx -y @kubb/mcp`, registered as the MCP server for Confluence."""
    return Candidate(
        kind="stdio",
        name="confluence-example-com",
        transport=Transport.STDIO,
        command=["npx", "-y", "@kubb/mcp"],
        handshake=WORKS,
    )


def disposable(tmp_path) -> Candidate:
    """The openQA bridge: an interpreter inside a virtualenv built to trial it."""
    venv = tmp_path / "artifacts" / "openqa-mcp-bridge" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python3").write_text("")
    return Candidate(
        kind="stdio",
        name="openqa-opensuse-org",
        transport=Transport.STDIO,
        command=[str(venv / "bin" / "python3"), "-m", "openqa_mcp"],
        env={"OPENQA_SERVER": "openqa.opensuse.org"},
        handshake=WORKS,
    )


# --------------------------------------------------------------------------- #
# the file
# --------------------------------------------------------------------------- #


def test_an_ungrounded_entry_is_absent_from_the_file(live, sandbox) -> None:
    McpWriter(live).publish(TargetOutcome(target="https://confluence.example.com", verified=[ungrounded()]))
    assert servers_on_disk(sandbox) == {}


def test_a_disposable_entry_is_absent_from_the_file(live, sandbox, tmp_path) -> None:
    """The portability gate had the same defect, and for longer."""
    McpWriter(live).publish(TargetOutcome(target="https://openqa.opensuse.org", verified=[disposable(tmp_path)]))
    assert servers_on_disk(sandbox) == {}


def test_a_good_entry_in_the_same_run_is_still_written(live, sandbox) -> None:
    """One refusal must not take the rest of the run with it."""
    good = Candidate(
        kind="url",
        name="ok",
        url="https://mcp.example.test/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=WORKS,
    )
    McpWriter(live).publish(TargetOutcome(target="https://mcp.example.test", verified=[good]))
    assert list(servers_on_disk(sandbox)) == ["mcp-example-test"]


def test_a_refusal_does_not_disturb_what_is_already_in_the_file(live, sandbox) -> None:
    """Nothing is written at all, so an operator's own entries are untouched."""
    path = sandbox / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"mine": {"type": "stdio", "command": "x"}}}))

    McpWriter(live).publish(TargetOutcome(target="https://confluence.example.com", verified=[ungrounded()]))
    assert servers_on_disk(sandbox) == {"mine": {"type": "stdio", "command": "x"}}


# --------------------------------------------------------------------------- #
# the reason survives to be read
# --------------------------------------------------------------------------- #


def test_the_reason_is_not_overwritten_by_the_cli_step(live, sandbox) -> None:
    """`refused` and `skipped` are different questions and were one field.

    The CLI step used to run on a refused entry and replace its reason with
    "the claude CLI is not installed", which is true, irrelevant, and the only
    thing anybody was shown.
    """
    registration = McpWriter(live).publish(
        TargetOutcome(target="https://confluence.example.com", verified=[ungrounded()])
    )[0]

    assert "confluence.example.com" in registration.refused
    assert registration.written is False
    assert registration.registered is False
    assert registration.skipped == "", "the CLI step must not run on a refused entry"


def test_the_reason_reaches_the_json_report(live, sandbox) -> None:
    registration = McpWriter(live).publish(
        TargetOutcome(target="https://confluence.example.com", verified=[ungrounded()])
    )[0]
    payload = registration.to_dict()

    assert payload["refused"]
    assert payload["grounding"] == "ungrounded"


def test_the_briefing_names_it_rather_than_briefing_on_it(live, sandbox) -> None:
    """A refused server has no entry, so it gets no "export this" instructions."""
    writer = McpWriter(live)
    registrations = writer.publish(TargetOutcome(target="https://confluence.example.com", verified=[ungrounded()]))
    briefing = writer.briefing(registrations)

    assert "confluence-example-com" not in briefing["servers"]
    assert "confluence-example-com" in briefing["these_were_found_but_not_registered"]
    assert "why_they_were_refused" in briefing
