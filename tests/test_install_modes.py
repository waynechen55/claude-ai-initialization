"""The three phase-1 modifiers: --force-install, --downgrade, --break-dependencies.

Each is a setting the installer reads, so the tests drive the installer with a
patched configuration rather than the command line -- the CLI mapping is covered
in ``test_cli``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import PhaseResult
from initialize_claude_ai.install.installers import (
    Installer,
    PythonEnvironment,
    looks_externally_managed,
)
from initialize_claude_ai.install.verify import CheckResult, Verifier
from initialize_claude_ai.mcp_auto_discovery.runtime import add_venv_to_path
from initialize_claude_ai.phases.phase1_install import InstallPhase


def installer(ctx, **overrides) -> Installer:
    """An Installer whose ``install.*`` settings carry ``overrides``."""
    if overrides:
        data = dict(ctx.settings.data)
        data["install"] = {**data.get("install", {}), **overrides}
        ctx.settings = Settings(data=data, environ=ctx.settings.environ)
    made = Installer(ctx)
    made.set_token("python", "/usr/bin/python3")
    made.set_token("uv", "")
    return made


# -- --force-install ---------------------------------------------------------


def test_the_plain_install_command_is_used_by_default(ctx) -> None:
    assert installer(ctx)._install_template("zypper")[-1] == "--auto-agree-with-licenses"


def test_force_appends_the_managers_reinstall_flag(ctx) -> None:
    assert "--force" in installer(ctx, force=True)._install_template("zypper")


def test_force_can_replace_the_whole_command(ctx) -> None:
    """dnf reinstalls with a different subcommand, not an extra flag."""
    assert installer(ctx, force=True)._install_template("dnf") == ["dnf", "reinstall", "-y"]


def test_pacman_forces_by_dropping_needed(ctx) -> None:
    forced = installer(ctx, force=True)._install_template("pacman")
    assert "--needed" not in forced and forced[:2] == ["pacman", "-S"]


def test_a_manager_with_no_forcing_form_installs_normally(ctx) -> None:
    """Nothing is invented on a manager's behalf."""
    plain = installer(ctx)._install_template("swupd")
    assert installer(ctx, force=True)._install_template("swupd") == plain


# -- --downgrade -------------------------------------------------------------


def test_nothing_is_pinned_without_the_flag(ctx) -> None:
    assert installer(ctx)._pinned("pip", ["anthropic"]) == ["anthropic"]


def test_a_pinned_package_gets_its_version(ctx) -> None:
    assert installer(ctx, downgrade=True)._pinned("pip", ["anthropic"]) == ["anthropic==1.4.0"]


def test_an_npm_scope_is_not_mistaken_for_a_version(ctx) -> None:
    """'@' opens an npm scope and also separates its version; only the second counts."""
    pinned = installer(ctx, downgrade=True)._pinned("npm", ["@anthropic-ai/claude-code"])
    assert pinned == ["@anthropic-ai/claude-code@2.0.77"]


@pytest.mark.parametrize(
    "ecosystem,package",
    [("pip", "anthropic==9.9.9"), ("pip", "anthropic>=1.0"), ("npm", "mcp-remote@0.13.5")],
)
def test_an_explicit_version_is_left_alone(ctx, ecosystem: str, package: str) -> None:
    assert installer(ctx, downgrade=True)._pinned(ecosystem, [package]) == [package]


def test_an_unlisted_package_still_resolves_to_latest(ctx) -> None:
    """The table is a set of known-good floors, not a lock file."""
    assert installer(ctx, downgrade=True)._pinned("pip", ["httpx"]) == ["httpx"]


# -- --break-dependencies ----------------------------------------------------


def _argv(ctx, *, env: PythonEnvironment | None = None, **overrides) -> list[str]:
    made = installer(ctx, **overrides)
    made._python_env["python3"] = env or PythonEnvironment(externally_managed=True)
    made.set_token("python", "python3")
    # Named rather than found on PATH, so the choice of front end is the test's.
    made.set_token("uv", "/opt/uv/bin/uv")
    argv, _label = made._python_install_argv(["anthropic"])
    return argv


def _pip_argv(ctx, **overrides) -> list[str]:
    return _argv(ctx, prefer_uv=False, **overrides)


def test_pep_668_is_respected_by_default(ctx) -> None:
    assert "--break-system-packages" not in _pip_argv(ctx)


def test_break_dependencies_overrides_pep_668(ctx) -> None:
    argv = _pip_argv(ctx, allow_break_system_packages=True)
    assert "--break-system-packages" in argv and "--user" in argv


def test_a_virtualenv_needs_no_override(ctx) -> None:
    """Nothing is being broken inside a venv, so neither flag is added."""
    argv = _pip_argv(
        ctx,
        env=PythonEnvironment(in_venv=True, externally_managed=True),
        allow_break_system_packages=True,
    )
    assert "--break-system-packages" not in argv and "--user" not in argv


def test_force_and_break_reach_the_pip_command_together(ctx) -> None:
    argv = _pip_argv(ctx, force=True, allow_break_system_packages=True)
    assert "--force-reinstall" in argv and "--break-system-packages" in argv


# -- PEP 668, the part that was failing ---------------------------------------


def test_uv_is_told_about_pep_668_too(ctx) -> None:
    """The bug: uv refuses an externally managed interpreter exactly as pip
    does, but only pip was ever being given the override."""
    argv = _argv(ctx, allow_break_system_packages=True)
    assert argv[0] == "/opt/uv/bin/uv"
    assert "--break-system-packages" in argv


def test_an_unmanaged_interpreter_is_not_broken_into(ctx) -> None:
    """--break-dependencies is about PEP 668. Where there is no marker there is
    nothing to override, whatever the flag says."""
    argv = _pip_argv(ctx, env=PythonEnvironment(externally_managed=False), allow_break_system_packages=True)
    assert "--break-system-packages" not in argv


def test_the_user_target_takes_the_front_end_that_has_user(ctx) -> None:
    """'uv pip install' has no --user, so asking for one chooses pip."""
    made = installer(ctx, prefer_uv=True)
    made._python_env["python3"] = PythonEnvironment(externally_managed=False)
    made.set_token("python", "python3")
    made.python_target = "user"
    argv, label = made._python_install_argv(["anthropic"])
    assert label == "pip install" and "--user" in argv


@pytest.mark.parametrize(
    "stderr",
    [
        "error: externally-managed-environment",  # pip's wording
        "error: The interpreter at /usr is externally managed, and indicates",  # uv's
    ],
)
def test_both_front_ends_are_recognised_as_pep_668_refusals(stderr: str) -> None:
    """Matching only pip's hyphenated phrase is why a uv host saw a bare rc=2."""
    assert looks_externally_managed(CommandResult(argv=["uv"], returncode=2, stderr=stderr))


def test_an_ordinary_failure_is_not_mistaken_for_pep_668() -> None:
    result = CommandResult(argv=["uv"], returncode=1, stderr="error: no such package 'anthropc'")
    assert not looks_externally_managed(result)


# -- what "auto" decides ------------------------------------------------------


def _auto(ctx, env: PythonEnvironment, **overrides) -> tuple[str, PhaseResult]:
    made = installer(ctx, **overrides)  # patches ctx.settings, so build the phase after
    phase = InstallPhase(ctx)
    made._python_env["python3"] = env
    result = PhaseResult(phase="install", title="Install")
    return phase._auto_python_target(made, "python3", result), result


def test_auto_reaches_for_a_virtualenv_on_a_pep_668_host(ctx) -> None:
    """The reported bug: on openSUSE, Debian, Fedora, Arch and Homebrew the
    system interpreter refuses both pip and uv, and 'auto' used to mean
    'try anyway and fail'."""
    target, result = _auto(ctx, PythonEnvironment(externally_managed=True))
    assert target == "venv"
    assert "externally managed" in result.steps[-1].detail


def test_auto_leaves_an_ordinary_interpreter_alone(ctx) -> None:
    """No marker, no reason to build anything."""
    target, result = _auto(ctx, PythonEnvironment(externally_managed=False))
    assert target == "system" and result.steps == []


def test_auto_does_not_second_guess_an_active_virtualenv(ctx) -> None:
    target, _ = _auto(ctx, PythonEnvironment(in_venv=True, externally_managed=True))
    assert target == "system"


def test_break_dependencies_keeps_auto_on_the_system_interpreter(ctx) -> None:
    """Asking to install system-wide is asking not to be sent to a venv."""
    target, _ = _auto(ctx, PythonEnvironment(externally_managed=True), allow_break_system_packages=True)
    assert target == "system"


def test_a_venv_from_another_python_is_not_put_on_sys_path(ctx, tmp_path) -> None:
    """Compiled wheels are ABI-specific: importing 3.14's rpds into 3.13 raises.
    Reporting the SDK missing and using the CLI runtime is the safe answer."""
    venv = tmp_path / "venv"
    (venv / "lib" / "python2.7" / "site-packages").mkdir(parents=True)
    ctx.env.set("PYTHON_VENV", str(venv))
    assert add_venv_to_path(ctx) == ""


def test_a_venv_from_this_python_is_put_on_sys_path(ctx, tmp_path) -> None:
    venv = tmp_path / "venv"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    packages = venv / "lib" / version / "site-packages"
    packages.mkdir(parents=True)
    ctx.env.set("PYTHON_VENV", str(venv))
    try:
        assert add_venv_to_path(ctx) == str(packages)
        assert str(packages) in sys.path
    finally:
        sys.path.remove(str(packages))


def test_verifying_python3_does_not_undo_the_virtualenv(ctx, tmp_path) -> None:
    """The 'python3' component provides {python}. Publishing it after a venv was
    chosen pointed the token back at the system interpreter, and the SDKs were
    then verified against the wrong one -- a full run failed where
    --install-only claude-sdk-python passed, because only a full run gets there.
    """
    phase = InstallPhase(ctx)
    verifier, made = Verifier(ctx), Installer(ctx)
    venv_python = tmp_path / "venv" / "bin" / "python"
    verifier.set_token("python", str(venv_python))
    made.set_token("python", str(venv_python))
    phase._pinned_tokens = frozenset({"python"})

    component = SimpleNamespace(provides_token="python", key="python3", env_key=lambda: "python3")
    checks = [CheckResult(kind="binary", ok=True, path="/usr/bin/python3", version="3.13.14")]
    phase._publish(component, checks, verifier, made)

    assert verifier.tokens["python"] == str(venv_python)
    assert made.tokens["python"] == str(venv_python)


def test_an_unpinned_token_is_still_published(ctx) -> None:
    """Pinning is for the interpreter, not a general freeze."""
    phase = InstallPhase(ctx)
    verifier, made = Verifier(ctx), Installer(ctx)
    component = SimpleNamespace(provides_token="claude", key="claude-cli", env_key=lambda: "claude")
    checks = [CheckResult(kind="binary", ok=True, path="/usr/local/bin/claude", version="2.1.270")]
    phase._publish(component, checks, verifier, made)
    assert verifier.tokens["claude"] == "/usr/local/bin/claude"
