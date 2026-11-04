"""Phase 1 installs for what Claude Code will run, and checks that what is installed works.

From the copied ``.mcp.json`` and the agent-mode runs of 2026-10-23:

* bugzilla-opensuse-org and confluence-example-com failed with ``ENOENT:
  posix_spawn '${ICA_MCP_UVX}'`` -- the default ``/root/.local/bin/uvx`` was the
  other machine's, and ``uvx`` sat in ``/usr/bin``;
* github-com's ``github-mcp-server`` and a client bridge's ``osc`` were never
  installed: phase 1 read only its own launcher templates;
* claude-agent-sdk 0.2.152 ran its own Claude Code 2.1.259, which "does not
  support this model", while the installed 2.1.309 served it -- phase 1 said OK
  because the module imported;
* a container candidate was rejected as "not accepted" with Docker's daemon
  stopped -- phase 1 checked only that ``docker`` exists.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from initialize_claude_ai.core.report import PhaseResult, Safety, Status
from initialize_claude_ai.install import verify as verify_mod
from initialize_claude_ai.install.verify import PRESENT, USABLE, Verifier, checks_for, remedy_for, usable
from initialize_claude_ai.mcp_auto_discovery import entry_needs, existing, runtime
from initialize_claude_ai.phases import phase1_install
from initialize_claude_ai.phases.phase1_install import InstallPhase

FAILS_LIKE_DOCKER = {
    "kind": "command",
    "purpose": "usable",
    "argv": [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('Cannot connect to the Docker daemon at unix:///x'); sys.exit(1)",
    ],
    "remedy": "make it reachable",
    "remedies": {"Cannot connect to the Docker daemon": "start the Docker service"},
}


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _project(entries: dict) -> None:
    Path(".mcp.json").write_text(json.dumps({"mcpServers": entries}))


# -- present vs usable ---------------------------------------------------------------------


def test_only_a_check_that_says_usable_is_one() -> None:
    specs = [{"kind": "binary", "names": ["docker"]}, FAILS_LIKE_DOCKER, "opaque"]
    assert checks_for(specs, USABLE) == [FAILS_LIKE_DOCKER]
    assert checks_for(specs, PRESENT) == [specs[0], "opaque"], "everything else stays a presence check"


def test_the_remedy_is_the_one_its_words_name() -> None:
    assert remedy_for(FAILS_LIKE_DOCKER, "error: Cannot connect to the Docker daemon") == "start the Docker service"
    assert remedy_for(FAILS_LIKE_DOCKER, "something else") == "make it reachable"


def test_a_failed_usable_check_carries_its_fix(ctx) -> None:
    result = Verifier(ctx).check(FAILS_LIKE_DOCKER)
    assert not result.ok and result.data["remedy"] == "start the Docker service"


def test_usable_is_asked_once_per_run(ctx) -> None:
    component = SimpleNamespace(key="engine", verify=[{"kind": "binary", "names": ["x"]}, FAILS_LIKE_DOCKER])
    works, words = usable(ctx, component)
    assert not works and "start the Docker service" in words
    ctx.state["install_usable"]["engine"] = (True, "")
    assert usable(ctx, component) == (True, ""), "the run's answer is shared, not asked again"
    assert usable(ctx, SimpleNamespace(key="plain", verify=[{"kind": "binary"}])) == (True, "")


def _phase_with(ctx, monkeypatch, components, process):
    phase = InstallPhase(ctx)
    monkeypatch.setattr(phase1_install, "load_components", lambda settings: components)
    monkeypatch.setattr(phase1_install, "select_components", lambda catalog, **k: (list(components), []))
    for name in ("_prefetch", "_entry_demand"):
        monkeypatch.setattr(phase, name, lambda *a, **k: {})
    for name in ("_prepare_python_target", "_seed_tokens", "_publish", "_record_environment", "_finish_entries"):
        monkeypatch.setattr(phase, name, lambda *a, **k: None)
    monkeypatch.setattr(phase, "_process", process)
    return phase


def test_installed_but_not_usable_is_partial_with_its_fix_and_never_reinstalled(ctx, monkeypatch) -> None:
    engine = SimpleNamespace(
        key="engine",
        title="engine",
        group="mcp-runtimes",
        required=False,
        verify=[{"kind": "binary", "names": ["sh"]}, FAILS_LIKE_DOCKER],
        install=[{"kind": "system"}],
    )
    installs: list[str] = []
    phase = _phase_with(
        ctx,
        monkeypatch,
        [engine],
        lambda component, *_a: (installs.append("verify only") and None) or (Status.OK, "/usr/bin/engine", []),
    )
    result = PhaseResult(phase="install")
    phase.execute(result)
    (step,) = [s for s in result.steps if s.name == "engine"]
    assert step.status == Status.PARTIAL and "not usable here" in step.detail
    assert "start the Docker service" in " ".join(step.remediation)
    assert installs == ["verify only"], "_process ran once; a usable failure is not a reason to install"
    assert phase.safety(result)[0] == Safety.CAUTION


def test_the_presence_verdict_ignores_usable_checks(ctx, monkeypatch) -> None:
    phase = InstallPhase(ctx)
    asked: list[dict] = []

    class Recorder:
        def check(self, spec):
            asked.append(spec)
            return verify_mod.CheckResult(kind="binary", ok=True, detail="/bin/sh", path="/bin/sh")

    component = SimpleNamespace(key="engine", verify=[{"kind": "binary", "names": ["sh"]}, FAILS_LIKE_DOCKER])
    assert all(c.ok for c in phase._verify(component, Recorder()))
    assert FAILS_LIKE_DOCKER not in asked


def test_a_component_needed_with_prefetch_false_is_checked_never_installed(ctx) -> None:
    phase = InstallPhase(ctx)
    phase._check_only = {"docker": "gitlab-x (project_file) starts with docker"}
    component = SimpleNamespace(
        key="docker",
        title="docker",
        verify=[{"kind": "binary", "names": ["no-such-docker-x"]}],
        install=[{"kind": "system"}],
    )
    status, detail, _ = phase._process(component, Verifier(ctx), None, PhaseResult(phase="install"))
    assert status == Status.SKIPPED and "prefetch = false" in detail and "--install-only docker" in detail


# -- what registered entries run -------------------------------------------------------------


def test_entries_are_read_from_the_project_and_claude_code_s_own_configuration(ctx, monkeypatch, tmp_path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"user-one": {"command": "u"}},
                "projects": {os.getcwd(): {"mcpServers": {"local-one": {"command": "l"}, "both": {"command": "cli"}}}},
            }
        )
    )
    _project({"both": {"command": "file"}, "file-one": {"command": "f"}})
    found = existing.loaded_entries(ctx)
    assert found["both"][0]["command"] == "file", "the project file says more than Claude Code's copy"
    assert {found[n][1] for n in ("user-one", "local-one", "file-one")} == {"user", "local", "project_file"}


def test_a_program_an_entry_starts_and_nothing_has_is_named_with_the_run_that_repairs_it(ctx) -> None:
    _project(
        {
            "code-example-test": {
                "command": "${ICA_MCP_NO_SUCH_SERVER_X:-/nowhere/no-such-server-x}",
                "args": ["stdio"],
                "env": {"API_URL": "https://code.example.test/api"},
            }
        }
    )
    (need,) = entry_needs.missing_programs(ctx)
    assert (need.program, need.role, need.server) == ("no-such-server-x", entry_needs.LAUNCH, "code-example-test")
    assert entry_needs.recheck_command(need.address).endswith("--mcp-recheck https://code.example.test")


def test_a_program_on_this_machine_under_another_path_is_not_missing_but_given_its_path(
    ctx, monkeypatch, tmp_path
) -> None:
    """The ENOENT '${ICA_MCP_UVX}' case: the default is the other machine's, the program is here."""
    here = _executable(tmp_path / "bin", "fake-launcher-x")
    ctx.runner.extra_paths.insert(0, str(here.parent))
    monkeypatch.setattr(entry_needs, "_fresh", lambda ctx: {})
    _project(
        {
            "a": {"command": "${ICA_MCP_FAKE_LAUNCHER_X:-/other-machine/bin/fake-launcher-x}", "args": ["pkg"]},
            "b": {"command": "${ICA_MCP_FAKE_LAUNCHER_X:-/other-machine/bin/fake-launcher-x}"},
        }
    )
    assert entry_needs.missing_programs(ctx) == []
    found = entry_needs.machine_paths(ctx, apply=False)
    assert found.set == {"ICA_MCP_FAKE_LAUNCHER_X": str(here)}
    assert sorted(found.users["ICA_MCP_FAKE_LAUNCHER_X"]) == ["a", "b"]


def test_a_path_is_written_where_a_new_terminal_reads_it_and_a_set_one_is_never_changed(
    ctx, monkeypatch, tmp_path
) -> None:
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    here = _executable(tmp_path / "bin", "fake-launcher-y")
    ctx.runner.extra_paths.insert(0, str(here.parent))
    ctx.dry_run = False
    ctx.settings.set("install.machine_paths.check_with_claude", False)
    monkeypatch.setattr(entry_needs, "_fresh", lambda ctx: {"ICA_MCP_ALREADY_SET": "/operator/choice"})
    monkeypatch.delenv("ICA_MCP_FAKE_LAUNCHER_Y", raising=False)
    _project(
        {
            "a": {
                "command": "${ICA_MCP_FAKE_LAUNCHER_Y:-/other/fake-launcher-y}",
                "args": ["${ICA_MCP_ALREADY_SET:-/other/x}"],
            }
        }
    )
    found = entry_needs.machine_paths(ctx, apply=True)
    assert found.kept == ["ICA_MCP_ALREADY_SET"] and "ICA_MCP_ALREADY_SET" not in found.set
    env = SettingsWriter(ctx, settings_path(ctx)).read().get("env") or {}
    assert env.get("ICA_MCP_FAKE_LAUNCHER_Y") == str(here)
    assert "ICA_MCP_ALREADY_SET" not in env
    monkeypatch.delenv("ICA_MCP_FAKE_LAUNCHER_Y", raising=False)


def test_a_file_this_machine_does_not_have_is_left_to_phase_4(ctx, monkeypatch) -> None:
    monkeypatch.setattr(entry_needs, "_fresh", lambda ctx: {})
    root = ctx.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
    _project(
        {
            "bridge-x": {
                "command": "/bin/sh",
                "args": [f"${{{root}_NO_SUCH_ROOT_X:-/other/state/servers}}/bridge-x/bridge.py"],
            }
        }
    )
    good = Path("servers") / "fine-x"
    good.mkdir(parents=True)
    (good / "bridge.py").write_text("")
    _project(
        {
            "bridge-x": {
                "command": "/bin/sh",
                "args": [f"${{{root}_NO_SUCH_ROOT_X:-/other/state/servers}}/bridge-x/bridge.py"],
            },
            "fine-x": {
                "command": "/bin/sh",
                "args": [f"${{{root}_NO_SUCH_ROOT_X:-{Path.cwd() / 'servers'}}}/fine-x/bridge.py"],
            },
        }
    )
    found = entry_needs.machine_paths(ctx, apply=False)
    assert not found.set and f"{root}_NO_SUCH_ROOT_X" in found.unresolved
    assert found.missing[f"{root}_NO_SUCH_ROOT_X"] == {"bridge-x": "/other/state/servers/bridge-x/bridge.py"}, (
        "only the entry whose file is missing, with its own path"
    )


def test_a_program_a_client_bridge_calls_is_installed_by_the_client_s_own_steps(ctx, monkeypatch, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.client_bridge import ENV_MANIFEST

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ctx.runner.extra_paths.insert(0, str(bin_dir))
    ctx.dry_run = False
    ctx.runner.dry_run = False
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"client": "fakeclient", "read": ["fake-client-x", "api"]}))
    make = f"printf '#!/bin/sh\\n' > {bin_dir}/fake-client-x && chmod +x {bin_dir}/fake-client-x"
    ctx.settings.set("mcp.discovery.clients", {"fakeclient": {"install": [["sh", "-c", make]]}})
    ctx.settings.set("install.machine_paths.enabled", False)
    _project({"build-x": {"command": "/bin/sh", "args": ["bridge.py"], "env": {ENV_MANIFEST: str(manifest)}}})
    (need,) = entry_needs.missing_programs(ctx)
    assert need.role == entry_needs.CALL and need.ways
    result = PhaseResult(phase="install")
    InstallPhase(ctx)._finish_entries(result, only=[])
    assert (bin_dir / "fake-client-x").exists()
    assert [s.status for s in result.steps] == [Status.CHANGED]
    assert entry_needs.missing_programs(ctx) == []


def test_needs_become_catalog_components_and_prefetch_false_only_checks(ctx, monkeypatch) -> None:
    from initialize_claude_ai.install.components import load_components

    catalog = load_components(ctx.settings)
    monkeypatch.setattr(
        entry_needs,
        "missing_programs",
        lambda ctx: [
            entry_needs.Need("go-server", "project_file", "go", entry_needs.LAUNCH),
            entry_needs.Need("box-server", "project_file", "docker", entry_needs.LAUNCH),
            entry_needs.Need("py-server", "project_file", "python3.13", entry_needs.LAUNCH),
        ],
    )
    phase = InstallPhase(ctx)
    selected: list = []
    added = phase._entry_demand(catalog, selected, only=[], skip=[], notes=[])
    keys = {c.key for c in selected}
    assert {"docker", "python"} <= keys and any("go" in k for k in keys)
    assert "go-server" in " ".join(added.values())
    assert set(phase._check_only) == {"docker"}, "prefetch = false: checked, never installed"
    assert phase._entry_demand(catalog, [], only=["git"], skip=[], notes=[]) == {}, "--install-only names it all"


# -- which Claude Code the SDK runs ----------------------------------------------------------


@pytest.fixture
def two_clis(ctx, monkeypatch):
    versions = {"/sdk/_bundled/claude": "2.1.259 (Claude Code)", "/usr/local/bin/claude": "2.1.309 (Claude Code)"}

    def probe(argv, timeout=None, **_k):
        if argv[-1] == "--version":
            text = versions.get(argv[0], "")
            return SimpleNamespace(ok=bool(text), output=text, stdout=text)
        return SimpleNamespace(ok=True, output="/sdk/_bundled/claude\n", stdout="/sdk/_bundled/claude\n")

    monkeypatch.setattr(ctx.runner, "probe", probe)
    monkeypatch.setattr(type(ctx), "tool", lambda self, name, config_key="": "/usr/local/bin/claude")
    return versions


@pytest.mark.parametrize(
    ("mode", "runs"),
    [("auto", "/usr/local/bin/claude"), ("installed", "/usr/local/bin/claude"), ("bundled", "/sdk/_bundled/claude")],
)
def test_the_sdk_runs_the_newest_claude_code_unless_told_otherwise(ctx, two_clis, mode, runs) -> None:
    ctx.settings.set("mcp.agent.sdk_cli", mode)
    choice = runtime.sdk_cli(ctx, python="/venv/bin/python")
    assert choice.runs == runs
    assert bool(choice.behind) is (mode == "bundled"), "only the bundled 2.1.259 is behind 2.1.309"


def test_a_newer_bundled_copy_is_kept_and_unknown_versions_change_nothing(ctx, two_clis) -> None:
    two_clis["/sdk/_bundled/claude"] = "2.2.0 (Claude Code)"
    assert runtime.sdk_cli(ctx, python="/a").path == "", "auto keeps the SDK's own when it is newer"
    two_clis["/sdk/_bundled/claude"] = ""
    ctx.state.pop("mcp_sdk_cli")
    assert runtime.sdk_cli(ctx, python="/a").path == "", "a version that cannot be read decides nothing"


def test_phase_1_reports_a_claude_code_the_sdk_runs_that_is_behind(ctx, two_clis) -> None:
    ctx.settings.set("mcp.agent.sdk_cli", "bundled")
    result = Verifier(ctx).check({"kind": "agent_cli", "purpose": "usable", "remedy": "set mcp.agent.sdk_cli = auto"})
    assert not result.ok and "2.1.259" in result.detail and "2.1.309" in result.detail
    assert result.data["remedy"] == "set mcp.agent.sdk_cli = auto"


def test_the_sdk_runtime_passes_the_chosen_claude_code(ctx, monkeypatch, two_clis, tmp_path) -> None:
    captured: dict = {}

    class Options:
        def __init__(
            self,
            cwd=None,
            allowed_tools=None,
            permission_mode=None,
            setting_sources=None,
            agents=None,
            model=None,
            max_turns=None,
            stderr=None,
            cli_path=None,
            **_k,
        ):
            captured["cli_path"] = cli_path

    async def query(prompt, options):
        if False:
            yield None

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = Options
    fake.AgentDefinition = lambda **k: k
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    ctx.state["mcp_sdk_cli"] = {"": runtime.sdk_cli(ctx, python="/venv/bin/python")}
    sdk = runtime.SdkRuntime(ctx)
    spec = runtime.AgentDefinitionSpec(name="scout", description="d", prompt="p")
    sdk.run("hello", agent=spec, workspace=tmp_path)
    assert captured["cli_path"] == "/usr/local/bin/claude"


# -- phase 4 does not launch with what cannot work --------------------------------------------


def test_phase_4_does_not_launch_a_server_whose_engine_is_not_usable(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.provisioning import Provisioner
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    engine = SimpleNamespace(key="engine", verify=[{"kind": "binary", "names": ["sh"]}, FAILS_LIKE_DOCKER])
    monkeypatch.setattr(Provisioner, "provider", lambda self, command: engine if command == "sh" else None)
    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="boxed", command=["sh"], args=["-c", "exit 0"])
    shake = CandidateVerifier(ctx)._trial_stdio(candidate, {})
    assert shake.not_launched and "not usable here" in shake.error and "start the Docker service" in shake.error
    ctx.settings.set("mcp.packages.check_launcher_usable", False)
    assert CandidateVerifier(ctx)._unusable_launcher("sh") == ""


# -- settings -----------------------------------------------------------------------------


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("install.demand_from_entries") is True
    assert settings.bool_("install.machine_paths.enabled") is True
    assert settings.str_("install.machine_paths.persist_to") == "settings"
    assert settings.bool_("install.machine_paths.check_with_claude") is True
    assert settings.str_("mcp.agent.sdk_cli") == "auto"
    assert settings.bool_("mcp.packages.check_launcher_usable") is True


def test_the_catalog_checks_docker_and_the_sdk_for_use(settings) -> None:
    from initialize_claude_ai.install.components import load_components

    catalog = {c.key: c for c in load_components(settings)}
    assert [s["argv"] for s in checks_for(catalog["docker"].verify, USABLE)] == [["docker", "info"]]
    assert [s["kind"] for s in checks_for(catalog["claude-agent-sdk-python"].verify, USABLE)] == ["agent_cli"]


def test_the_path_this_program_recorded_here_wins_over_another_copy_on_path(ctx, monkeypatch, tmp_path) -> None:
    """Two uvx on one machine: the entries were proved with the one this program recorded writing."""
    from initialize_claude_ai.mcp_auto_discovery.writer import record_path_variable

    on_path = _executable(tmp_path / "first", "fake-launcher-z")
    recorded = _executable(tmp_path / "recorded", "fake-launcher-z")
    ctx.runner.extra_paths.insert(0, str(on_path.parent))
    monkeypatch.setattr(entry_needs, "_fresh", lambda ctx: {})
    ctx.dry_run = False
    record_path_variable(ctx, "ICA_MCP_FAKE_LAUNCHER_Z", str(recorded))
    ctx.dry_run = True
    _project({"a": {"command": "${ICA_MCP_FAKE_LAUNCHER_Z:-/other/fake-launcher-z}"}})
    assert entry_needs.machine_paths(ctx, apply=False).set == {"ICA_MCP_FAKE_LAUNCHER_Z": str(recorded)}
    recorded.unlink()
    assert entry_needs.machine_paths(ctx, apply=False).set == {"ICA_MCP_FAKE_LAUNCHER_Z": str(on_path)}, (
        "a recorded path that is gone gives way to the one on PATH"
    )
