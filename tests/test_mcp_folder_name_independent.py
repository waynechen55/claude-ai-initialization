"""The program's folder may be called anything -- claude_ai_initialization here, claude-ai-initialization
once cloned from GitHub -- and moved anywhere. Paths into it are written through ICA_MCP_PROJECT, and an
existing .mcp.json naming the old folder is re-pointed by each path's own tail, never by a list of names."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project(ctx, tmp_path, monkeypatch):
    folder = tmp_path / "work" / "claude-ai-initialization"
    (folder / "bridges").mkdir(parents=True)
    (folder / "bridges" / "mcp_client_bridge_example_test.py").write_text("print('x')\n")
    ctx.settings.set("mcp.mcp_json_path", str(folder / ".mcp.json"))
    ctx.settings.set("mcp.registration.artifacts_home", str(folder / "mcp-servers"))
    return folder


def _writer(ctx):
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    return McpWriter(ctx)


def test_a_path_into_the_project_folder_is_written_through_its_variable(ctx, project) -> None:
    writer = _writer(ctx)
    roots = writer.path_roots()
    script = str(project / "bridges" / "mcp_client_bridge_example_test.py")
    assert (
        writer.portable(script, roots) == f"${{ICA_MCP_PROJECT:-{project}}}/bridges/mcp_client_bridge_example_test.py"
    )
    served = str(project / "mcp-servers" / "example-test" / "m.json")
    assert writer.portable(served, roots).startswith("${ICA_MCP_SERVERS:-"), "the more specific root wins"
    assert "ICA_MCP_PROJECT" not in writer.portable("/etc/ssl/ca-bundle.pem", roots), (
        "outside the project: never its variable"
    )
    assert writer.resolve_program_variable("ICA_MCP_PROJECT") == str(project)


def test_the_home_folder_never_stands_for_the_project(ctx, tmp_path) -> None:
    home = Path.home()
    ctx.settings.set("mcp.mcp_json_path", str(home / ".mcp.json"))
    assert "ICA_MCP_PROJECT" not in dict(_writer(ctx).path_roots())


def test_an_old_folder_name_is_re_pointed_by_the_path_s_own_tail(ctx, project) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate

    old = "/home/someone/claude_project/claude_ai_initialization/bridges/mcp_client_bridge_example_test.py"
    candidate = Candidate(
        kind="stdio",
        name="example-test",
        command=["python3", old],
        env={"MANIFEST": "/elsewhere/claude_ai_initialization/bridges/mcp_client_bridge_example_test.py"},
    )
    moved = _writer(ctx).repoint_missing(candidate)
    assert candidate.command[1] == str(project / "bridges" / "mcp_client_bridge_example_test.py")
    assert candidate.env["MANIFEST"] == candidate.command[1] and len(moved) == 2


def test_a_tail_found_in_two_places_is_never_guessed(ctx, project, tmp_path, monkeypatch) -> None:
    writer = _writer(ctx)
    other = tmp_path / "other"
    (other / "bridges").mkdir(parents=True)
    (other / "bridges" / "mcp_client_bridge_example_test.py").write_text("print('y')\n")
    monkeypatch.setattr(writer, "_program_roots", lambda: [project, other])
    assert writer._found_by_tail(Path("/old/x/bridges/mcp_client_bridge_example_test.py")) is None


def test_re_pointing_by_tail_can_be_turned_off(ctx, project) -> None:
    ctx.settings.set("mcp.registration.repoint_by_relative_path", False)
    assert _writer(ctx)._found_by_tail(Path("/old/x/bridges/mcp_client_bridge_example_test.py")) is None


def test_packaged_settings(settings) -> None:
    assert settings.dict_("mcp.registration.path_variables")["ICA_MCP_PROJECT"] == "."
    assert settings.str_list("mcp.registration.path_root_never") == ["/", "~"]
    assert settings.bool_("mcp.registration.repoint_by_relative_path") is True


def test_phase_1_sets_the_project_variable_to_where_the_folder_is_now(ctx, project, monkeypatch) -> None:
    import json

    from initialize_claude_ai.mcp_auto_discovery import entry_needs

    entry = {
        "command": "python3",
        "args": [
            "${ICA_MCP_PROJECT:-/home/someone/claude_ai_initialization}/bridges/mcp_client_bridge_example_test.py"
        ],
    }
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"example-test": entry}}))
    monkeypatch.setattr(entry_needs, "_entries", lambda c: {"example-test": (entry, str(project / ".mcp.json"))})
    found = entry_needs.machine_paths(ctx, apply=False)
    assert found.set.get("ICA_MCP_PROJECT") == str(project)


def test_an_existing_entry_is_judged_with_its_files_found_again(ctx, project, monkeypatch) -> None:
    """gitlab-example-net: re-pointed for its trial, then judged "does not start as written" from the old path."""
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    verifier = CandidateVerifier(ctx)
    old = "/home/someone/claude_ai_initialization/bridges/mcp_client_bridge_example_test.py"
    entry = {
        "command": "python3",
        "args": [old],
        "env": {"CA": "${CA_X:-/home/someone/claude_ai_initialization/bridges/mcp_client_bridge_example_test.py}"},
    }
    seen = []
    monkeypatch.setattr(
        verifier, "verify_as_written", lambda e, n, **k: seen.append(e) or Handshake(ok=True, tools=["t"])
    )
    candidate = Candidate(kind="stdio", name="example-test", command=["python3", old])
    assert verifier.as_claude_runs_it("example-test", entry, "project_file", candidate) == ""
    here = str(project / "bridges" / "mcp_client_bridge_example_test.py")
    assert seen[0]["args"] == [here] and seen[0]["env"]["CA"] == f"${{CA_X:-{here}}}"
