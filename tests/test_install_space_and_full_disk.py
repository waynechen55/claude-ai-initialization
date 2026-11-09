"""Phase 1 filled a disk with an in-place reinstall, then blamed every later failure on it.

* ``gcloud components reinstall`` stages a whole copy of the SDK beside the one
  in use; with just over 1 GB free it filled the disk. What a step needs is now
  asked before it runs, and a reinstall that does not fit is kept, not run.
* What the failed step left next to the folder it owns is removed.
* browser, xdg-utils and clipboard each said "install command (gcloud) … No
  space left": a full disk is blamed only on the step whose command hit it, and
  it stops the phase at once.
* python313-base was force-reinstalled three times; once per run now.
* "kept the existing verified" named nothing; the component's title does.
* "export BUILD_EXAMPLE_NET_USERNAME" was asked although settings env held it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.install import installers, ownership, space
from initialize_claude_ai.install.installers import Installer
from initialize_claude_ai.install.verify import CheckResult
from initialize_claude_ai.phases import phase1_install
from initialize_claude_ai.phases.phase1_install import InstallPhase


def _sdk(tmp_path, mb: int = 2):
    sdk = tmp_path / "google-cloud-sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "bin" / "gcloud").write_text("x")
    (sdk / "blob").write_bytes(b"\0" * (mb * 1024 * 1024))
    return sdk


def _gcloud(sdk, **extra):
    script = {
        "kind": "script",
        "url": "https://sdk.example.test",
        "adds_bin_dir": str(sdk / "bin"),
        "reinstall_argv": ["{binary}", "components", "reinstall", "--quiet"],
        "space_factor": 2,
        **extra,
    }
    return SimpleNamespace(
        key="gcloud",
        title="Google Cloud CLI",
        verify=[{"kind": "binary", "names": ["gcloud"]}],
        install=[script],
        provides=[],
        capabilities=[],
    )


def _phase(ctx, monkeypatch, checks, tried=None):
    ctx.dry_run = False
    ctx.settings.set("install.force", True)
    phase = InstallPhase(ctx)
    monkeypatch.setattr(phase, "_verify", lambda component, verifier: checks)
    if tried is not None:
        monkeypatch.setattr(phase1_install, "run_strategies", tried)
    return phase


# -- what a step needs ---------------------------------------------------------------------------


def test_an_in_place_reinstall_needs_its_folder_times_its_factor(ctx, tmp_path) -> None:
    sdk = _sdk(tmp_path, mb=3)
    made = Installer(ctx)
    component = _gcloud(sdk)
    owner = ownership.find(made, component, [CheckResult(kind="binary", ok=True, path=str(sdk / "bin" / "gcloud"))])
    assert owner.folder == str(sdk), "the installation, not its bin folder"
    strategies, _ = ownership.plan(made, component, owner)
    need, how = space.need_mb(made, owner, strategies)
    assert need == 3 * 2 + space.margin_mb(ctx.settings) and "x2" in how


def test_a_package_s_need_is_its_installed_size(ctx, monkeypatch) -> None:
    made = Installer(ctx)
    made.platform = SimpleNamespace(package_manager="zypper")
    monkeypatch.setattr(made, "_query", lambda manager, name, fills: [str(300 * 1024 * 1024)])
    owner = ownership.Owner(kind="system", packages=["MozillaFirefox"])
    need, how = space.need_mb(made, owner, [{"kind": "system"}])
    assert need == 300 + space.margin_mb(ctx.settings) and "MozillaFirefox" in how


def test_a_reinstall_that_does_not_fit_is_kept_not_run(ctx, monkeypatch, tmp_path) -> None:
    sdk = _sdk(tmp_path)
    ran: list[int] = []
    checks = [CheckResult(kind="binary", ok=True, path=str(sdk / "bin" / "gcloud"), detail="gcloud (590.0.0)")]
    phase = _phase(ctx, monkeypatch, checks, lambda *a, **k: ran.append(1))
    monkeypatch.setattr(space, "free_mb", lambda path: 10)
    status, detail, _ = phase._process(_gcloud(sdk), None, Installer(ctx), PhaseResult(phase="install"))
    assert status == Status.OK and "kept -- reinstalling it in place needs" in detail and "10 MB free" in detail
    assert not ran


def test_a_fresh_install_that_cannot_fit_is_not_attempted(ctx, monkeypatch) -> None:
    checks = [CheckResult(kind="binary", ok=False, detail="node not found")]
    ran: list[int] = []
    phase = _phase(ctx, monkeypatch, checks, lambda *a, **k: ran.append(1))
    ctx.settings.set("install.force", False)
    monkeypatch.setattr(space, "free_mb", lambda path: 5)
    component = SimpleNamespace(key="node", title="Node.js", verify=["x"], install=[{"kind": "system"}])
    status, detail, _ = phase._process(component, None, Installer(ctx), PhaseResult(phase="install"))
    assert status == Status.FAILED and detail.startswith("not attempted:") and not ran
    assert phase._disk_full


# -- what a failed step left --------------------------------------------------------------------


def test_what_another_tool_left_beside_its_folder_is_never_deleted_by_its_name(ctx, monkeypatch, tmp_path) -> None:
    sdk = _sdk(tmp_path)
    (tmp_path / "google-cloud-sdk.backup-of-yours").mkdir()

    def failing(*_a, **_k):
        (tmp_path / "google-cloud-sdk.staging").mkdir()
        (tmp_path / "google-cloud-sdk.staging" / "python3").write_text("partial")
        return installers.StrategyRun(
            installed=False,
            attempts=["command: rc=1"],
            reasons=["command: rc=1: OSError: [Errno 28] No space left on device"],
        )

    checks = [CheckResult(kind="binary", ok=True, path=str(sdk / "bin" / "gcloud"), detail="gcloud (590.0.0)")]
    phase = _phase(ctx, monkeypatch, checks, failing)
    status, detail, _ = phase._process(_gcloud(sdk), None, Installer(ctx), PhaseResult(phase="install"))
    assert status == Status.PARTIAL and "Errno 28" in detail
    # A name proves nothing: gcloud's own staging copy is not this program's to delete.
    assert (tmp_path / "google-cloud-sdk.staging").exists()
    assert (tmp_path / "google-cloud-sdk.backup-of-yours").exists(), "what was there before is never touched"


# -- a full disk: blamed per step, and a stop ---------------------------------------------------------


def test_an_earlier_full_disk_is_not_this_step_s_reason(ctx, monkeypatch) -> None:
    checks = [CheckResult(kind="binary", ok=True, detail="/usr/bin/firefox (157.0.1)")]
    tried = installers.StrategyRun(
        installed=False,
        attempts=["system: x"],
        reasons=["system: zypper: MozillaFirefox (rc=4: Target initialization failed)"],
    )
    phase = _phase(ctx, monkeypatch, checks, lambda *a, **k: tried)
    ctx.runner.out_of_space = "install command (gcloud): No space left on device"
    component = SimpleNamespace(key="browser", title="Browser", verify=["x"], install=["x"])
    status, detail, _ = phase._process(component, None, None, PhaseResult(phase="install"))
    assert status == Status.PARTIAL and "Target initialization failed" in detail and "gcloud" not in detail


def test_a_full_disk_stops_the_phase_at_once(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    phase = InstallPhase(ctx)
    components = [
        SimpleNamespace(key=k, title=k, group="base", required=True, verify=["x"], install=["x"])
        for k in ("gcloud", "browser", "clipboard")
    ]
    monkeypatch.setattr(phase1_install, "load_components", lambda settings: components)
    monkeypatch.setattr(phase1_install, "select_components", lambda catalog, **k: (components, []))
    monkeypatch.setattr(phase, "_prefetch", lambda *a, **k: {})
    monkeypatch.setattr(phase, "_prepare_python_target", lambda *a, **k: None)
    monkeypatch.setattr(phase, "_seed_tokens", lambda *a, **k: None)
    monkeypatch.setattr(phase, "_publish", lambda *a, **k: None)
    monkeypatch.setattr(phase, "_record_environment", lambda *a, **k: None)
    processed: list[str] = []

    def process(component, *_a):
        processed.append(component.key)
        ctx.runner.out_of_space = "install command (gcloud): No space left on device"
        return Status.PARTIAL, "reinstall failed", []

    monkeypatch.setattr(phase, "_process", process)
    result = PhaseResult(phase="install")
    phase.execute(result)
    assert processed == ["gcloud"]
    rest = {s.name: s for s in result.steps if s.name in ("browser", "clipboard")}
    assert all(s.status == Status.SKIPPED and "disk is full" in s.detail for s in rest.values()) and len(rest) == 2


# -- once per package, and names ------------------------------------------------------------------


def test_an_owning_package_is_reinstalled_once_per_run(ctx, monkeypatch, tmp_path) -> None:
    binary = tmp_path / "python3"
    binary.write_text("x")
    checks = [CheckResult(kind="binary", ok=True, path=str(binary), detail="python3 (3.13)")]
    ran: list[int] = []
    phase = _phase(ctx, monkeypatch, checks, lambda *a, **k: ran.append(1))
    made = Installer(ctx)
    made.reinstalled.add("python313-base")
    monkeypatch.setattr(
        ownership, "find", lambda *a, **k: ownership.Owner(kind="system", packages=["python313-base"], path=str(binary))
    )
    component = SimpleNamespace(key="pip", title="pip", verify=["x"], install=[{"kind": "system"}])
    status, detail, _ = phase._process(component, None, made, PhaseResult(phase="install"))
    assert status == Status.OK and "reinstalled this run with python313-base" in detail and not ran


def test_a_kept_component_without_a_path_is_named_by_its_title() -> None:
    assert (
        InstallPhase._describe([CheckResult(kind="command", ok=True)], "gke-gcloud-auth-plugin")
        == "gke-gcloud-auth-plugin"
    )
    assert InstallPhase._describe([CheckResult(kind="command", ok=True)]) == "verified"


# -- phase 4's on-demand installs -----------------------------------------------------------------


def test_phase_4_installs_nothing_on_a_full_disk(ctx, monkeypatch) -> None:
    import test_mcp_provisioning as harness

    from initialize_claude_ai.mcp_auto_discovery.provisioning import Provisioner

    machine = harness.Machine(ctx, monkeypatch)
    ctx.runner.out_of_space = "zypper install: No space left on device"
    assert Provisioner.for_run(ctx).ensure("go", reason="launch command") == ""
    assert machine.calls == []
    [record] = Provisioner.for_run(ctx).records
    assert "disk is full" in record.detail


# -- what Claude Code already has ---------------------------------------------------------------------


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("install.check_space") is True
    assert settings.int_("install.space_margin_mb") == space.DEFAULT_MARGIN_MB
    assert settings.bool_("install.clean_failed_step_leftovers") and settings.bool_("install.stop_on_full_disk")
    assert tuple(settings.str_list("install.ownership.bin_folder_names")) == ownership.BIN_FOLDER_NAMES
    assert settings.bool_("mcp.registration.skip_values_claude_has") is True


def test_the_verdict_never_asks_for_a_value_claude_code_already_has(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    def verdict_after(held: bool) -> str:
        ctx.dry_run = False
        if held:
            SettingsWriter(ctx, settings_path(ctx)).apply({"env": {"BUILD_EXAMPLE_TEST_USERNAME": "someone"}})
        phase = McpPhase(ctx)
        result = PhaseResult(phase=phase.slug, title=phase.title)
        candidate = Candidate(kind="stdio", name="build-example-test", command=["python3", "/b.py"])
        candidate.secret_env = {"BUILD_EXAMPLE_TEST_USERNAME": "env:BUILD_EXAMPLE_TEST_USERNAME"}
        candidate.handshake = Handshake(ok=True, tools=["whoami"])
        outcome = TargetOutcome(target="https://build.example.test")
        stage = StageOutcome(stage="bridge", candidates=[candidate], verified=[candidate])
        outcome.stages.append(stage)
        outcome.absorb(stage)
        phase._report_verdict(result, McpWriter(ctx), [outcome])
        return " ".join(s.detail for s in result.steps)

    assert "BUILD_EXAMPLE_TEST_USERNAME" in verdict_after(False)
    assert "export BUILD_EXAMPLE_TEST_USERNAME" not in verdict_after(True)


def test_an_entry_whose_values_are_all_set_but_unproved_is_named_not_called_fine(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    ctx.dry_run = False
    SettingsWriter(ctx, settings_path(ctx)).apply({"env": {"CONF_EXAMPLE_TOKEN": "x" * 24}})
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    candidate = Candidate(kind="stdio", name="conf-example-test", command=["uvx", "mcp-atlassian"])
    candidate.handshake = Handshake(ok=True, tools=["confluence_search"], credential_pending=["CONF_EXAMPLE_TOKEN"])
    writer = McpWriter(ctx)
    registration = Registration(
        name="conf-example-test", candidate=candidate, entry={"command": "uvx"}, target="https://conf.example.test"
    )
    registration.provisional, registration.needs_env = True, ["CONF_EXAMPLE_TOKEN"]
    writer.published.append(registration)
    phase._report_verdict(result, writer, [TargetOutcome(target="https://conf.example.test")])
    text = " ".join(s.detail for s in result.steps)
    assert "NOT PROVED -- conf-example-test" in text and "No API key" not in text


def test_a_leftover_carrying_this_program_s_marker_is_removed(ctx, monkeypatch, tmp_path) -> None:
    from initialize_claude_ai.install import space

    sdk = _sdk(tmp_path)

    def failing(*_a, **_k):
        ours = tmp_path / "google-cloud-sdk.ours"
        ours.mkdir()
        (ours / space.OWNED_MARKER).write_text("")
        return installers.StrategyRun(installed=False, attempts=["command: rc=1"], reasons=["command: rc=1"])

    checks = [CheckResult(kind="binary", ok=True, path=str(sdk / "bin" / "gcloud"), detail="gcloud (590.0.0)")]
    phase = _phase(ctx, monkeypatch, checks, failing)
    phase._process(_gcloud(sdk), None, Installer(ctx), PhaseResult(phase="install"))
    assert not (tmp_path / "google-cloud-sdk.ours").exists()


def test_a_failed_reinstall_that_broke_the_tool_puts_a_runnable_copy_back(ctx, monkeypatch, tmp_path) -> None:
    """gcloud's reinstall failed activating its staging copy, and /root/google-cloud-sdk had no bin/gcloud left."""
    sdk = _sdk(tmp_path)
    good = [CheckResult(kind="binary", ok=True, path=str(sdk / "bin" / "gcloud"), detail="gcloud (590.0.0)")]

    def failing(*_a, **_k):
        staging = tmp_path / "google-cloud-sdk.staging"
        (staging / "bin").mkdir(parents=True)
        (staging / "bin" / "gcloud").write_text("whole copy")
        (sdk / "bin" / "gcloud").unlink()  # what the vendor's failed activation left
        return installers.StrategyRun(
            installed=False, attempts=["command: rc=1"], reasons=["command: rc=1: Destination path already exists"]
        )

    phase = _phase(ctx, monkeypatch, good, failing)
    monkeypatch.setattr(
        phase,
        "_verify",
        lambda component, verifier: (
            good
            if (sdk / "bin" / "gcloud").exists()
            else [CheckResult(kind="binary", ok=False, path="", detail="gcloud not found")]
        ),
    )
    monkeypatch.setattr(phase, "_tool_runs", lambda path: Path(path).is_file())
    status, detail, _ = phase._process(_gcloud(sdk), None, Installer(ctx), PhaseResult(phase="install"))
    assert (sdk / "bin" / "gcloud").read_text() == "whole copy", "the runnable copy is back in place"
    assert list(tmp_path.glob("google-cloud-sdk.broken-*")), "what stood there is kept, never deleted"
    assert status == Status.PARTIAL


def test_packaged_install_ownership_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.install import space

    assert settings.str_("install.owned_marker") == space.OWNED_MARKER
    assert settings.bool_("install.deleting_needs_ownership") and settings.bool_("install.restore_after_failed_step")
