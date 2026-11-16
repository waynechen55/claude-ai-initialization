"""gitlab-example-net connected without its token; the verdict called kept, written entries missing.

* Written entries' files lived in this program's state folder, so a copied
  .mcp.json arrived without them; gitlab-example-net was pointed at an older script
  reading GITLAB_TOKEN while the entry passed GITLAB_EXAMPLE_NET_TOKEN. Files now
  live beside the file that names them, and ICA_MCP_SERVERS names the folder.
* The "script reads this slot" check ran only in trials. The final file check
  now compares every entry's credential slots with its script.
* build-example-net and build-opensuse-org were in .mcp.json and reported missing
  from it: the verdict counts what the file holds for an address.
"""

from __future__ import annotations

import json
from pathlib import Path

from initialize_claude_ai.core.paths_home import artifacts_home
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, TargetOutcome

BRIDGE = """import os
TOKEN = os.environ.get("GITLAB_TOKEN", "")
TIMEOUT = float(os.environ.get("MCP_BRIDGE_TIMEOUT", "30"))
"""


# -- files travel with the file -------------------------------------------------------------------


def test_the_servers_folder_lies_beside_the_file_entries_are_written_to(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    project = sandbox / "project"
    project.mkdir()
    ctx.settings.set("mcp.mcp_json_path", str(project / ".mcp.json"))
    assert artifacts_home(ctx.settings) == (project / "mcp-servers").resolve()
    roots = dict(McpWriter(ctx).path_roots())
    assert roots["ICA_MCP_SERVERS"] == str((project / "mcp-servers").resolve())
    assert McpWriter(ctx).resolve_program_variable("ICA_MCP_SERVERS") == str((project / "mcp-servers").resolve())


def test_a_scratch_file_moves_into_the_project_and_the_entry_names_it_through_the_variable(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    project = sandbox / "project"
    project.mkdir()
    ctx.settings.set("mcp.mcp_json_path", str(project / ".mcp.json"))
    own = Path(ctx.work_dir) / "agent-workspace" / "artifacts" / "gitlab-example-test"
    own.mkdir(parents=True)
    (own / "bridge.py").write_text(BRIDGE)
    candidate = Candidate(kind="stdio", name="gitlab-example-test", command=["python3", str(own / "bridge.py")])
    writer = McpWriter(ctx)
    writer.own_files(candidate, "gitlab-example-test")
    assert Path(candidate.command[1]).is_relative_to((project / "mcp-servers").resolve())
    entry = writer.entry_for(candidate, "gitlab-example-test")
    assert any("${ICA_MCP_SERVERS:-" in str(a) for a in entry.get("args") or [])


# -- credential slots checked against the script, in the file -------------------------------------


def _entry(script: Path, env: dict) -> dict:
    return {"type": "stdio", "command": "python3", "args": ["-I", str(script)], "env": env}


def test_a_slot_the_script_never_reads_is_renamed_to_the_one_it_does(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    script = tmp_path / "mcp_bridge_gitlab.py"
    script.write_text(BRIDGE)
    fixed, change = McpWriter(ctx).align_entry_slots(
        _entry(script, {"GITLAB_EXAMPLE_NET_TOKEN": "${GITLAB_EXAMPLE_NET_TOKEN:-}"})
    )
    assert fixed is not None and fixed["env"] == {"GITLAB_TOKEN": "${GITLAB_EXAMPLE_NET_TOKEN:-}"}
    assert "renamed to GITLAB_TOKEN" in change


def test_an_unread_slot_with_no_single_target_is_reported_not_guessed(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE + 'OTHER = os.environ.get("OTHER_API_TOKEN", "")\n')
    fixed, change = McpWriter(ctx).align_entry_slots(_entry(script, {"WRONG_TOKEN": "${WRONG_TOKEN:-}"}))
    assert fixed is None and "never reaches the server" in change
    assert McpWriter(ctx).align_entry_slots(_entry(script, {"GITLAB_TOKEN": "x"})) == (None, "")
    assert McpWriter(ctx).align_entry_slots({"command": "uvx", "args": ["mcp-atlassian"], "env": {"X_TOKEN": "y"}}) == (
        None,
        "",
    ), "a package keeps its keys"


def test_the_final_file_check_repairs_every_entry_s_slots(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    script = sandbox / "mcp_bridge_gitlab.py"
    script.write_text(BRIDGE)
    # The entry is named for the instance its variable names (gitlab-example-net ->
    # GITLAB_EXAMPLE_NET_TOKEN), so the owned-name rule leaves the reference alone.
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "gitlab-example-net": _entry(script, {"GITLAB_EXAMPLE_NET_TOKEN": "${GITLAB_EXAMPLE_NET_TOKEN:-}"})
                }
            }
        )
    )
    rows = McpWriter(ctx).verify_written(["some-other-entry"])
    assert any(r["name"] == "gitlab-example-net" and "renamed to GITLAB_TOKEN" in r["change"] for r in rows)
    env = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["gitlab-example-net"]["env"]
    assert env == {"GITLAB_TOKEN": "${GITLAB_EXAMPLE_NET_TOKEN:-}"}


# -- the verdict reads the file -------------------------------------------------------------------


def test_a_kept_entry_in_the_file_counts_as_the_address_s(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"build-example-test": {"command": "python3"}}}))
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.answered_existing["build-example-test"] = "claude_cli"
    McpPhase(ctx)._reconcile_with_file(McpWriter(ctx), outcome)
    assert outcome.registered == ["build-example-test"]
    ctx.settings.set("mcp.registration.verdict_reads_file", False)
    other = TargetOutcome(target="https://build.example.test")
    other.answered_existing["build-example-test"] = "claude_cli"
    McpPhase(ctx)._reconcile_with_file(McpWriter(ctx), other)
    assert other.registered == []


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.core.paths_home import DEFAULT_ARTIFACTS_HOME

    assert settings.str_("mcp.registration.artifacts_home") == DEFAULT_ARTIFACTS_HOME
    assert settings.bool_("mcp.registration.align_slots_in_file") is True
    assert settings.bool_("mcp.registration.verdict_reads_file") is True


def test_a_variable_the_entry_s_manifest_names_counts_as_read(ctx, tmp_path) -> None:
    """A client or generated bridge reads the variables its manifest lists: no false "never reaches"."""
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    script = tmp_path / "mcp_client_bridge.py"
    script.write_text(
        'import os, json\nM = json.load(open(os.environ["MCP_CLIENT_MANIFEST"]))\n'
        'values = {k: os.environ.get(k, "") for k in M["parts"]}\n'
    )
    manifest = tmp_path / "client.json"
    manifest.write_text(json.dumps({"parts": {"BUILD_EXAMPLE_PASSWORD": "pass = {value}"}}))
    entry = _entry(
        script, {"BUILD_EXAMPLE_PASSWORD": "${BUILD_EXAMPLE_PASSWORD:-}", "MCP_CLIENT_MANIFEST": str(manifest)}
    )
    assert McpWriter(ctx).align_entry_slots(entry) == (None, "")


def test_files_in_an_earlier_home_move_into_the_project_too(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    project = sandbox / "project"
    project.mkdir()
    ctx.settings.set("mcp.mcp_json_path", str(project / ".mcp.json"))
    legacy = sandbox / "state" / "old-servers"
    (legacy / "build-example-test").mkdir(parents=True)
    (legacy / "build-example-test" / "bridge.py").write_text("print()\n")
    ctx.settings.set("mcp.registration.legacy_artifacts_homes", [str(legacy)])
    candidate = Candidate(
        kind="stdio", name="build-example-test", command=["python3", str(legacy / "build-example-test" / "bridge.py")]
    )
    McpWriter(ctx).own_files(candidate, "build-example-test")
    assert Path(candidate.command[1]).is_relative_to((project / "mcp-servers").resolve())
    assert Path(candidate.command[1]).is_file()
