"""A full disk is seen before the run, stopped on when it happens, and this program's leftovers are given back.

A run on another machine started on a full disk: phase 1 reported OK for
installs that had just failed with "No space left on device", phase 4 printed a
traceback per address while copying the agent's skills, then the log itself
stopped and no .mcp.json was written -- with nothing saying why. The machine
also held 683 temporary directories the client bridge had left, one per start.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from initialize_claude_ai.core import diskspace
from initialize_claude_ai.core.report import PhaseResult, Status


def test_a_full_disk_is_recognised_in_every_shape() -> None:
    assert diskspace.is_out_of_space(OSError(errno.ENOSPC, "No space left on device"))
    assert diskspace.is_out_of_space(shutil.Error([("a", "b", "[Errno 28] No space left on device: 'b'")]))
    assert diskspace.is_out_of_space("repo write failed: write error str: No space left on device")
    assert not diskspace.is_out_of_space(OSError(errno.EACCES, "Permission denied"))
    assert not diskspace.is_out_of_space("connection refused")


def test_a_short_filesystem_is_named_before_a_phase(ctx, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(total=10, used=10, free=100 * 1024 * 1024))
    shortfalls = diskspace.check(ctx)
    assert shortfalls and shortfalls[0].free_mb == 100 and shortfalls[0].needed_mb == 1024
    from initialize_claude_ai.cli import _disk_preflight
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    stopped = _disk_preflight(ctx, McpPhase)
    assert stopped is not None and stopped.fatal and stopped.steps[0].name == "free space"


def test_enough_room_starts_the_phase(ctx, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(total=10, used=1, free=50 * 1024**3))
    assert diskspace.check(ctx) == []


def test_a_phase_that_hits_a_full_disk_stops_with_one_line_not_a_traceback(ctx, caplog) -> None:
    from initialize_claude_ai.phases.base import Phase

    class Writes(Phase):
        slug, title, number = "writes", "Writes", 9

        def execute(self, result: PhaseResult) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    result = Writes(ctx).run()
    [step] = [s for s in result.steps if s.name == "free space"]
    assert step.status == Status.FAILED and result.fatal
    assert ctx.state["out_of_space"]
    assert "Traceback" not in caplog.text


def test_a_command_that_fails_for_want_of_space_is_noticed() -> None:
    from initialize_claude_ai.core.proc import Runner

    runner = Runner(dry_run=False)
    runner.run([sys.executable, "-c", "import sys; sys.stderr.write('No space left on device'); sys.exit(1)"])
    assert "No space left on device" in runner.out_of_space


def test_a_reinstall_that_failed_is_partial_even_with_the_old_copy_there(ctx, monkeypatch) -> None:
    from initialize_claude_ai.install.verify import CheckResult
    from initialize_claude_ai.phases import phase1_install
    from initialize_claude_ai.phases.phase1_install import InstallPhase

    ctx.dry_run = False
    ctx.settings.set("install.force", True)
    phase = InstallPhase(ctx)
    ok = [CheckResult(kind="v", ok=True, detail="/usr/bin/node (26.7.0)")]
    monkeypatch.setattr(phase, "_verify", lambda component, verifier: ok)
    monkeypatch.setattr(phase, "_satisfied", lambda checks: True)
    monkeypatch.setattr(
        phase1_install,
        "run_strategies",
        lambda *a, **k: SimpleNamespace(attempts=["script: rc=1"], remediation=["Run it yourself"], installed=False),
    )
    ctx.runner.out_of_space = "install script (node): No space left on device"
    component = SimpleNamespace(verify=["x"], install=["x"], key="node", title="Node.js")
    status, detail, _ = phase._process(component, None, None, PhaseResult(phase="install"))
    assert status == Status.PARTIAL and "reinstall failed" in detail and "kept the existing" in detail


def test_an_agent_workspace_that_cannot_be_prepared_turns_agents_off_once(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    pipeline = AgenticPipeline(ctx)
    calls: list[int] = []

    def full(*_a, **_k):
        calls.append(1)
        raise shutil.Error([("s", "d", "[Errno 28] No space left on device: 'd'")])

    monkeypatch.setattr(pipeline, "prepare_workspace", full)
    assert "no space left" in pipeline._workspace_problem()
    assert pipeline._workspace_problem() and len(calls) == 1, "decided once for the run"
    assert ctx.state["out_of_space"]


def test_a_log_file_that_cannot_be_written_is_said_on_the_terminal(tmp_path, capsys) -> None:
    from initialize_claude_ai.core.logging_setup import Transcript

    class Full:
        closed = False

        def write(self, _text):
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self):
            pass

    transcript = Transcript(path=tmp_path / "run.log", stream=Full())
    transcript.write("first line")
    transcript.write("second line")
    err = capsys.readouterr().err
    assert err.count("can no longer be written") == 1
    assert transcript.unwritten == ["first line", "second line"]


def test_an_unwritten_mcp_json_is_reported_with_its_reason(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    writer = McpWriter(ctx)

    def full(path, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(writer, "_atomic_write", full)
    candidate = Candidate(kind="url", name="x", url="https://example.test/mcp", transport=Transport.STREAMABLE_HTTP)
    writer._write_json(
        [
            SimpleNamespace(
                name="x",
                entry={"type": "http", "url": "https://example.test/mcp"},
                had_previous=False,
                previous_entry=None,
                remediation=[],
                written=False,
                simulated=False,
                note=lambda *_: None,
                candidate=candidate,
            )
        ]
    )
    assert writer.write_failures and ctx.state["out_of_space"]
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_file_not_written(result, writer)
    [step] = result.steps
    assert step.status == Status.FAILED and "No space left" in step.detail


def test_cleanup_removes_only_what_nothing_uses(ctx, tmp_path, monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.chdir(tmp_path)
    ctx.dry_run = False
    old = time.time() - 7200
    stale = tmp_path / "mcp-client-old"
    young = tmp_path / "mcp-client-young"
    used = tmp_path / "ica-used"
    foreign = tmp_path / "somebody-elses"
    for d in (stale, young, used, foreign):
        d.mkdir()
        (d / "f").write_text("x")
        diskspace.mark_owned(d)
    for d in (stale, used, foreign):
        os.utime(d, (old, old))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {"command": f"{used}/server"}}}))
    done = diskspace.cleanup(ctx)
    assert not stale.exists(), "an old leftover of this program is removed"
    assert young.exists(), "a running server's directory is kept"
    assert used.exists() and str(used) in done.kept, "a path an entry names is never removed"
    assert foreign.exists(), "nothing outside this program's prefixes is touched"


def test_cleanup_never_runs_in_a_dry_run(ctx, tmp_path, monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "mcp-client-old"
    stale.mkdir()
    diskspace.mark_owned(stale)
    os.utime(stale, (time.time() - 7200,) * 2)
    ctx.dry_run = True
    assert diskspace.cleanup(ctx).removed == [] and stale.exists()


@pytest.mark.parametrize(
    "flags,expected", [(["--no-cleanup"], "run.cleanup.enabled=false"), (["--do-cleanup"], "run.cleanup.enabled=true")]
)
def test_the_cleanup_flags_reach_their_setting(flags, expected) -> None:
    from initialize_claude_ai.cli import _overrides_from_args, build_parser

    args = build_parser().parse_args(["run", *flags])
    assert expected in _overrides_from_args(args)


def test_cleanup_is_on_by_default(settings) -> None:
    assert settings.bool_("run.cleanup.enabled") is True


def test_the_client_bridge_removes_its_temporary_directory_when_stopped(tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.client_bridge import _SOURCE

    script = tmp_path / "bridge.py"
    script.write_text(_SOURCE)
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "server": "s",
                "client": "osc",
                "read": ["osc"],
                "config": "",
                "config_variable": "",
                "isolate": [],
                "parts": {},
                "extra_lines": [],
                "file_parts": [],
                "optional_parts": [],
                "key_part": "",
                "key_files": [],
                "timeout": 5,
                "tools": [],
            }
        )
    )
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "MCP_CLIENT_MANIFEST": str(manifest), "TMPDIR": str(scratch)}
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    deadline = time.time() + 10
    while not list(scratch.glob("mcp-client-*")) and time.time() < deadline:
        time.sleep(0.05)
    assert list(scratch.glob("mcp-client-*")), "the bridge made its directory"
    deadline = time.time() + 10
    while not list(scratch.glob(f"mcp-client-*/{diskspace.OWNER_MARKER}")) and time.time() < deadline:
        time.sleep(0.05)
    assert list(scratch.glob(f"mcp-client-*/{diskspace.OWNER_MARKER}")), "marked as this program's, for cleanup"
    proc.terminate()
    proc.wait(timeout=10)
    assert not list(scratch.glob("mcp-client-*")), "and removed it when the client stopped it"


def test_packaged_settings_equal_the_code(settings) -> None:
    assert tuple(settings.str_list("run.cleanup.temp_prefixes")) == diskspace.TEMP_PREFIXES
    assert tuple(settings.str_list("run.cleanup.work_subtrees")) == diskspace.WORK_SUBTREES
    assert tuple(settings.str_list("run.disk.out_of_space_markers")) == diskspace.OUT_OF_SPACE_MARKERS
    assert settings.bool_("run.cleanup.require_owner_marker") is True


@pytest.mark.parametrize("require", [True, False])
def test_a_directory_only_named_like_ours_is_never_removed(ctx, tmp_path, monkeypatch, require) -> None:
    """Cleanup matched ``/tmp/ica-*`` by name and removed somebody's ``/tmp/ica-baseline``.

    The prefix says where to look; only the marker this program writes says a
    directory is its own. ``baseline`` is even eight lowercase letters -- the
    shape of a tempfile suffix -- so no name pattern could have told them apart.
    """
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.chdir(tmp_path)
    ctx.dry_run = False
    ctx.settings.set("run.cleanup.require_owner_marker", require)
    old = time.time() - 7200
    theirs = tmp_path / "ica-baseline"
    ours = tmp_path / "ica-validate-k2j4h1x9"
    for d in (theirs, ours):
        d.mkdir()
        (d / "f").write_text("x")
    diskspace.mark_owned(ours)
    for d in (theirs, ours):
        os.utime(d, (old, old))
    done = diskspace.cleanup(ctx)
    assert not ours.exists(), "a leftover this program marked is removed"
    if require:
        assert theirs.exists() and str(theirs) in done.unproven, "an unmarked directory is somebody else's"
    else:
        assert not theirs.exists(), "the setting decides; off, the prefix alone is trusted again"


def test_every_temporary_directory_the_program_makes_is_marked() -> None:
    """A temporary directory under a cleanup prefix is made through :func:`diskspace.temporary_directory`.

    Made any other way it carries no marker: cleanup would then never give it
    back -- or, before the marker, would take anybody's directory named alike.
    """
    import re
    from pathlib import Path

    import initialize_claude_ai

    root = Path(initialize_claude_ai.__file__).parent
    pattern = re.compile(r"(?:TemporaryDirectory|mkdtemp)\((?:[^)]*?)prefix=[\"']([^\"']+)")
    unmarked = []
    for source in root.rglob("*.py"):
        if source.name == "diskspace.py":
            continue
        text = source.read_text(encoding="utf-8")
        for found in pattern.finditer(text):
            if not found.group(1).startswith(diskspace.TEMP_PREFIXES):
                continue
            after = text[found.end() : found.end() + 400]
            if diskspace.OWNER_MARKER not in after:
                unmarked.append(f"{source.relative_to(root)}: {found.group(0)}")
    assert not unmarked, unmarked
    with diskspace.temporary_directory("ica-test-") as made:
        assert diskspace.owned(Path(made))


def test_a_path_another_projects_entry_uses_is_kept(ctx, tmp_path, monkeypatch) -> None:
    """Run from one directory, cleanup must still see every project Claude Code knows."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    here = tmp_path / "here"
    other = tmp_path / "other-project"
    here.mkdir()
    other.mkdir()
    monkeypatch.chdir(here)
    home = tmp_path / "claude-config"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    (home / ".claude.json").write_text(json.dumps({"projects": {str(other): {}}}))
    work = tmp_path / "work"
    built = work / "agent-workspace" / "artifacts" / "build-x"
    built.mkdir(parents=True)
    (built / "server").write_bytes(b"x" * (2 * 1024 * 1024))
    (other / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": f"{built}/server"}}}))
    monkeypatch.setattr(type(ctx), "work_dir", property(lambda self: work))
    ctx.dry_run = False
    ctx.settings.set("run.cleanup.work_budget_mb", 0)
    done = diskspace.cleanup(ctx)
    assert built.exists() and str(built) in done.kept


@pytest.mark.parametrize("variable_set", [False, True])
def test_a_path_named_through_a_variable_default_is_kept(ctx, tmp_path, monkeypatch, variable_set) -> None:
    """``${ICA_MCP_ARTIFACTS:-/…/artifacts}/gitlab-example-net/…`` names the bridge, not the directory above it.

    Matching the raw text protected the artifacts directory itself and not the
    build under it, so cleanup would have removed a working server's files.
    The entry is read as Claude Code expands it -- with this shell's value, and
    with the default alone.
    """
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-config"))
    work = tmp_path / "work"
    artifacts = work / "agent-workspace" / "artifacts"
    built = artifacts / "gitlab-example-net"
    (built / "bridge").mkdir(parents=True)
    (built / "bridge" / "mcp_bridge.py").write_bytes(b"x" * (2 * 1024 * 1024))
    unused = artifacts / "nobody-uses-this"
    unused.mkdir()
    (unused / "f").write_bytes(b"x" * (2 * 1024 * 1024))
    if variable_set:
        monkeypatch.setenv("ICA_TEST_ARTIFACTS", str(artifacts))
        default = "/nowhere"
    else:
        monkeypatch.delenv("ICA_TEST_ARTIFACTS", raising=False)
        default = str(artifacts)
    entry = {
        "command": "python3",
        "args": [f"${{ICA_TEST_ARTIFACTS:-{default}}}/gitlab-example-net/bridge/mcp_bridge.py"],
    }
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"gitlab-example-net": entry}}))
    monkeypatch.setattr(type(ctx), "work_dir", property(lambda self: work))
    ctx.dry_run = False
    ctx.settings.set("run.cleanup.work_budget_mb", 0)
    done = diskspace.cleanup(ctx, apply=False)
    assert str(built) in done.kept and str(built) not in done.removed
    assert str(unused) in done.removed, "what no entry launches is still given back"
