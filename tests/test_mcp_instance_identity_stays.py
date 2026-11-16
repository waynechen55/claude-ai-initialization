"""The instance an entry serves stays in the entry.

The operator's ``~/.bashrc`` exported ``BUGZILLA_SERVER=https://bugzilla.opensuse.org``.
Re-checking ``bugzilla-opensuse-org`` (``BUGZILLA_SERVER: "https://bugzilla.opensuse.org"``),
the rule "a literal equal to what the operator exports under that name is an
earlier expansion -- write the reference" rewrote it as ``${BUGZILLA_SERVER}``,
and ``BUGZILLA_SERVER`` was then kept in Claude Code's global settings. The entry
now served whatever that generic variable says tomorrow, and bugzilla.example.com's
entry could never own the same name.

A value naming a host says which instance the entry serves: it is never
replaced by a reference to a variable that is not namespaced to the entry, and
an existing reference of that kind is written back as its value.
"""

from __future__ import annotations

import json
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://bugzilla.example.test"


def project_file(env: dict) -> None:
    (Path.cwd() / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bugzilla-example-test": {
                        "type": "stdio",
                        "command": "uvx",
                        "args": ["mcp-bugzilla", "--bugzilla-server", TARGET],
                        "env": env,
                    }
                }
            }
        )
    )


def candidate_for(ctx):
    [candidate] = ExistingRegistrations(ctx).for_target(TARGET, "package")
    return candidate


def test_a_literal_naming_the_instance_stays_even_when_the_shell_exports_it(ctx, sandbox, monkeypatch) -> None:
    monkeypatch.setenv("BUGZILLA_SERVER", TARGET)
    project_file({"BUGZILLA_SERVER": TARGET})
    candidate = candidate_for(ctx)
    assert "BUGZILLA_SERVER" not in candidate.references
    entry = McpWriter(ctx).entry_for(candidate, "bugzilla-example-test")
    assert entry["env"]["BUGZILLA_SERVER"] == TARGET


def test_an_existing_generic_reference_to_the_instance_is_written_back_as_its_value(ctx, sandbox, monkeypatch) -> None:
    """Self-healing: the entry the bad rule wrote is repaired by the next re-check."""
    monkeypatch.setenv("BUGZILLA_SERVER", TARGET)
    project_file({"BUGZILLA_SERVER": "${BUGZILLA_SERVER}"})
    entry = McpWriter(ctx).entry_for(candidate_for(ctx), "bugzilla-example-test")
    assert entry["env"]["BUGZILLA_SERVER"] == TARGET


def test_a_reference_namespaced_to_the_entry_is_the_operators_choice(ctx, sandbox, monkeypatch) -> None:
    monkeypatch.setenv("BUGZILLA_EXAMPLE_TEST_URL", TARGET)
    project_file({"BUGZILLA_SERVER": "${BUGZILLA_EXAMPLE_TEST_URL}"})
    candidate = candidate_for(ctx)
    assert candidate.references["BUGZILLA_SERVER"] == "${BUGZILLA_EXAMPLE_TEST_URL}"


def test_a_setting_that_names_no_host_keeps_its_reference(ctx, sandbox, monkeypatch) -> None:
    """The rule this narrows still holds for everything else: no user name is put into the file."""
    monkeypatch.setenv("BUGZILLA_EXAMPLE_TEST_USERNAME", "alice")
    project_file({"BUGZILLA_USER": "${BUGZILLA_EXAMPLE_TEST_USERNAME}", "MODE": "alice"})
    monkeypatch.setenv("MODE", "alice")
    candidate = candidate_for(ctx)
    assert candidate.references["BUGZILLA_USER"] == "${BUGZILLA_EXAMPLE_TEST_USERNAME}"
    assert candidate.references["MODE"] == "${MODE}"


def test_the_rule_is_configurable(settings) -> None:
    assert settings.bool_("mcp.discovery.reuse.keep_instance_literal") is True
