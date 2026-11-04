"""Toolchains: what provides a command, what later phases need, and prefetching it.

Phase 4 launches servers with programs phase 1 had no other reason to install.
Everything here answers "which component puts this command here?" from the
catalog, so none of it may know a program by name.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.envfile import EnvStore
from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.install.components import ComponentSpec, _apply_overrides, load_components
from initialize_claude_ai.install.installers import Installer, InstallOutcome, run_strategies
from initialize_claude_ai.install.toolchains import (
    PREFETCH_MODES,
    PROVISION_MODES,
    command_index,
    demanded_commands,
    prefetch_components,
    prefetch_mode,
    provision_mode,
    query_bin_dir,
)
from initialize_claude_ai.phases import all_phases
from initialize_claude_ai.phases.phase1_install import InstallPhase


def component(key: str, *binaries: str, **extra) -> ComponentSpec:
    raw = {"key": key, "verify": [{"kind": "binary", "names": list(binaries)}] if binaries else []}
    raw.update(extra)
    return ComponentSpec.from_mapping(raw)


# -- the catalog as shipped ---------------------------------------------------------


def test_every_launcher_the_shipped_configuration_uses_has_a_provider(settings) -> None:
    """Otherwise prefetching and provisioning both silently cover nothing."""
    index = command_index(load_components(settings))
    for command, source in demanded_commands(settings):
        assert command in index, f"{command} (from {source}) has no catalog component"


def test_the_shipped_runtimes_are_optional_and_mapped(settings) -> None:
    catalog = load_components(settings)
    index = command_index(catalog)
    for command in ("go", "cargo", "deno", "bunx", "npx", "uvx", "docker"):
        assert command in index
    runtimes = [c for c in catalog if c.group == "mcp-runtimes"]
    assert runtimes and not any(c.required for c in runtimes)
    assert "mcp-runtimes" in settings.str_list("install.optional_groups")
    assert "mcp-runtimes" in settings.str_list("install.prefetch_groups")


def test_the_shipped_default_prefetches_nothing_new(settings) -> None:
    """'launchers' exists for launchers configured later; today uv and npm are selected anyway."""
    catalog = load_components(settings)
    selected = [c for c in catalog if c.group not in settings.str_list("install.optional_groups")]
    added, _ = prefetch_components(catalog, settings, mode="launchers", selected=selected)
    assert added == []


def test_modes_are_one_definition(settings) -> None:
    assert prefetch_mode(settings) in PREFETCH_MODES
    assert provision_mode(settings) in PROVISION_MODES


def test_an_unknown_mode_falls_back_to_the_old_behaviour(settings) -> None:
    settings.set("install.prefetch", "sometimes")
    settings.set("mcp.packages.provision_missing", "maybe")
    assert prefetch_mode(settings) == "none"
    assert provision_mode(settings) == "never"


@pytest.mark.parametrize(("raw", "mode"), [("true", "always"), ("Off", "never"), ("AUTO", "auto")])
def test_a_provisioning_mode_may_be_spelled_as_a_switch(settings, raw, mode) -> None:
    settings.set("mcp.packages.provision_missing", raw)
    assert provision_mode(settings) == mode


# -- command -> component -------------------------------------------------------------


def test_provides_counts_as_a_command_and_catalog_order_wins() -> None:
    first = component("npm", "npm", provides=["npx"])
    second = component("other", "npx")
    index = command_index([first, second])
    assert index["npx"] is first
    assert first.commands() == ["npm", "npx"]


def test_demanded_commands_reads_every_template_shape(settings) -> None:
    settings.set("demo.table", {"a": ["tool-a", "x"], "b": ["{python}", "-m", "y"]})
    settings.set("demo.nested", [["/opt/bin/tool-b", "z"], ["tool-a"]])
    settings.set("demo.single", ["tool-c", "--flag"])
    settings.set("install.demand_sources", ["demo.table", "demo.nested", "demo.single", "demo.absent"])
    assert demanded_commands(settings) == [
        ("tool-a", "demo.table"),
        ("tool-b", "demo.nested"),
        ("tool-a", "demo.nested"),  # each source that needs it is named
        ("tool-c", "demo.single"),
    ]


# -- prefetching ----------------------------------------------------------------------


@pytest.fixture
def catalog(settings):
    settings.set("demo.launchers", {"one": ["alpha", "run"], "two": ["beta"], "three": ["gamma"]})
    settings.set("install.demand_sources", ["demo.launchers"])
    settings.set("install.prefetch_groups", ["extras"])
    return [
        component("a", "alpha", group="extras"),
        component("b", "beta", group="extras", prefetch=False),
        component("c", "gamma", group="extras", platforms=["NoSuchOS"]),
        component("d", "delta", group="extras"),
        component("e", "epsilon", group="core"),
    ]


def keys(added) -> list[str]:
    return [c.key for c, _ in added]


def test_none_adds_nothing(catalog, settings) -> None:
    assert prefetch_components(catalog, settings, mode="none") == ([], [])


def test_launchers_adds_what_is_launched_and_explains_what_it_does_not(catalog, settings) -> None:
    added, notes = prefetch_components(catalog, settings, mode="launchers")
    assert keys(added) == ["a"]
    assert any("prefetch = false" in n and "'b'" in n for n in notes)


def test_all_adds_the_groups_too_and_keeps_catalog_order(catalog, settings) -> None:
    added, _ = prefetch_components(catalog, settings, mode="all")
    assert keys(added) == ["a", "b", "d"]


def test_selected_is_not_added_twice_and_skip_wins(catalog, settings) -> None:
    added, notes = prefetch_components(catalog, settings, mode="all", selected=[catalog[0]], skip=["d"])
    assert keys(added) == ["b"]
    assert any("--install-skip" in n for n in notes)
    added, _ = prefetch_components(catalog, settings, mode="all", skip=["extras"])
    assert added == []


def test_install_only_turns_prefetching_off(ctx, catalog) -> None:
    ctx.settings.set("install.prefetch", "all")
    phase = InstallPhase(ctx)
    selected: list[ComponentSpec] = []
    notes: list[str] = []
    assert phase._prefetch(catalog, selected, only=["e"], skip=[], notes=notes) == {}
    assert selected == [] and "ignored" in notes[0]
    reasons = phase._prefetch(catalog, selected, only=[], skip=[], notes=notes)
    assert list(reasons) == [c.key for c in selected] == ["a", "b", "d"]


# -- where a toolchain puts what it builds --------------------------------------------


def test_the_bin_dir_comes_from_stdout_with_its_suffix(ctx, monkeypatch, tmp_path) -> None:
    go = component(
        "go",
        "go",
        bin_dir_queries=[{"args": ["env", "GOBIN"]}, {"args": ["env", "GOPATH"], "suffix": "bin"}],
    )
    answers = {
        "GOBIN": CommandResult(argv=[], returncode=0, stdout="\n", stderr="warning: noise"),
        "GOPATH": CommandResult(argv=[], returncode=0, stdout=f"{tmp_path}/a:{tmp_path}/b\n"),
    }
    monkeypatch.setattr(ctx.runner, "probe", lambda argv, **k: answers[argv[-1]])
    assert query_bin_dir(ctx.runner, go, "/usr/bin/go", 5) == f"{tmp_path}/a/bin"
    assert query_bin_dir(ctx.runner, component("x", "x"), "/usr/bin/x", 5) == ""


def test_a_recorded_bin_dir_goes_on_the_path_as_is(tmp_path) -> None:
    env = EnvStore(state_dir=tmp_path, dry_run=True)
    env.set_binary("go", "/usr/lib/go/bin/go")
    env.set_bin_dir("go", "/home/u/go/bin")
    assert env.bin_dirs() == ["/usr/lib/go/bin", "/home/u/go/bin"]
    assert any(key.endswith("BINDIR_GO") for key, _ in env)


# -- overrides and strategies ------------------------------------------------------------


def test_overrides_set_component_fields_even_beside_strategies() -> None:
    spec = component("go", "go", install=[{"kind": "system"}])
    patched = _apply_overrides(
        spec,
        {"go": {"provides": ["gofmt"], "on_demand": False, "strategies": [{"kind": "brew"}]}},
    )
    assert patched.provides == ["gofmt"] and patched.on_demand is False
    assert patched.install == [{"kind": "brew"}]


def test_a_download_uses_the_vendors_architecture_name(ctx, monkeypatch) -> None:
    installer = Installer(ctx)
    monkeypatch.setattr(type(installer.platform), "arch", property(lambda self: "x64"))
    outcome = installer._install_download(
        {"url": "https://example.invalid/t-{arch}.tar.gz", "arch_names": {"x64": "amd64"}}, "t"
    )
    assert "t-amd64.tar.gz" in outcome.detail  # dry run: says what it would fetch


def test_privilege_is_read_from_the_strategy(ctx) -> None:
    installer = Installer(ctx)
    assert installer.needs_privilege({"kind": "system"})
    assert installer.needs_privilege({"kind": "download", "url": "https://x.invalid/a.rpm"})
    assert not installer.needs_privilege({"kind": "download", "url": "https://x.invalid/a.tar.gz"})
    assert not installer.needs_privilege({"kind": "script"})


def test_run_strategies_stops_at_the_first_success(ctx, monkeypatch) -> None:
    spec = component("t", "t", install=[{"kind": "one"}, {"kind": "two"}, {"kind": "three"}])
    results = {
        "one": InstallOutcome.skip("not here"),
        "two": InstallOutcome(ok=True, bin_dir="/opt/t/bin"),
        "three": InstallOutcome(ok=False),
    }
    installer = Installer(ctx)
    monkeypatch.setattr(installer, "run", lambda strategy, key: results[strategy["kind"]])
    dirs: list[str] = []
    run = run_strategies(installer, spec, on_bin_dir=dirs.append)
    assert run.installed and dirs == ["/opt/t/bin"]
    assert run.attempts == ["one: not here", "two: ok"]


# -- the command line -------------------------------------------------------------------


def parse(*argv: str):
    return cli.build_parser().parse_args(cli._normalise_argv(list(argv)))


def test_both_flags_map_to_their_settings() -> None:
    overrides = cli._overrides_from_args(parse("run", "--install-prefetch", "all", "--mcp-provision-missing", "never"))
    assert "install.prefetch=all" in overrides
    assert "mcp.packages.provision_missing=never" in overrides
    assert not any(
        o.startswith(("install.prefetch=", "mcp.packages.provision_missing="))
        for o in cli._overrides_from_args(parse("run"))
    )


def test_the_flags_accept_only_their_modes() -> None:
    with pytest.raises(SystemExit):
        parse("run", "--install-prefetch", "some")
    with pytest.raises(SystemExit):
        parse("run", "--mcp-provision-missing", "sometimes")


def test_a_flag_for_a_phase_not_in_the_run_is_warned_about() -> None:
    by_slug = {c.enable_key or c.slug: c for c in all_phases()}
    args = parse("run", "--install-prefetch", "all", "--mcp-provision-missing", "always")
    warnings = cli.inert_phase_flags(args, [by_slug["mcp"]])
    assert len(warnings) == 1 and "--install-prefetch" in warnings[0]
    assert cli.inert_phase_flags(args, [by_slug["install"], by_slug["mcp"]]) == []
    assert cli.inert_phase_flags(parse("run"), []) == []
