"""``--mcp-recheck all``: a ``.mcp.json`` copied from another machine is proved on this one.

The file from 2026-10-22 was written under ``/srv/projects`` and copied
to a machine where the project lives under ``/home/user/projects``.
Its paths had to be rewritten by hand, its start-up wait was set in the other
machine's settings.json, and its credentials lived in the other machine's env.
Verification is per machine; re-checking every entry here is one flag, and the
address each entry serves is found by the program's own rules -- never guessed.
"""

from __future__ import annotations

import json

from initialize_claude_ai.mcp_auto_discovery import known_good
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def write_project_file(ctx, sandbox, servers: dict) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_each_entry_is_tied_to_the_address_it_serves(ctx, sandbox) -> None:
    write_project_file(
        ctx,
        sandbox,
        {
            "bugzilla-example-test": {
                "command": "uvx",
                "args": ["mcp-bugzilla", "--bugzilla-server", "https://bugzilla.example.test"],
            },
            "openqa-example-test": {"type": "http", "url": "https://openqa.example.test/mcp"},
            "gitlab-example-test": {
                "command": "python3",
                "args": ["b.py"],
                "env": {"MCP_BRIDGE_BASE_URL": "https://gitlab.example.test/api/graphql"},
            },
        },
    )
    addresses, unplaced = McpWriter(ctx).served_addresses()
    assert addresses == ["https://bugzilla.example.test", "https://openqa.example.test", "https://gitlab.example.test"]
    assert unplaced == []


def test_the_record_of_its_last_proof_names_the_address_first(ctx, sandbox, monkeypatch) -> None:
    """developers-google-com reaches developerknowledge.googleapis.com; its address is developers.google.com."""
    write_project_file(ctx, sandbox, {"docs-example-test": {"type": "http", "url": "https://api.other.test/mcp"}})
    monkeypatch.setattr(
        known_good,
        "load",
        lambda _ctx: {"https://docs.example.test/knowledge": [{"name": "docs-example-test", "entry": {}}]},
    )
    addresses, unplaced = McpWriter(ctx).served_addresses()
    assert addresses == ["https://docs.example.test/knowledge"] and unplaced == []


def test_an_entry_nothing_ties_to_an_address_is_named_never_guessed(ctx, sandbox) -> None:
    write_project_file(
        ctx,
        sandbox,
        {
            "my-own-name": {"type": "http", "url": "https://somewhere.example.test/mcp"},
            "two-hosts": {"command": "x", "env": {"A": "https://a.example.test", "B": "https://b.example.test"}},
        },
    )
    addresses, unplaced = McpWriter(ctx).served_addresses()
    assert addresses == []
    assert unplaced == ["my-own-name", "two-hosts"]


def test_the_word_expands_only_when_rechecking(ctx, sandbox) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    write_project_file(
        ctx, sandbox, {"bugzilla-example-test": {"type": "http", "url": "https://bugzilla.example.test/mcp"}}
    )
    phase = McpPhase(ctx)
    assert phase._expand_recheck_all(["all"]) == ["all"], "outside --mcp-recheck, 'all' is just a word"
    ctx.settings.set("mcp.recheck", True)
    ctx.state["mcp_servers"] = ["all", "jira.example.test"]
    assert phase._expand_recheck_all(["all", "jira.example.test"]) == [
        "jira.example.test",
        "https://bugzilla.example.test",
    ]
    assert ctx.state["mcp_servers"] == ["jira.example.test", "https://bugzilla.example.test"], (
        "every other reader of the addresses sees them, not the word"
    )


def test_the_word_is_configuration(settings) -> None:
    assert settings.str_("mcp.recheck_all_word") == "all"
