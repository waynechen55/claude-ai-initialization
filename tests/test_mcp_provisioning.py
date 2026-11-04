"""--mcp-provision-missing: installing a toolchain a candidate needs, and nothing else.

The contract that matters most is what does *not* change: which server is
picked, and the words a candidate is rejected in when its toolchain cannot be
installed. Everything else -- once per run, never a prompt in ``auto``, only
catalog strategies -- is how it stays safe to leave on.
"""

from __future__ import annotations

import threading
import time

import pytest

from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.install.components import ComponentSpec, _apply_overrides
from initialize_claude_ai.install.installers import StrategyRun
from initialize_claude_ai.install.verify import CheckResult
from initialize_claude_ai.mcp_auto_discovery import provisioning
from initialize_claude_ai.mcp_auto_discovery.provisioning import (
    DECLINED,
    FAILED,
    INSTALLED,
    Provisioner,
    working_on,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

GO = {
    "key": "toolchain-go",
    "title": "Go",
    "verify": [{"kind": "binary", "names": ["go"]}],
    "provides": ["gofmt"],
    "install": [{"kind": "system", "packages": {"zypper": ["go"]}}, {"kind": "script", "url": "u"}],
}


class Machine:
    """What is installed, what installing does, and what was asked of it."""

    def __init__(self, ctx, monkeypatch, *, installs: bool = True, delay: float = 0.0, catalog=(GO,)):
        self.present: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []
        self.ran: list[list[str]] = []
        self._lock = threading.Lock()
        monkeypatch.setattr(ctx, "dry_run", False)
        monkeypatch.setattr(ctx.runner, "dry_run", False)
        monkeypatch.setattr(
            ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}" if name in self.present else None
        )
        monkeypatch.setattr(ctx.runner, "run", self._run)
        monkeypatch.setattr(
            provisioning,
            "load_components",
            lambda settings: [
                _apply_overrides(ComponentSpec.from_mapping(raw), settings.get("install.overrides") or {})
                for raw in catalog
            ],
        )

        def run_strategies(installer, component, *, on_bin_dir, strategies=None, logger=None):
            time.sleep(delay)
            with self._lock:
                self.calls.append((component.key, [s["kind"] for s in strategies]))
                if installs:
                    self.present.update(component.commands())
            return StrategyRun(installed=installs, attempts=[f"{s['kind']}: ok" for s in strategies])

        machine = self

        class Verifier:
            def __init__(self, ctx):
                pass

            def set_token(self, *a):
                pass

            def check(self, spec):
                name = spec["names"][0]
                ok = name in machine.present
                return CheckResult(
                    kind="binary",
                    ok=ok,
                    path=f"/usr/bin/{name}" if ok else "",
                    detail="found" if ok else f"{name} not found",
                )

        monkeypatch.setattr(provisioning, "run_strategies", run_strategies)
        monkeypatch.setattr(provisioning, "Verifier", Verifier)
        # Root, so auto may use every strategy unless a test says otherwise.
        monkeypatch.setattr(Provisioner, "_can_elevate", lambda self: True)

    def _run(self, argv, **kwargs) -> CommandResult:
        self.ran.append(list(argv))
        return CommandResult(argv=list(argv), returncode=0)


def stdio(command, install=()) -> Candidate:
    return Candidate(kind="stdio", name="vendor-mcp", command=list(command), install=list(install))


# -- when nothing is installed ------------------------------------------------------


def test_never_installs_nothing(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    ctx.settings.set("mcp.packages.provision_missing", "never")
    assert Provisioner.for_run(ctx).ensure("go", reason="launch command") == ""
    assert machine.calls == []


def test_a_program_already_here_is_not_touched(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    machine.present.add("go")
    assert Provisioner.for_run(ctx).ensure("go", reason="launch command") == "/usr/bin/go"
    assert machine.calls == [] and Provisioner.for_run(ctx).records == []


def test_a_program_no_component_provides_is_left_missing(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    assert Provisioner.for_run(ctx).ensure("some-vendor-binary", reason="launch command") == ""
    assert machine.calls == [] and Provisioner.for_run(ctx).records == []


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (lambda ctx: setattr(ctx, "dry_run", True), "dry run"),
        (lambda ctx: ctx.settings.set("install.skip", ["toolchain-go"]), "--install-skip"),
        (lambda ctx: ctx.settings.set("install.on_demand_default", False), "on_demand = false"),
        (
            lambda ctx: ctx.settings.set("install.overrides", {"toolchain-go": {"on_demand": False}}),
            "on_demand = false",
        ),
    ],
)
def test_declined_with_its_reason(ctx, monkeypatch, arrange, reason) -> None:
    machine = Machine(ctx, monkeypatch)
    arrange(ctx)
    provisioner = Provisioner.for_run(ctx)
    assert provisioner.ensure("go", reason="launch command") == ""
    assert machine.calls == []
    (record,) = provisioner.records
    assert record.status == DECLINED and reason in record.detail
    assert any("--install-only toolchain-go" in line for line in record.remediation)


# -- installing ------------------------------------------------------------------------


def test_installed_once_and_found_by_any_command_it_provides(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    provisioner = Provisioner.for_run(ctx)
    with working_on("https://example.com"):
        assert provisioner.ensure("go", reason="launch command", server="s") == "/usr/bin/go"
    assert provisioner.ensure("gofmt", reason="install step") == "/usr/bin/gofmt"
    assert [key for key, _ in machine.calls] == ["toolchain-go"]
    (record,) = provisioner.records
    assert (record.status, record.address, record.server) == (INSTALLED, "https://example.com", "s")


def test_concurrent_addresses_share_one_install(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch, delay=0.2)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(Provisioner.for_run(ctx).ensure("go", reason="r")))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(machine.calls) == 1
    assert results == ["/usr/bin/go"] * 6
    assert len(Provisioner.for_run(ctx).records) == 1


def test_different_toolchains_are_never_installed_at_once(ctx, monkeypatch) -> None:
    """Two installs through one package manager would meet at its lock."""
    rust = {
        "key": "toolchain-rust",
        "verify": [{"kind": "binary", "names": ["cargo"]}],
        "install": [{"kind": "script", "url": "u"}],
    }
    Machine(ctx, monkeypatch, catalog=(GO, rust))
    active: list[int] = []
    peak: list[int] = [0]
    guard = threading.Lock()
    real = provisioning.run_strategies

    def counting(*args, **kwargs):
        with guard:
            active.append(1)
            peak[0] = max(peak[0], len(active))
        try:
            time.sleep(0.2)
            return real(*args, **kwargs)
        finally:
            with guard:
                active.pop()

    monkeypatch.setattr(provisioning, "run_strategies", counting)
    threads = [
        threading.Thread(target=Provisioner.for_run(ctx).ensure, args=(name,), kwargs={"reason": "r"})
        for name in ("go", "cargo", "go", "cargo")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1
    assert sorted(r.component for r in Provisioner.for_run(ctx).records) == ["toolchain-go", "toolchain-rust"]


def test_the_catalog_is_never_seen_without_its_index(ctx, monkeypatch) -> None:
    Machine(ctx, monkeypatch)
    slow = provisioning.command_index

    def command_index(catalog):
        time.sleep(0.2)
        return slow(catalog)

    monkeypatch.setattr(provisioning, "command_index", command_index)
    provisioner = Provisioner.for_run(ctx)
    found: list[object] = []
    threads = [threading.Thread(target=lambda: found.append(provisioner.provider("go"))) for _ in range(4)]
    for thread in threads:
        thread.start()
        time.sleep(0.05)
    for thread in threads:
        thread.join()
    assert all(f is not None for f in found) and len(found) == 4


def test_a_failure_is_remembered_not_retried(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch, installs=False)
    provisioner = Provisioner.for_run(ctx)
    assert provisioner.ensure("go", reason="r") == ""
    assert provisioner.ensure("go", reason="r") == ""
    assert len(machine.calls) == 1
    (record,) = provisioner.records
    assert record.status == FAILED and record.attempts


def test_auto_leaves_out_what_would_prompt(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    monkeypatch.setattr(Provisioner, "_can_elevate", lambda self: False)
    Provisioner.for_run(ctx).ensure("go", reason="r")
    assert machine.calls == [("toolchain-go", ["script"])]
    (record,) = Provisioner.for_run(ctx).records
    assert any("needs root" in line for line in record.attempts)


def test_auto_declines_when_every_strategy_would_prompt(ctx, monkeypatch) -> None:
    only_system = {**GO, "install": [GO["install"][0]]}
    machine = Machine(ctx, monkeypatch, catalog=(only_system,))
    monkeypatch.setattr(Provisioner, "_can_elevate", lambda self: False)
    assert Provisioner.for_run(ctx).ensure("go", reason="r") == ""
    assert machine.calls == []
    assert Provisioner.for_run(ctx).records[0].status == DECLINED


def test_always_may_use_every_strategy(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    monkeypatch.setattr(Provisioner, "_can_elevate", lambda self: False)
    ctx.settings.set("mcp.packages.provision_missing", "always")
    Provisioner.for_run(ctx).ensure("go", reason="r")
    assert machine.calls == [("toolchain-go", ["system", "script"])]


def test_elevation_is_asked_without_a_prompt_and_only_once(ctx, monkeypatch) -> None:
    asked: list[list[str]] = []
    monkeypatch.setattr(ctx.platform, "is_root", False)
    monkeypatch.setattr(ctx.platform, "sudo_prefix", lambda: ["sudo", "-n"])
    monkeypatch.setattr(ctx.runner, "probe", lambda argv, **k: asked.append(argv) or type("R", (), {"ok": False})())
    provisioner = Provisioner(ctx)
    assert provisioner._can_elevate() is False and provisioner._can_elevate() is False
    assert asked == [["sudo", "-n", *ctx.settings.str_list("mcp.packages.provision_privilege_probe")]]


# -- the verifier: picking and wording unchanged -----------------------------------------


def test_one_provisioner_serves_every_pipeline(ctx) -> None:
    assert Provisioner.for_run(ctx) is Provisioner.for_run(ctx)


def test_the_rejection_text_is_the_same_whether_or_not_provisioning_tried(ctx, monkeypatch) -> None:
    Machine(ctx, monkeypatch, installs=False)
    candidate = stdio(["go", "run", "example.invalid/server@latest"])
    tried = CandidateVerifier(ctx)._verify_stdio(candidate).error

    ctx.state.clear()
    ctx.settings.set("mcp.packages.provision_missing", "never")
    untried = CandidateVerifier(ctx)._verify_stdio(candidate).error

    assert tried == untried == "go is not installed"


def test_an_install_step_keeps_its_words_too(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch, installs=False)
    machine.present.add("vendor-mcp")
    candidate = stdio(["vendor-mcp"], install=[["gofmt", "-l", "."]])
    result = CandidateVerifier(ctx)._verify_stdio(candidate)
    assert result.error == "prerequisite needs gofmt, which is not installed (step: gofmt -l .)"
    assert machine.ran == []


def test_a_provisioned_install_step_then_runs(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    machine.present.add("vendor-mcp")
    CandidateVerifier(ctx)._verify_stdio(stdio(["vendor-mcp"], install=[["go", "install", "x@v1"]]))
    assert machine.calls and machine.ran[0][:2] == ["go", "install"]


def test_nothing_is_installed_when_no_trial_would_follow(ctx, monkeypatch) -> None:
    machine = Machine(ctx, monkeypatch)
    ctx.settings.set("mcp.packages.trial_run", False)
    CandidateVerifier(ctx)._verify_stdio(stdio(["go", "run", "x"]))
    assert machine.calls == []


# -- the report ----------------------------------------------------------------------------


def test_the_report_step_explains_and_never_fails(ctx, monkeypatch) -> None:
    Machine(ctx, monkeypatch, installs=False)
    Provisioner.for_run(ctx).ensure("go", reason="launch command", server="s")
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._report_toolchains(result)
    step = next(s for s in result.steps if s.name == "toolchains")
    assert step.status == Status.OK
    assert "could not install: toolchain-go" in step.detail
    assert step.data["provisioned"][0]["status"] == FAILED


def test_no_report_step_when_nothing_happened(ctx) -> None:
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._report_toolchains(result)
    assert not [s for s in result.steps if s.name == "toolchains"]
