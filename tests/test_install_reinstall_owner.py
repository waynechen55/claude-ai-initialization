"""--force-install on a machine where everything was installed: npm and gcloud "failed", node was "CHANGED".

* npm's only openSUSE strategy named ``npm22``; the machine's npm came from
  ``npm26``. The system is now asked which package provides the command, and a
  name it has no package for falls back to what it does have.
* gcloud's script refused its own existing folder. A reinstall now goes through
  the owner of the copy in use -- gcloud repairs itself in place -- and an
  owner with no in-place form keeps what is there.
* node's nvm post-install timed out, and node was CHANGED although
  /usr/bin/node was never touched. A failed post-install step fails the
  strategy, and CHANGED needs the binary in use to have changed.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.install import installers, ownership
from initialize_claude_ai.install.installers import Installer, last_words
from initialize_claude_ai.install.verify import CheckResult
from initialize_claude_ai.phases import phase1_install
from initialize_claude_ai.phases.phase1_install import InstallPhase

GCLOUD_OUTPUT = (
    "####################   99.9%\n" + "#" * 120 + "  100.0%\nmkdir -p /root\n"
    '"/root/google-cloud-sdk" already exists and may contain out of date files.\n'
    "Remove /root/google-cloud-sdk or select a new installation directory, then run again.\n"
)


def live(ctx, monkeypatch, answers):
    """An Installer whose commands answer from ``answers(argv) -> CommandResult``."""
    ctx.dry_run = False
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append([str(a) for a in argv])
        return answers([str(a) for a in argv])

    monkeypatch.setattr(ctx.runner, "run", run)
    monkeypatch.setattr(ctx.runner, "which", lambda name, **_: f"/usr/bin/{os.path.basename(str(name))}")
    made = Installer(ctx)
    made.platform = SimpleNamespace(
        package_manager="zypper", is_root=True, arch="x64", system="Linux", sudo_prefix=lambda: []
    )
    return made, calls


# -- the installer's own words ----------------------------------------------------------------


def test_the_installer_s_own_last_words_are_the_reason_not_its_progress_bar() -> None:
    said = last_words(GCLOUD_OUTPUT)
    assert "already exists" in said and "then run again" in said and "%" not in said


def test_a_timeout_says_how_far_the_download_got(ctx) -> None:
    result = CommandResult(
        argv=["bash"], returncode=124, stdout="####### 69.1%\n######## 69.6%", duration=600, timed_out=True
    )
    assert Installer(ctx).failure_words(result) == "timed out after 600s while downloading (69.6%)"


def test_shell_code_keeps_its_variables_for_the_shell(ctx, monkeypatch) -> None:
    monkeypatch.setenv("NVM_DIR", "/somewhere/else")
    made = Installer(ctx)
    made.set_token("python", "/usr/bin/python3")
    code = 'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; {python} -V'
    assert made.expand_shell(code) == 'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; /usr/bin/python3 -V'


def test_a_web_page_is_never_run_as_an_installer(ctx, tmp_path) -> None:
    page, script = tmp_path / "page", tmp_path / "script"
    page.write_text("<!DOCTYPE html><html><head><title>Claude</title>")
    script.write_text("#!/bin/sh\necho '<html>'\n")
    assert Installer(ctx)._web_page(page) == "<!doctype html" and Installer(ctx)._web_page(script) == ""


def test_a_failed_post_install_step_fails_the_strategy(ctx, monkeypatch) -> None:
    def answers(argv):
        if argv[-1].endswith("nvm install --lts"):
            return CommandResult(argv=argv, returncode=124, stdout="### 69.1%", duration=600, timed_out=True)
        return CommandResult(argv=argv, returncode=0)

    made, _ = live(ctx, monkeypatch, answers)
    monkeypatch.setattr(made, "_download", lambda url, dest, timeout: (dest.write_text("#!/bin/bash\n"), (True, ""))[1])
    outcome = made.run(
        {
            "kind": "script",
            "url": "https://example.test/install.sh",
            "post_shell": '. "$NVM_DIR/nvm.sh" && nvm install --lts',
        },
        "node",
    )
    assert not outcome.ok and "post-install step failed" in outcome.detail and "69.1%" in outcome.detail


# -- names the system has --------------------------------------------------------------------------


def test_a_package_name_the_system_lacks_falls_back_to_the_one_it_has(ctx, monkeypatch) -> None:
    def answers(argv):
        if argv[:2] == ["/usr/bin/zypper", "--non-interactive"] and "npm22" in argv:
            return CommandResult(argv=argv, returncode=104, stderr="No provider of 'npm22' found.")
        if "search" in argv:
            return CommandResult(
                argv=argv,
                returncode=0,
                stdout=(
                    "S | Name | Summary | Type\n--+------+---------+-----\n"
                    "  | npm24 | package manager | package\n  | npm26 | package manager | package\n"
                    "  | npm8  | old | package\n"
                ),
            )
        if argv[0].endswith("rpm"):
            return CommandResult(argv=argv, returncode=1)
        return CommandResult(argv=argv, returncode=0)

    made, calls = live(ctx, monkeypatch, answers)
    outcome = made.run(
        {"kind": "system", "packages": {"zypper": ["npm22"]}, "package_patterns": {"zypper": ["^npm[0-9]+$"]}}, "npm"
    )
    assert outcome.ok and outcome.data["packages"] == ["npm26"]
    assert any(c[-1] == "npm26" for c in calls)


def test_an_installed_provider_is_preferred_to_a_guess(ctx, monkeypatch) -> None:
    def answers(argv):
        if argv[0].endswith("rpm") and "--whatprovides" in argv:
            return CommandResult(argv=argv, returncode=0, stdout="npm26\n")
        return CommandResult(argv=argv, returncode=0)

    made, _ = live(ctx, monkeypatch, answers)
    assert made.resolve_packages(
        "zypper", {"capabilities": ["npm"], "package_patterns": {"zypper": ["^npm[0-9]+$"]}}, "npm"
    ) == ["npm26"]


# -- who owns the copy in use ----------------------------------------------------------------------


def _component(**kw):
    base = dict(
        key="x", title="x", verify=[{"kind": "binary", "names": ["npm"]}], install=[], provides=["npx"], capabilities=[]
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_package_that_owns_or_provides_the_command_owns_it(ctx, monkeypatch, tmp_path) -> None:
    binary = tmp_path / "npm"
    binary.write_text("x")

    def answers(argv):
        if "-qf" in argv:
            return CommandResult(argv=argv, returncode=0, stdout="nodejs-common\n")
        if "--whatprovides" in argv and argv[-1] == "npm":
            return CommandResult(argv=argv, returncode=0, stdout="npm26\n")
        return CommandResult(argv=argv, returncode=1)

    made, _ = live(ctx, monkeypatch, answers)
    component = _component(install=[{"kind": "system", "packages": {"zypper": ["npm22"]}}])
    owner = ownership.find(made, component, [CheckResult(kind="binary", ok=True, path=str(binary))])
    assert owner.kind == "system" and owner.packages == ["nodejs-common", "npm26"]
    [forced], why = ownership.plan(made, component, owner)
    assert forced["packages"]["zypper"] == ["nodejs-common", "npm26"] and not why


def test_a_script_install_is_reinstalled_by_its_own_in_place_form(ctx, monkeypatch, tmp_path) -> None:
    sdk = tmp_path / "google-cloud-sdk" / "bin"
    sdk.mkdir(parents=True)
    (sdk / "gcloud").write_text("x")
    made, _ = live(ctx, monkeypatch, lambda argv: CommandResult(argv=argv, returncode=1))
    script = {
        "kind": "script",
        "url": "https://sdk.example.test",
        "adds_bin_dir": str(sdk),
        "reinstall_argv": ["{binary}", "components", "reinstall", "--quiet"],
    }
    component = _component(verify=[{"kind": "binary", "names": ["gcloud"]}], install=[script])
    owner = ownership.find(made, component, [CheckResult(kind="binary", ok=True, path=str(sdk / "gcloud"))])
    assert owner.kind == "script" and owner.index == 0
    [step], _ = ownership.plan(made, component, owner)
    assert step == {
        "kind": "command",
        "argv": [str(sdk / "gcloud"), "components", "reinstall", "--quiet"],
        "timeout_seconds": None,
    }

    without = _component(verify=component.verify, install=[{k: v for k, v in script.items() if k != "reinstall_argv"}])
    steps, why = ownership.plan(made, without, owner)
    assert steps == [] and "cannot reinstall in place" in why


def test_a_copy_where_a_kind_installs_belongs_to_that_kind(ctx, monkeypatch, tmp_path) -> None:
    modules = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    modules.mkdir(parents=True)
    (modules / "cli.js").write_text("x")
    link = tmp_path / "claude"
    link.symlink_to(modules / "cli.js")
    made, _ = live(ctx, monkeypatch, lambda argv: CommandResult(argv=argv, returncode=1))
    component = _component(
        verify=[{"kind": "binary", "names": ["claude"]}],
        install=[{"kind": "script", "url": "https://x.test"}, {"kind": "npm", "packages": ["c"]}],
    )
    owner = ownership.find(made, component, [CheckResult(kind="binary", ok=True, path=str(link))])
    assert owner.kind == "npm" and owner.index == 1
    assert ownership.plan(made, component, owner)[0] == [component.install[1]]


# -- phase 1 -------------------------------------------------------------------------------------


def _phase(ctx, monkeypatch, checks, tried):
    ctx.dry_run = False
    ctx.settings.set("install.force", True)
    phase = InstallPhase(ctx)
    monkeypatch.setattr(phase, "_verify", lambda component, verifier: checks)
    monkeypatch.setattr(phase1_install, "run_strategies", lambda *a, **k: tried)
    return phase


def test_an_owner_that_cannot_reinstall_in_place_keeps_the_copy(ctx, monkeypatch, tmp_path) -> None:
    sdk = tmp_path / "sdk" / "bin"
    sdk.mkdir(parents=True)
    (sdk / "gcloud").write_text("x")
    checks = [CheckResult(kind="binary", ok=True, path=str(sdk / "gcloud"), detail=f"{sdk}/gcloud (590.0.0)")]
    ran: list[int] = []
    phase = _phase(ctx, monkeypatch, checks, None)
    monkeypatch.setattr(phase1_install, "run_strategies", lambda *a, **k: ran.append(1))
    component = SimpleNamespace(
        key="gcloud",
        title="gcloud",
        verify=[{"kind": "binary", "names": ["gcloud"]}],
        install=[{"kind": "script", "url": "https://x.test", "adds_bin_dir": str(sdk)}],
        provides=[],
        capabilities=[],
    )
    status, detail, _ = phase._process(component, None, Installer(ctx), PhaseResult(phase="install"))
    assert status == Status.OK and "kept" in detail and "cannot reinstall in place" in detail and not ran


def test_changed_needs_the_copy_in_use_to_change(ctx, monkeypatch, tmp_path) -> None:
    node = tmp_path / "node"
    node.write_text("x")
    checks = [CheckResult(kind="binary", ok=True, path=str(node), version="26.7.0", detail="node (26.7.0)")]
    tried = installers.StrategyRun(installed=True, attempts=["script: ok"])
    phase = _phase(ctx, monkeypatch, checks, tried)
    component = SimpleNamespace(key="node", title="Node.js", verify=["x"], install=["x"])
    status, detail, _ = phase._process(component, None, None, PhaseResult(phase="install"))
    assert status == Status.PARTIAL and "did not change" in detail


def test_a_failed_reinstall_gives_the_installer_s_own_words(ctx, monkeypatch) -> None:
    checks = [CheckResult(kind="binary", ok=True, detail="gcloud (590.0.0)")]
    said = 'script: installer rc=1: "/root/google-cloud-sdk" already exists … then run again.'
    tried = installers.StrategyRun(
        installed=False, attempts=["script: x"], remediation=["Run it manually"], reasons=[said]
    )
    phase = _phase(ctx, monkeypatch, checks, tried)
    component = SimpleNamespace(key="gcloud", title="gcloud", verify=["x"], install=["x"])
    status, detail, _ = phase._process(component, None, None, PhaseResult(phase="install"))
    assert status == Status.PARTIAL and "already exists" in detail and "Run it manually" not in detail


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("install.reinstall_owner_only") is True
    assert settings.bool_("install.changed_needs_proof") is True
    assert settings.str_("install.progress_line_pattern") == installers.PROGRESS_LINE_PATTERN
    assert tuple(settings.str_list("install.script_web_page_markers")) == installers.WEB_PAGE_MARKERS
    assert settings.dict_("install.ownership.path_markers") == ownership.PATH_MARKERS
    assert tuple(settings.str_list("install.ownership.in_place_kinds")) == ownership.IN_PLACE_KINDS
