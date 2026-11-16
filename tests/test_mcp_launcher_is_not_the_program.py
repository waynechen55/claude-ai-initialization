"""A launcher is not the program, and installed is not runnable.

bugzilla-opensuse-org was written to run ``/root/.local/share/uv/tools/mcp-bugzilla/bin/mcp-bugzilla``:
a script whose first line runs that tool's private Python. Copied to another
machine it failed with ENOENT (the interpreter is not there); pointed at the
system's Python it failed to import mcp_bugzilla (Connection closed). The same
package by its launcher form -- ``uvx mcp-bugzilla==0.21.0`` -- starts anywhere.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery import launch_form
from initialize_claude_ai.mcp_auto_discovery.types import Candidate

ROOT = Path(__file__).resolve().parents[1]


def _uv_tool(tmp_path: Path, package: str = "pkg-x", version: str = "1.2.3", script: str = "pkg-x") -> Path:
    venv = tmp_path / "share" / "uv" / "tools" / package
    meta = venv / "lib" / "python3.13" / "site-packages" / f"{package.replace('-', '_')}-{version}.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n")
    launcher = venv / "bin" / script
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{venv}/bin/python\nimport sys\nfrom {package.replace('-', '_')} import main\n")
    launcher.chmod(0o755)
    return launcher


def _which(found=("uvx", "npx")):
    return lambda name, *a, **k: f"/usr/bin/{name}" if name in found else None


# -- 1. the portable form ---------------------------------------------------------------------


def test_a_uv_tool_launcher_becomes_uvx_pinned_to_the_installed_version(tmp_path, settings) -> None:
    launcher = _uv_tool(tmp_path)
    portable, found = launch_form.launcher_form([str(launcher), "--flag"], settings)
    assert portable == ["uvx", "pkg-x==1.2.3", "--flag"] and found["family"] == "uv-tool"


def test_a_script_named_otherwise_is_launched_from_its_package(tmp_path, settings) -> None:
    launcher = _uv_tool(tmp_path, script="serve-x")
    portable, _ = launch_form.launcher_form([str(launcher)], settings)
    assert portable == ["uvx", "--from", "pkg-x==1.2.3", "serve-x"]


def test_another_machine_s_path_is_recognised_without_its_files(settings) -> None:
    """The entry copied from the run machine names a path that is not here: the path alone says what it is."""
    portable, found = launch_form.launcher_form(
        ["/root/.local/share/uv/tools/mcp-bugzilla/bin/mcp-bugzilla", "--read-only"],
        settings,
        install=[["uv", "tool", "install", "mcp-bugzilla==0.21.0"]],
    )
    assert portable == ["uvx", "mcp-bugzilla==0.21.0", "--read-only"] and found["package"] == "mcp-bugzilla"


def test_a_copied_launcher_names_its_install_by_its_first_line(tmp_path, settings) -> None:
    copy = tmp_path / "bin" / "mcp-bugzilla"
    copy.parent.mkdir()
    copy.write_text(
        "#!/root/.local/share/uv/tools/mcp-bugzilla/bin/python\nimport sys\nfrom mcp_bugzilla import main\n"
    )
    copy.chmod(0o755)
    assert launch_form.launcher_form([str(copy)], settings)[0] == ["uvx", "mcp-bugzilla"]


def test_an_npm_global_module_becomes_npx(tmp_path, settings) -> None:
    module = tmp_path / "lib" / "node_modules" / "@scope" / "srv"
    (module / "dist").mkdir(parents=True)
    (module / "package.json").write_text(json.dumps({"name": "@scope/srv", "version": "2.0.1"}))
    target = module / "dist" / "cli.js"
    target.write_text("#!/usr/bin/env node\n")
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "srv-mcp"
    link.symlink_to(target)
    assert launch_form.launcher_form([str(link)], settings)[0] == [
        "npx",
        "-y",
        "--package",
        "@scope/srv@2.0.1",
        "srv-mcp",
    ]


def test_anything_else_is_left_alone(settings) -> None:
    assert launch_form.launcher_form(["uvx", "mcp-bugzilla==0.21.0"], settings) == ([], {})
    assert launch_form.launcher_form([sys.executable, "/srv/bridge.py"], settings) == ([], {})


# -- 2. runnable, not present ---------------------------------------------------------------------


def test_a_launcher_of_another_machine_s_install_cannot_run(tmp_path) -> None:
    script = tmp_path / "x"
    script.write_text("#!/nowhere/uv/tools/x/bin/python\nimport sys\nfrom x import main\n")
    script.chmod(0o755)
    assert "is not on this machine" in launch_form.runnable(str(script))


def test_an_interpreter_without_the_package_cannot_run_it(tmp_path) -> None:
    script = tmp_path / "x"
    script.write_text(f"#!{sys.executable}\nimport sys\nfrom no_such_module_here_xyz import main\n")
    script.chmod(0o755)
    assert "cannot import no_such_module_here_xyz" in launch_form.runnable(str(script))
    script.write_text(f"#!{sys.executable}\nimport sys\nimport json\n")
    assert launch_form.runnable(str(script)) == "", "the standard library is no package to install"


def test_the_trial_says_why_a_present_launcher_cannot_run(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    script = tmp_path / "srv"
    script.write_text(f"#!{sys.executable}\nimport sys\nfrom no_such_module_here_xyz import main\n")
    script.chmod(0o755)
    ctx.dry_run = False  # the check answers before anything is launched
    shake = CandidateVerifier(ctx)._trial_stdio(Candidate(kind="stdio", name="s", command=[str(script)]), {})
    assert "cannot run here" in shake.error and "cannot import" in shake.error


def test_phase_1_counts_a_launcher_it_cannot_run_as_missing(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.entry_needs import _runnable

    script = tmp_path / "srv"
    script.write_text("#!/nowhere/bin/python\n")
    script.chmod(0o755)
    assert not _runnable(ctx, str(script)) and _runnable(ctx, sys.executable)


# -- every path: the trial, the writer, the final check, phase 1 ------------------------------------


def test_every_trial_launches_the_portable_form(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setattr(ctx.runner, "which", _which())
    launcher = _uv_tool(tmp_path)
    candidate = Candidate(
        kind="stdio",
        name="x",
        command=[str(launcher)],
        args=["--read-only"],
        install=[["uv", "tool", "install", "pkg-x"], ["apt", "install", "libfoo"]],
    )
    assert CandidateVerifier(ctx).use_launcher_form(candidate)
    assert candidate.full_command() == ["uvx", "pkg-x==1.2.3", "--read-only"]
    assert candidate.install == [["apt", "install", "libfoo"]], "the private install step is no longer needed"


def test_without_the_portable_launcher_nothing_is_rewritten_and_it_is_said(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setattr(ctx.runner, "which", _which(found=()))
    launcher = _uv_tool(tmp_path)
    candidate = Candidate(kind="stdio", name="x", command=[str(launcher)])
    assert not CandidateVerifier(ctx).use_launcher_form(candidate)
    assert candidate.command == [str(launcher)] and any("private launcher" in n for n in candidate.notes)


def test_the_final_check_rewrites_a_copied_entry(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    monkeypatch.setattr(ctx.runner, "which", _which())
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bugzilla-example-org": {
                        "type": "stdio",
                        "command": "${ICA_MCP_MCP_BUGZILLA:-/root/.local/share/uv/tools/mcp-bugzilla/bin/mcp-bugzilla}",
                        "args": ["--bugzilla-server", "https://bugzilla.example.org"],
                    }
                }
            }
        )
    )
    rows = McpWriter(ctx).verify_written(["bugzilla-example-org"])
    entry = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["bugzilla-example-org"]
    assert entry["command"] == "uvx" and entry["args"][:2] == ["mcp-bugzilla", "--bugzilla-server"]
    assert any("private launcher" in r["change"] for r in rows)


def test_phase_1_rewrites_it_on_the_machine_the_file_was_copied_to(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.entry_needs import repair_private_launchers

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    monkeypatch.setattr(ctx.runner, "which", _which())
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"x": {"command": "/home/someone/.local/share/uv/tools/pkg-y/bin/pkg-y", "args": ["--a"]}}}
        )
    )
    [line] = repair_private_launchers(ctx, apply=True)
    assert "uvx pkg-y" in line
    assert json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["x"] == {
        "command": "uvx",
        "args": ["pkg-y", "--a"],
    }


def test_the_final_check_says_a_present_launcher_cannot_run(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    script = tmp_path / "srv"
    script.write_text(f"#!{sys.executable}\nimport sys\nfrom no_such_module_here_xyz import main\n")
    script.chmod(0o755)
    _fixed, problems = McpWriter(ctx).check_as_run({"command": str(script)})
    assert any("cannot run here" in text and "copying a launcher" in text for text, _done in problems)


def test_the_switch_turns_it_off(ctx, tmp_path, settings) -> None:
    settings.set("mcp.launch_form.enabled", False)
    assert launch_form.launcher_form(["/x/uv/tools/p/bin/p"], settings) == ([], {})


# -- 4. every remove names its scope --------------------------------------------------------------


def test_no_remove_anywhere_is_written_without_its_scope() -> None:
    """Every `claude mcp remove` the program says or runs comes from remove_by_hand / the scope flag."""
    offenders = []
    for path in [*ROOT.glob("initialize_claude_ai/**/*.py"), *ROOT.glob("initialize_claude_ai/data/*.toml")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "mcp remove" not in line or "--scope" in line or "scope_flag" in line:
                continue
            if line.strip().startswith(("#", '"""', "``")) or "``claude mcp remove``" in line:
                continue  # a comment or docstring describing removes, never one the operator is told to run
            quoted = re.search(r"[f]?[\"'][^\"']*mcp remove[^\"']*[\"']", line)
            if (
                quoted
                and "never" not in line.lower()
                and "without" not in line.lower()
                and 'or f"claude mcp remove {name}"' not in line
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_the_usage_guidance_names_scopes_and_never_a_bare_remove(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    text = "\n".join(McpWriter(ctx).usage_instructions())
    assert "`claude mcp remove <name>`" not in text and "registered by this run at --scope" not in text
    assert "delete its entry there" in text
