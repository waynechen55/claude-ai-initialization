"""Every kind of new terminal, every CLI scope, and every entry of the file -- judged the same way.

A login shell exported SSL_CERT_FILE as a deleted /tmp bundle while a terminal
window had the system's: a check that asked one shell saw half the machine.
`claude mcp get` reports project entries too ("Scope: Project config"), prints
``${X}`` for the file's ``${X:-default}``, and a scopeless `claude mcp remove`
took build-opensuse-org out of .mcp.json as "a CLI copy". bugzilla.opensuse.org
answered 429 while being identified and came out "logins offered: none seen".
The final check left entries no write path touched with no login slot at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


@pytest.fixture
def live(ctx, monkeypatch):
    """Writes the file for real; the CLI is reached only through a recording stub."""
    calls: list[list[str]] = []

    class Done:
        ok, simulated, stdout, stderr = True, False, "", ""

    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "claude")
    monkeypatch.setattr(ctx.runner, "run", lambda argv, **k: calls.append(list(argv)) or Done())
    ctx.dry_run = False
    ctx.calls = calls
    return ctx


# -- fix 6: every kind of new terminal -------------------------------------------------------------


def test_terminals_that_disagree_are_judged_by_the_one_that_fails(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import claude_check

    good = "/etc/hosts"  # a file that exists, standing for a working bundle
    monkeypatch.setattr(
        claude_check,
        "shell_values_each",
        lambda c, names: {
            "a new login shell": {"SSL_CERT_FILE": "/tmp/gone-for-good/ca-bundle.pem"},
            "a new interactive shell": {"SSL_CERT_FILE": good},
        },
    )
    assert claude_check.shell_values(ctx, ["SSL_CERT_FILE"]) == {"SSL_CERT_FILE": "/tmp/gone-for-good/ca-bundle.pem"}


def test_the_shell_kinds_are_the_per_machine_writer_s(ctx) -> None:
    kinds = ctx.settings.dict_("machine_env.effective_shells", {})
    assert len(kinds) >= 2 and ctx.settings.bool_("mcp.registration.fresh_env.use_effective_shells", False)


# -- fix 5: "later" is not an answer -----------------------------------------------------------------


def test_a_busy_host_is_waited_for_as_it_asked(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import traditional

    class Answer:
        headers = {"Retry-After": "12"}

    assert traditional._retry_after(Answer()) == 12.0
    assert traditional._retry_after(object()) == 0.0
    slept: list[float] = []
    monkeypatch.setattr(traditional.time, "sleep", lambda s: slept.append(s))
    ctx.settings.set("mcp.verification.login_check.temporary_retry_seconds", 5.0)
    ctx.settings.set("mcp.verification.login_check.temporary_retry_max_seconds", 30.0)
    probe = traditional.TraditionalPipeline.__new__(traditional.TraditionalPipeline)
    probe.settings = ctx.settings
    probe._identify_pause(12.0, 0)
    probe._identify_pause(0.0, 1)
    assert slept == [12.0, 10.0], "what it asked for, else a pause that doubles"


def test_an_undecided_address_is_asked_again_once_and_not_at_once(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import traditional
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    probe = traditional.TraditionalPipeline.__new__(traditional.TraditionalPipeline)
    probe.ctx, probe.settings = ctx, ctx.settings
    probe.discovery = ctx.settings.section("mcp.discovery")
    outcome = TargetOutcome(target="https://busy.example.test")
    outcome.identification_undecided = "busy.example.test answered HTTP 429 while it was being identified"
    clock = [1000.0]
    monkeypatch.setattr(traditional.time, "monotonic", lambda: clock[0])
    ctx.state["mcp_reidentified"] = {outcome.target: (0, clock[0])}
    assert not probe._reidentify_now(outcome), "not at once"
    clock[0] += 61
    assert probe._reidentify_now(outcome) and not probe._reidentify_now(outcome), "once more, then no more"


# -- fix 10: the CLI's scopes ---------------------------------------------------------------------


def test_a_project_scope_entry_is_never_a_cli_copy(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    report = (
        "build-example-org:\n  Scope: Project config (shared via .mcp.json)\n  Status: ✔ Connected\n"
        "  Type: stdio\n  Command: ${ICA_MCP_PYTHON3_13}\n  Args: /srv/bridge.py\n"
    )
    monkeypatch.setattr(ExistingRegistrations, "_cli", lambda self, key, name="": report)
    assert ExistingRegistrations.for_run(ctx).cli_entry("build-example-org") == {}
    local = report.replace("Project config (shared via .mcp.json)", "Local config (private to you in this project)")
    monkeypatch.setattr(ExistingRegistrations, "_cli", lambda self, key, name="": local)
    assert ExistingRegistrations.for_run(ctx).cli_entry("build-example-org")["command"] == "${ICA_MCP_PYTHON3_13}"


def test_a_default_the_cli_does_not_print_is_no_difference() -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import _same_copy, _same_launch

    file_entry = {"type": "stdio", "command": "${ICA_MCP_UVX:-/usr/bin/uvx}", "args": ["pkg"], "env": {"K": "${A_K:-}"}}
    printed = {"type": "stdio", "command": "${ICA_MCP_UVX}", "args": ["pkg"], "env": {"K": "${A_K}"}}
    assert _same_launch(file_entry, printed) and _same_copy(file_entry, printed)
    assert not _same_launch(file_entry, dict(printed, args=["other"]))


def test_a_remove_always_names_its_scope_and_none_is_run_without_one(live, monkeypatch) -> None:
    writer = McpWriter(live)
    monkeypatch.setattr(McpWriter, "cli_scopes", lambda self, name: [])
    assert writer._remove_from_cli("claude", "build-example-org", "retire") is True
    assert live.calls == [], "no CLI scope holds it: nothing removed -- a bare remove takes the project file's"
    assert "--scope <local|user>" in writer.remove_by_hand("build-example-org")
    monkeypatch.setattr(McpWriter, "cli_scopes", lambda self, name: ["local", "user"])
    writer._remove_from_cli("claude", "build-example-org", "retire")
    assert [c[-2:] for c in live.calls] == [["--scope", "local"], ["--scope", "user"]]
    assert writer.remove_by_hand("build-example-org").count("--scope") == 2


def test_the_scope_get_reports_is_read_when_the_configuration_says_nothing(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    monkeypatch.setattr(ExistingRegistrations, "cli_config_entries", lambda self: {})
    monkeypatch.setattr(
        ExistingRegistrations,
        "_cli",
        lambda self, key, name="": "x:\n  Scope: User config (available in all your projects)\n",
    )
    assert McpWriter(ctx).cli_scopes("x") == ["user"]
    monkeypatch.setattr(
        ExistingRegistrations, "_cli", lambda self, key, name="": "x:\n  Scope: Project config (shared via .mcp.json)\n"
    )
    assert McpWriter(ctx).cli_scopes("x") == []


# -- fix 7: the final check makes every file entry what this run would write ------------------------


def _file(sandbox: Path, servers: dict) -> Path:
    path = sandbox / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_the_final_check_gives_a_file_entry_its_missing_login_slot(live, sandbox, tmp_path) -> None:
    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    script = tmp_path / "graphql_server.py"
    script.write_text('import os\nTOKEN = os.environ.get("API_ACCESS_TOKEN")\n')
    path = _file(
        sandbox,
        {
            "gitlab-example-test": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(script)],
                "env": {"GRAPHQL_ENDPOINT": "https://gitlab.example.test/api/graphql"},
            }
        },
    )
    rows = McpWriter(live).verify_written(["gitlab-example-test"])
    env = json.loads(path.read_text())["mcpServers"]["gitlab-example-test"]["env"]
    assert env["API_ACCESS_TOKEN"] == "${GITLAB_EXAMPLE_TEST_API_ACCESS_TOKEN:-}"
    assert any("added the credential slot API_ACCESS_TOKEN" in r["change"] for r in rows)


def test_the_final_check_renames_generic_references_and_keeps_the_login(live, sandbox) -> None:
    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    live.claude_home.mkdir(parents=True, exist_ok=True)
    (live.claude_home / "settings.json").write_text(
        json.dumps({"env": {"JIRA_PERSONAL_TOKEN": "tok-12345678-abcd"}})  # gitleaks:allow
    )
    path = _file(
        sandbox,
        {
            "jira-example-test": {
                "type": "stdio",
                "command": "uvx",
                "args": ["mcp-atlassian"],
                "env": {"JIRA_URL": "https://jira.example.test", "JIRA_PERSONAL_TOKEN": "${JIRA_PERSONAL_TOKEN}"},
            }
        },
    )
    writer = McpWriter(live)
    writer.verify_written(["jira-example-test"])
    env = json.loads(path.read_text())["mcpServers"]["jira-example-test"]["env"]
    assert env["JIRA_PERSONAL_TOKEN"] == "${JIRA_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN}"
    assert writer._claude_settings_env().get("JIRA_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN") == "tok-12345678-abcd"


def test_the_final_check_moves_a_literal_credential_out_of_the_file(live, sandbox) -> None:
    live.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    path = _file(
        sandbox,
        {
            "bugzilla-example-org": {
                "type": "stdio",
                "command": "uvx",
                "args": ["mcp-bugzilla"],
                "env": {
                    "BUGZILLA_API_KEY": "Zq8xY2kL0pW4nB6vR1tC3mH5jF7dS9aE0uI2oP4g"  # gitleaks:allow
                },
            }
        },
    )
    writer = McpWriter(live)
    writer.verify_written(["bugzilla-example-org"])
    text = path.read_text()
    assert "Zq8xY2kL0pW4nB6vR1tC3mH5jF7dS9aE0uI2oP4g" not in text
    assert (
        json.loads(text)["mcpServers"]["bugzilla-example-org"]["env"]["BUGZILLA_API_KEY"]
        == "${BUGZILLA_EXAMPLE_ORG_BUGZILLA_API_KEY}"
    )
    assert writer._claude_settings_env()["BUGZILLA_EXAMPLE_ORG_BUGZILLA_API_KEY"].startswith("Zq8x")


# -- fix 9: where each value comes from ----------------------------------------------------------------


def test_each_value_s_origin_is_named_never_its_value(ctx) -> None:
    ctx.claude_home.mkdir(parents=True, exist_ok=True)
    (ctx.claude_home / "settings.json").write_text(json.dumps({"env": {"A_TOKEN": "secret-value-1"}}))
    origins = McpWriter(ctx).value_origins(
        {
            "command": "${ICA_MCP_UVX:-/usr/bin/uvx}",
            "env": {"T": "${A_TOKEN}", "O": "${B_OPTIONAL:-}", "M": "${C_MISSING}"},
        }
    )
    assert origins == {
        "ICA_MCP_UVX": "the entry's default",
        "A_TOKEN": "Claude Code's settings",
        "B_OPTIONAL": "unset (optional)",
        "C_MISSING": "unset",
    }
    assert "secret-value-1" not in json.dumps(origins)


def test_phase_1_repairs_broken_trust_on_the_machine_the_file_was_copied_to(ctx, monkeypatch) -> None:
    from initialize_claude_ai.core.report import Status
    from initialize_claude_ai.mcp_auto_discovery import consumer_trust
    from initialize_claude_ai.phases import phase1_install

    class Fixed:
        variable, value, why = "SSL_CERT_FILE", "/etc/ssl/ca-bundle.pem", "SSL_CERT_FILE named a deleted file"

    monkeypatch.setattr(consumer_trust, "repair_broken", lambda c, skip=(): [Fixed()])
    added: list[tuple] = []

    class Result:
        def add(self, *a, **k):
            added.append(a)

    phase = phase1_install.InstallPhase.__new__(phase1_install.InstallPhase)
    phase.ctx, phase.console = ctx, ctx.console
    ctx.dry_run = False
    phase._repair_trust(Result())
    assert added and added[0][1] == Status.CHANGED and "deleted file" in added[0][2]


def test_a_bot_check_is_try_later_and_https_is_kept(ctx, monkeypatch) -> None:
    """openqa.opensuse.org showed "Making sure you're not a bot!" over https; the cleartext endpoint was written."""
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    verifier = CandidateVerifier(ctx)
    wall = Handshake(
        ok=False,
        status=200,
        error="response was not a JSON-RPC message (HTTP 200 text/html: "
        "<title>Making sure you&#39;re not a bot!</title>)",
    )
    assert verifier.answered_try_later(wall) and verifier.looks_transient(wall)
    monkeypatch.setattr(CandidateVerifier, "_verify_url", lambda self, c: wall)
    candidate = Candidate(kind="url", name="openqa", url="http://openqa.example.org/mcp")
    assert verifier._prefer_https(candidate) and candidate.url == "https://openqa.example.org/mcp"


def test_an_existing_entry_is_read_as_claude_code_expands_it(ctx, tmp_path, monkeypatch) -> None:
    """${ICA_MCP_GITHUB_MCP_SERVER:-/root/go/bin/...} was "not installed" while the settings named the binary."""
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    monkeypatch.delenv("ICA_MCP_GITHUB_MCP_SERVER", raising=False)

    binary = tmp_path / "github-mcp-server"
    binary.write_text("#!/bin/sh\n")
    ctx.claude_home.mkdir(parents=True, exist_ok=True)
    (ctx.claude_home / "settings.json").write_text(json.dumps({"env": {"ICA_MCP_GITHUB_MCP_SERVER": str(binary)}}))
    reader = ExistingRegistrations.for_run(ctx)
    assert reader._expand("${ICA_MCP_GITHUB_MCP_SERVER:-/root/go/bin/github-mcp-server}") == str(binary)
