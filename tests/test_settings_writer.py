"""The two primitives behind an auditable settings.json edit: ``drop`` and ``diff``.

Everything phase 3 writes goes through them, and a retraction is the one edit
that can make a working configuration stop working, so the edge cases are worth
pinning down on their own rather than only through the phase.
"""

from __future__ import annotations

from initialize_claude_ai.provision.settings_writer import diff, drop


def test_drop_removes_a_nested_key_and_says_it_did() -> None:
    document = {"env": {"CLAUDE_CODE_USE_VERTEX": "1", "HTTPS_PROXY": "http://p:3128"}}
    assert drop(document, "env.CLAUDE_CODE_USE_VERTEX") is True
    assert document == {"env": {"HTTPS_PROXY": "http://p:3128"}}


def test_drop_on_something_that_was_not_there_changes_nothing() -> None:
    """The caller names every variable it *could* have written, not every one
    that is present, so most names on a given run are simply absent."""
    document = {"env": {"HTTPS_PROXY": "http://p:3128"}}
    assert drop(document, "env.AWS_REGION") is False
    assert drop(document, "permissions.allow") is False  # no such parent
    assert drop(document, "env.HTTPS_PROXY.port") is False  # parent is not a dict
    assert document == {"env": {"HTTPS_PROXY": "http://p:3128"}}


def test_an_emptied_container_is_left_in_place() -> None:
    """``env: {}`` is something the operator can see and understand.

    Pruning it would reshape a part of their document that was not the point of
    the edit, and the diff would have no honest way to describe having done so.
    """
    document = {"env": {"AWS_REGION": "us-east-1"}}
    drop(document, "env.AWS_REGION")
    assert document == {"env": {}}


def test_diff_reports_additions_changes_and_removals() -> None:
    before = {"env": {"A": "1", "B": "keep"}, "model": "opus"}
    after = {"env": {"B": "keep", "C": "new"}, "model": "sonnet"}
    described = {change.key: change for change in diff(before, after)}

    assert described["env.C"].is_new
    assert described["env.C"].describe() == "env.C = new"
    assert described["model"].describe() == "model: opus -> sonnet"
    assert described["env.A"].is_removal
    assert described["env.A"].describe() == "env.A: 1 -> (removed)"
    assert "env.B" not in described


def test_a_removal_is_not_mistaken_for_an_addition() -> None:
    """``is_new`` and ``is_removal`` both test against the same sentinel."""
    removal = next(c for c in diff({"env": {"A": "1"}}, {"env": {}}) if c.key == "env.A")
    assert removal.is_removal and not removal.is_new
