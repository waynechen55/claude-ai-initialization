"""Argument handling and the phase registry."""

from __future__ import annotations

import json

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.config import ENV_PREFIX
from initialize_claude_ai.core.logging_setup import DEFAULT_PALETTE, PALETTES
from initialize_claude_ai.phases import all_phases, select  # importing also registers them


def parse(*argv: str):
    return cli.build_parser().parse_args(cli._normalise_argv(list(argv)))


def every_action():
    """Every argparse action in the program, subparsers included."""
    import argparse

    stack = [cli.build_parser()]
    while stack:
        parser = stack.pop()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
                continue
            yield parser.prog, action


# -- self-documentation -------------------------------------------------------


def test_every_argument_says_what_it_does() -> None:
    """--help is the only documentation most people will read, so no flag may
    reach it undescribed."""
    undocumented = [
        f"{prog} {action.option_strings or action.dest}" for prog, action in every_action() if not action.help
    ]
    assert undocumented == []


def test_every_argument_appears_in_the_readme() -> None:
    """The README's reference table is the flag list people read before running
    anything; a flag missing from it is a feature nobody finds."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pages = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    # The generated reference lists every flag by construction; this asks for prose.
    readme = "\n".join(p.read_text(encoding="utf-8") for p in pages if p.name != "cli-reference.md")
    missing = sorted(
        option
        for _prog, action in every_action()
        for option in action.option_strings
        if option.startswith("--") and option != "--help" and option not in readme
    )
    assert missing == []


def run_groups() -> dict[str, str]:
    """Every option of ``run``, mapped to the title of the group it is shown in."""
    import argparse

    parser = cli.build_parser()
    subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {
        option: g.title
        for g in subs.choices["run"]._action_groups
        for action in g._group_actions
        for option in action.option_strings
    }


def phase_title(section: str) -> str:
    cls = next(c for c in all_phases() if (c.enable_key or c.slug) == section)
    return cli.phase_group_title(cls)


def test_the_program_help_lists_every_option_of_every_command(capsys) -> None:
    """``--help`` used to stop at the shared options and the command names: argparse
    never prints a subcommand's options from its parent, so not one phase flag
    was visible. Held against the parsers themselves, so a new flag anywhere is
    covered without this test being edited."""
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(cli._normalise_argv(["--help"]))
    assert exited.value.code == 0
    shown = capsys.readouterr().out
    missing = sorted(
        {option for _prog, action in every_action() for option in action.option_strings}
        - set(__import__("re").findall(r"(?<![\w-])(--?[\w][\w-]*)", shown))
    )
    assert missing == []
    for cls in all_phases():
        assert f"{cli.phase_group_title(cls)}:" in shown
    for name in ("run", "phases", "config", "palette", "env"):
        assert f"=== initialize-claude-ai {name}: " in shown


def test_the_program_help_keeps_the_examples_last(capsys) -> None:
    with pytest.raises(SystemExit):
        parse("--help")
    shown = capsys.readouterr().out
    assert shown.rstrip().endswith("run --dry-run")
    assert shown.index("=== initialize-claude-ai run: run one or more phases ===") < shown.index("examples:")


def test_no_run_option_is_left_in_the_untitled_group() -> None:
    """argparse's untitled group reads as "belongs to no phase"; only -h may be there."""
    stray = sorted(o for o, title in run_groups().items() if title == "options")
    assert stray == ["--help", "-h"]


def test_every_phase_has_a_group_named_after_it() -> None:
    titles = set(run_groups().values())
    for cls in all_phases():
        title = cli.phase_group_title(cls)
        assert title in titles
        # The heading names what --phases accepts, so the two cannot drift.
        assert f"phase {cls.number} ({cls.slug})" in title


@pytest.mark.parametrize("flag", cli.SETTING_FLAGS, ids=lambda f: f.flag)
def test_a_setting_flag_is_shown_under_the_phase_that_reads_it(flag) -> None:
    """--agent-max-turns and --agent-max-attempts were listed as general options,
    though only phase 4 reads them. The group is derived from the key now."""
    sections = {c.enable_key or c.slug for c in all_phases()}
    expected = phase_title(flag.section) if flag.section in sections else "run control"
    assert run_groups()[flag.flag] == expected


def test_the_agent_flags_are_phase_four_flags() -> None:
    groups = run_groups()
    assert groups["--agent-max-turns"] == phase_title("mcp")
    assert groups["--agent-max-attempts"] == phase_title("mcp")
    assert groups["--timeout-scale"] == "run control"


@pytest.mark.parametrize("flag", cli.SECRET_FLAGS, ids=lambda f: f.plain)
def test_a_credential_flag_is_filed_under_a_phase_that_reads_its_key(flag) -> None:
    """A credential grouped by hand must still name the phase its key belongs to,
    unless it is deliberately shared between phases."""
    groups = run_groups()
    if flag.group == "shared":
        assert groups[flag.plain] == "any phase"
    else:
        assert flag.key.split(".", 1)[0] == flag.group
        assert groups[flag.plain] == phase_title(flag.group)
    assert groups[flag.ref] == groups[flag.plain]


def test_help_is_short_enough_to_scan() -> None:
    """--help is a reference, not the manual; the rationale lives in the README.
    Entries of ten lines buried the flags they described."""
    too_long = sorted(
        f"{prog} {action.option_strings}" for prog, action in every_action() if action.help and len(action.help) > 330
    )
    assert too_long == []


def test_no_agent_names_every_flag_it_disables() -> None:
    no_agent = next(a for _p, a in every_action() if "--no-agent" in a.option_strings)
    for flag in cli.SETTING_FLAGS:
        assert (flag.flag in no_agent.help) is flag.agent_only


def test_every_flag_the_sources_table_names_is_a_real_flag() -> None:
    """``[sources]`` is what a "cannot continue without this" failure reads out.

    It tells the operator which flag to pass, so a name that has gone stale
    there sends them to type something argparse will reject -- at exactly the
    moment they are already stuck.
    """
    from initialize_claude_ai.core.config import load_settings

    known = {option for _prog, action in every_action() for option in action.option_strings}
    named = {
        str(entry.get("flag", ""))
        for entry in load_settings().dict_("sources").values()
        if isinstance(entry, dict) and entry.get("flag")
    }
    assert named - known == set()


def test_every_value_taking_argument_says_what_it_takes() -> None:
    """A bare EMAIL/PATH/LIST in the usage line is what distinguishes
    --gcloud-service-account from --gcloud-service-account-file at a glance."""
    unlabelled = [
        f"{prog} {action.option_strings}"
        for prog, action in every_action()
        if action.option_strings and action.nargs != 0 and not (action.metavar or action.choices)
    ]
    assert unlabelled == []


def test_bare_flags_imply_the_run_command() -> None:
    assert cli._normalise_argv(["--dry-run"]) == ["run", "--dry-run"]
    assert cli._normalise_argv(["run", "--dry-run"]) == ["run", "--dry-run"]
    assert cli._normalise_argv(["phases"]) == ["phases"]
    assert cli._normalise_argv([]) == ["run"]


def test_help_and_version_are_not_rewritten() -> None:
    assert cli._normalise_argv(["--version"]) == ["--version"]


def test_an_option_value_is_not_mistaken_for_the_subcommand() -> None:
    """Regression: ``--config x config http`` was rewritten to a bare ``run``
    because the file path, not being a flag, ended the scan."""
    assert cli._normalise_argv(["--config", "x.toml", "config", "http"]) == [
        "--config",
        "x.toml",
        "config",
        "http",
    ]
    assert cli._normalise_argv(["--log-level", "debug", "phases"]) == [
        "--log-level",
        "debug",
        "phases",
    ]
    # ...and the shorthand still works when there really is no subcommand.
    assert cli._normalise_argv(["--config", "x.toml", "--dry-run"]) == [
        "run",
        "--config",
        "x.toml",
        "--dry-run",
    ]


def test_a_global_flag_is_accepted_on_either_side_of_the_subcommand() -> None:
    for argv in (("--config", "x.toml", "config"), ("config", "--config", "x.toml")):
        assert parse(*argv).config == ["x.toml"]


def test_a_global_flag_survives_the_subcommand() -> None:
    """Regression: subparser defaults used to overwrite the shared parent's."""
    args = parse("--json", "phases")
    for key, value in cli.COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    assert args.json is True


def test_flags_map_onto_configuration_keys() -> None:
    args = parse(
        "run",
        "--provision-backend",
        "vertex",
        "--gcloud-region",
        "us-east5",
        "--vertex-project",
        "example-project",
        "--default-model",
        "claude-opus-5",
        "--headless",
        "--exhaustive-mcp",
        "--mcp-token-ref",
        "env:MY_TOKEN",
    )
    overrides = cli._overrides_from_args(args)
    assert "provision.backend=vertex" in overrides
    assert "provision.vertex.region=us-east5" in overrides
    assert "provision.vertex.project=example-project" in overrides
    assert "provision.settings.model=claude-opus-5" in overrides
    assert "auth.browser_mode=headless" in overrides
    assert "mcp.exhaustive=true" in overrides
    assert "mcp.discovery.auth.bearer_token_ref=env:MY_TOKEN" in overrides


def test_one_project_flag_reaches_both_phases_that_need_it() -> None:
    """Signing in is not project-neutral: quota, API enablement and Vertex
    access all attach to a project, so phase 2 must be told the same one phase 3
    provisions. Two keys, because a config file may still set them apart."""
    for spelling in ("--gcloud-project", "--vertex-project"):
        overrides = cli._overrides_from_args(parse("run", spelling, "example-project"))
        assert "auth.google_cloud.project=example-project" in overrides
        assert "provision.vertex.project=example-project" in overrides


def test_a_flag_more_than_one_phase_reads_is_not_filed_under_a_phase() -> None:
    """A flag in the "phase 3" group reads as one phase 3 acts on, and somebody
    running --phases 2 would leave it off and get a different sign-in."""
    import argparse

    parser = cli.build_parser()
    subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    run = subs.choices["run"]
    group = {
        option: g.title for g in run._action_groups for action in g._group_actions for option in action.option_strings
    }
    assert group["--gcloud-project"] == "any phase"
    assert group["--vertex-project"] == "any phase"
    # Phase 2 verifies the Anthropic key; phase 3 asks it for the catalogue.
    assert group["--auth-anthropic-key"] == "any phase"
    assert group["--auth-anthropic-key-ref"] == "any phase"
    # And one that really does belong to a single phase, so the check has teeth.
    assert group["--gcloud-region"].startswith("phase 3 (provision)")


def test_a_token_reference_is_a_reference_not_a_value() -> None:
    """--mcp-token-ref names where to read the token; it is never the token."""
    overrides = cli._overrides_from_args(parse("run", "--mcp-token-ref", "env:MY_TOKEN"))
    assert all("env:MY_TOKEN" in o for o in overrides if o.startswith("mcp.discovery"))


def test_phase_one_modifiers_map_onto_install_settings() -> None:
    overrides = cli._overrides_from_args(parse("run", "--force-install", "--break-dependencies", "--downgrade"))
    assert "install.force=true" in overrides
    assert "install.allow_break_system_packages=true" in overrides
    assert "install.downgrade=true" in overrides


def test_component_selection_maps_onto_install_settings() -> None:
    args = parse("run", "--install-only", "claude-cli,gcloud", "--install-skip", "browser")
    overrides = cli._overrides_from_args(args)
    assert "install.only=claude-cli,gcloud" in overrides
    assert "install.skip=browser" in overrides


def test_account_flags_map_onto_the_auth_settings() -> None:
    """Identities, so they carry their value directly -- see test_secure_mode
    for the credential flags, which may not."""
    args = parse(
        "run",
        "--gcloud-user-account",
        "me@example.com",
        "--gcloud-service-account",
        "vertex@p.iam.gserviceaccount.com",
        "--claude-user-account",
        "me@example.com",
    )
    overrides = cli._overrides_from_args(args)
    assert "auth.google_cloud.account=me@example.com" in overrides
    assert "auth.google_cloud.impersonate_service_account=vertex@p.iam.gserviceaccount.com" in overrides
    assert "auth.claude_oauth.account=me@example.com" in overrides


def test_a_key_file_becomes_an_absolute_file_reference(tmp_path) -> None:
    """Bare path in, file: reference out -- and anchored, so it survives a chdir."""
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    overrides = cli._overrides_from_args(parse("run", "--gcloud-service-account-file", str(key)))
    assert f"auth.google_cloud.service_account_key_ref=file:{key}" in overrides


def test_a_relative_key_file_is_anchored(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    overrides = cli._overrides_from_args(parse("run", "--gcloud-service-account-file", "sa.json"))
    assert f"auth.google_cloud.service_account_key_ref=file:{tmp_path / 'sa.json'}" in overrides


def test_a_key_file_path_is_not_a_secret() -> None:
    """--secure governs values, not locations; a path is the safe form already."""
    args = parse("run", "--secure", "--gcloud-service-account-file", "/etc/sa.json")
    assert cli.check_credential_flags(args) is None
    assert cli.check_service_account_flags(args) is None


def test_the_two_service_account_routes_are_mutually_exclusive() -> None:
    """They pick different methods and the key file wins, so the impersonation
    would be configured, reported unsatisfied every run, and never applied."""
    complaint = cli.check_service_account_flags(
        parse(
            "run",
            "--gcloud-service-account",
            "sa@p.iam.gserviceaccount.com",
            "--gcloud-service-account-file",
            "/etc/sa.json",
        )
    )
    assert complaint and "not both" in complaint


def test_either_service_account_route_alone_is_fine() -> None:
    for argv in (
        ("--gcloud-service-account", "sa@p.iam.gserviceaccount.com"),
        ("--gcloud-service-account-file", "/etc/sa.json"),
    ):
        assert cli.check_service_account_flags(parse("run", *argv)) is None


def test_force_auth_maps_onto_the_auth_force_setting() -> None:
    args = parse("run", "--force-auth")
    assert "auth.force=true" in cli._overrides_from_args(args)
    assert "auth.force=true" not in cli._overrides_from_args(parse("run"))


def test_no_reuse_existing_mcp_turns_reuse_off() -> None:
    assert "mcp.discovery.reuse_existing=false" in cli._overrides_from_args(parse("run", "--no-reuse-existing-mcp"))
    assert not any(o.startswith("mcp.discovery.reuse_existing") for o in cli._overrides_from_args(parse("run")))


def test_force_auth_with_reset_asks_for_both_halves() -> None:
    """Clearing a credential and then not signing in again would leave the
    machine worse off than it was found, so the flag sets force too."""
    overrides = cli._overrides_from_args(parse("run", "--force-auth-with-reset"))
    assert "auth.reset=true" in overrides
    assert "auth.force=true" in overrides


def test_plain_force_auth_never_clears_anything() -> None:
    """The destructive half has to be asked for by name. --force-auth adds a
    credential; only --force-auth-with-reset takes one away."""
    assert "auth.reset=true" not in cli._overrides_from_args(parse("run", "--force-auth"))
    assert "auth.reset=true" not in cli._overrides_from_args(parse("run"))


def test_the_gcloud_region_stays_out_of_the_other_cloud() -> None:
    """A flag named after one provider must not quietly configure another.

    It used to be ``--region`` and wrote both, which was defensible while the
    name said nothing about whose region it meant. ``--gcloud-region`` setting
    an AWS region would be a lie, so Bedrock keeps its own key instead.
    """
    overrides = cli._overrides_from_args(parse("run", "--provision-backend", "bedrock", "--gcloud-region", "us-east5"))
    assert "provision.vertex.region=us-east5" in overrides
    assert not [o for o in overrides if o.startswith("provision.bedrock.region=")]


def test_a_tier_pin_reaches_every_backend_that_has_one() -> None:
    """``--default-opus-model`` names a model, not a provider.

    Only the selected backend's alias table is ever read, so writing the others
    is inert -- and writing just one would make the flag silently do nothing
    under a ``--provision-backend`` the author did not think of.
    """
    overrides = cli._overrides_from_args(
        parse("run", "--default-opus-model", "claude-opus-5", "--default-haiku-model", "haiku-x")
    )
    assert "provision.vertex.model_aliases.opus=claude-opus-5" in overrides
    assert "provision.anthropic_api.model_aliases.opus=claude-opus-5" in overrides
    assert "provision.vertex.model_aliases.haiku=haiku-x" in overrides
    # A tier that was not pinned must stay out of the way of discovery.
    assert not [o for o in overrides if ".model_aliases.sonnet=" in o]


def test_use_vertex_is_silent_unless_it_is_given() -> None:
    """Defaulting it to 1 in argparse would beat an env file that said 0.

    "Vertex on by default" is a property of the packaged configuration, not of
    the command line -- the flag only speaks when somebody typed it.
    """
    assert not [o for o in cli._overrides_from_args(parse("run")) if o.startswith("provision.vertex.enabled")]
    assert "provision.vertex.enabled=0" in cli._overrides_from_args(parse("run", "--use-vertex", "0"))
    assert "provision.vertex.enabled=1" in cli._overrides_from_args(parse("run", "--use-vertex", "1"))


def test_the_two_browser_modes_are_mutually_exclusive() -> None:
    """Both would compile to auth.browser_mode and the later one would win, so
    the operator would get a flow they did not choose and could not see."""
    with pytest.raises(SystemExit) as raised:
        parse("run", "--headless", "--browser-loopback-redirect")
    assert raised.value.code == 2


def test_either_browser_mode_alone_maps_onto_the_setting() -> None:
    assert "auth.browser_mode=headless" in cli._overrides_from_args(parse("run", "--headless"))
    assert "auth.browser_mode=browser" in cli._overrides_from_args(parse("run", "--browser-loopback-redirect"))


def test_the_two_force_flags_are_independent() -> None:
    """--force-install reinstalls software, --force-auth re-signs-in; a phase-1
    rebuild has no business stealing a working session."""
    install = cli._overrides_from_args(parse("run", "--force-install"))
    assert "install.force=true" in install and "auth.force=true" not in install
    auth = cli._overrides_from_args(parse("run", "--force-auth"))
    assert "auth.force=true" in auth and "install.force=true" not in auth


def test_secure_has_nothing_to_say_about_an_account() -> None:
    """An account name is public; refusing it would be theatre."""
    args = parse("run", "--secure", "--gcloud-user-account", "me@example.com")
    assert cli.check_credential_flags(args) is None
    assert "auth.google_cloud.account=me@example.com" in cli._overrides_from_args(args)


def test_the_two_failure_policies_are_independent() -> None:
    """--ignore-failure loosens the 'failed' policy, --no-partial-success
    tightens the 'partial' one; neither implies the other."""
    args = parse("run", "--ignore-failure")
    assert args.ignore_failure is True and args.no_partial_success is False
    args = parse("run", "--no-partial-success")
    assert args.no_partial_success is True and args.ignore_failure is False


def test_auto_agree_has_a_short_form() -> None:
    assert parse("run", "--auto-agree").auto_agree is True
    assert parse("run", "-a").auto_agree is True
    assert parse("run").auto_agree is False


def test_unset_flags_produce_no_overrides() -> None:
    assert cli._overrides_from_args(parse("run")) == []


# -- SETTING_FLAGS: one row per numeric shortcut, every row held to the same rules --


SETTING_FLAGS = pytest.mark.parametrize("flag", cli.SETTING_FLAGS, ids=lambda f: f.flag)


def acceptable(flag) -> str:
    """The smallest value the row accepts, spelt as an operator would type it."""
    value = flag.minimum + 1 if flag.exclusive else flag.minimum
    return str(flag.kind(value))


@SETTING_FLAGS
def test_every_setting_flag_writes_a_key_the_program_ships(flag) -> None:
    """A row pointed at a misspelt key would be accepted, echoed back, and read by nothing."""
    from initialize_claude_ai.core.config import load_settings

    assert load_settings().get(flag.key, None) is not None, flag.key


@SETTING_FLAGS
def test_every_setting_flag_requires_a_value(flag, capsys) -> None:
    with pytest.raises(SystemExit) as refused:
        parse("run", flag.flag)
    assert refused.value.code == 2
    assert "expected one argument" in capsys.readouterr().err


@SETTING_FLAGS
def test_every_setting_flag_refuses_a_value_of_the_wrong_type(flag) -> None:
    with pytest.raises(SystemExit):
        parse("run", flag.flag, "not-a-number")


@SETTING_FLAGS
def test_every_setting_flag_is_exactly_set_key_equals_value(flag) -> None:
    value = acceptable(flag)
    assert cli._overrides_from_args(parse("run", flag.flag, value)) == [f"{flag.key}={value}"]


@SETTING_FLAGS
def test_every_setting_flag_left_out_leaves_no_trace(flag) -> None:
    assert getattr(parse("run"), flag.dest) is None
    assert not [o for o in cli._overrides_from_args(parse("run")) if o.startswith(flag.key + "=")]


@SETTING_FLAGS
def test_every_setting_flag_refuses_a_value_below_its_bound(flag) -> None:
    """Including the bound itself when it is exclusive, and 0 -- which truthiness would drop."""
    from initialize_claude_ai.core.errors import ConfigError

    below = flag.minimum if flag.exclusive else flag.minimum - 1
    with pytest.raises(ConfigError) as refused:
        cli._overrides_from_args(parse("run", flag.flag, str(flag.kind(below))))
    assert flag.flag in refused.value.message
    assert refused.value.remediation


def test_setting_flags_are_not_declared_twice() -> None:
    flags = [f.flag for f in cli.SETTING_FLAGS]
    keys = [f.key for f in cli.SETTING_FLAGS]
    assert len(flags) == len(set(flags)) and len(keys) == len(set(keys))


def test_agent_max_attempts_leaves_the_shipped_default_alone() -> None:
    """Without the flag the setting decides, exactly as before the flag existed."""
    from initialize_claude_ai.core.config import load_settings

    def attempts(*argv: str) -> int:
        args = parse("run", *argv)
        overrides = [*getattr(args, "overrides", []), *cli._overrides_from_args(args)]
        return load_settings(overrides=overrides).int_("mcp.agent.max_attempts", -1)

    shipped = attempts()
    assert attempts("--agent-max-attempts", "5") == 5
    assert attempts("--set", "mcp.agent.max_attempts=7") == 7
    assert attempts("--set", "mcp.agent.max_attempts=7", "--agent-max-attempts", "2") == 2
    assert attempts() == shipped


def test_an_agent_only_flag_warns_when_the_agent_is_off() -> None:
    from initialize_claude_ai.core.config import load_settings

    args = parse("run", "--no-agent", "--agent-max-attempts", "2")
    warnings = cli.inert_setting_flags(args, load_settings(overrides=cli._overrides_from_args(args)))
    assert len(warnings) == 1 and "--agent-max-attempts" in warnings[0]

    args = parse("run", "--agent-max-attempts", "2")
    assert cli.inert_setting_flags(args, load_settings(overrides=cli._overrides_from_args(args))) == []

    # --timeout-scale is read by both modes, so --no-agent is no reason to mention it.
    args = parse("run", "--no-agent", "--timeout-scale", "2")
    assert cli.inert_setting_flags(args, load_settings(overrides=cli._overrides_from_args(args))) == []


def test_all_four_phases_are_registered() -> None:
    numbered = sorted((phase.number, phase.slug) for phase in all_phases())
    assert numbered == [(1, "install"), (2, "auth"), (3, "provision"), (4, "mcp")]


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1,3", ["install", "provision"]),
        ("2-4", ["auth", "provision", "mcp"]),
        ("all", ["install", "auth", "provision", "mcp"]),
        ("mcp", ["mcp"]),
        ("4,4", ["mcp"]),
    ],
)
def test_phase_selection_accepts_numbers_ranges_and_names(spec: str, expected) -> None:
    assert [phase.slug for phase in select(cli._split(spec))] == expected


def test_omitting_phases_runs_every_registered_phase(settings) -> None:
    """No --phases means the whole sequence, however many phases exist."""
    tokens = settings.str_list("run.default_phases")
    assert select(tokens) == all_phases()


def test_phases_command_lists_them_as_json(capsys: pytest.CaptureFixture, sandbox) -> None:
    assert cli.main(["phases", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [p["slug"] for p in payload["phases"]] == ["install", "auth", "provision", "mcp"]


def test_config_command_prints_one_key(capsys: pytest.CaptureFixture, sandbox) -> None:
    assert cli.main(["config", "meta.name"]) == 0
    assert "initialize-claude-ai" in capsys.readouterr().out


def test_config_command_reports_an_unknown_key(capsys: pytest.CaptureFixture, sandbox) -> None:
    assert cli.main(["config", "no.such.key"]) == 1
    assert "not set" in capsys.readouterr().err


def test_palette_command_shows_every_scheme_and_how_to_pick_one(capsys: pytest.CaptureFixture, sandbox) -> None:
    """A swatch nobody can act on is decoration, so the setting is printed too."""
    assert cli.main(["palette"]) == 0
    out = capsys.readouterr().out
    for name in PALETTES:
        assert f"console.palette={name}" in out
    assert DEFAULT_PALETTE in out
    assert ENV_PREFIX + "CONSOLE_PALETTE" in out


def test_palette_command_names_the_schemes_as_json(capsys: pytest.CaptureFixture, sandbox) -> None:
    assert cli.main(["palette", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default"] == DEFAULT_PALETTE
    assert sorted(payload["palettes"]) == sorted(PALETTES)


# -- what the run says about itself before it starts --------------------------


def invocation(ctx, *argv: str):
    """The facts block, and the screen it produces, for one command line."""
    import io

    from initialize_claude_ai.core.logging_setup import Console

    args = parse(*argv)
    args.argv = list(argv)
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    facts = cli._invocation_facts(ctx, args, select(["4"]))
    cli._show_invocation(ctx, facts)
    return facts, ctx.console.stream.getvalue()


def test_the_run_opens_by_saying_what_it_was_told(ctx) -> None:
    """The first question of a run that behaved oddly is what it was run with."""
    facts, screen = invocation(ctx, "run", "--dry-run", "--phases", "4")
    assert facts["command_line"].startswith(cli.PROGRAM + " run ")
    assert "--dry-run" in facts["options"]
    assert facts["options"]["--phases"] == "4"
    assert "HOW THIS RUN IS CONFIGURED" in screen
    assert "--dry-run" in screen
    assert "4:mcp" in facts["phases"]


def test_an_option_nobody_gave_is_not_a_fact_about_this_run(ctx) -> None:
    facts, _screen = invocation(ctx, "run", "--phases", "4")
    assert "--gcloud-project" not in facts["options"]
    assert facts["options_given"] < facts["options_available"]


def test_the_setting_a_variable_decided_is_named_with_the_variable(ctx) -> None:
    ctx.settings.environ["CLOUD_ML_REGION"] = "us-east5"
    ctx.settings.origins["provision.vertex.region"] = "packaged:30-provision.toml"
    _facts, screen = invocation(ctx, "run")
    assert "provision.vertex.region" in screen
    assert "from CLOUD_ML_REGION" in screen


def test_a_credential_in_the_environment_is_not_printed(ctx) -> None:
    """The block is read over shoulders and pasted into tickets like any other."""
    ctx.settings.environ["ANTHROPIC_API_KEY"] = "sk-ant-not-a-real-key"
    facts, screen = invocation(ctx, "run")
    assert facts["environment"]["ANTHROPIC_API_KEY"] != "sk-ant-not-a-real-key"
    assert "sk-ant-not-a-real-key" not in screen


def test_a_variable_this_program_never_reads_is_none_of_its_business(ctx) -> None:
    ctx.settings.environ["SOME_UNRELATED_TOOL_TOKEN"] = "whatever"
    facts, _screen = invocation(ctx, "run")
    assert "SOME_UNRELATED_TOOL_TOKEN" not in facts["environment"]


def test_a_long_section_is_elided_rather_than_filling_the_screen(ctx) -> None:
    ctx.settings.set("run.invocation.max_rows", 2)
    for index in range(9):
        ctx.settings.environ[f"ANTHROPIC_FAKE_{index}"] = str(index)
    facts, screen = invocation(ctx, "run")
    assert f"and {len(facts['environment']) - 2} more" in screen


def test_the_block_can_be_silenced_without_losing_the_facts(ctx) -> None:
    """Off is off the human output -- and the report keeps them regardless. A
    report that cannot say what it was run with is worth less months later than
    a quieter terminal is worth now."""
    ctx.settings.set("run.invocation.show", False)
    facts, screen = invocation(ctx, "run", "--dry-run")
    assert screen == ""
    assert "--dry-run" in facts["options"]


def test_every_option_the_block_names_is_spelled_as_the_flag(ctx) -> None:
    """dest names are an implementation detail; the reader typed the flag."""
    facts, _screen = invocation(ctx, "run", "--dry-run", "--no-log-file")
    assert all(name.startswith("-") for name in facts["options"])


def test_a_value_too_long_for_its_column_gets_a_line_of_its_own(ctx) -> None:
    """A path read character by character must not be broken across two lines."""
    long_path = "/root/.local/share/claude/versions/2.1.270/some/deep/place"
    ctx.settings.environ["CLAUDE_CODE_EXECPATH"] = long_path
    _facts, screen = invocation(ctx, "run")
    assert any(line.strip("| ") == long_path for line in screen.splitlines())


def test_a_secret_written_into_a_config_file_is_still_masked(ctx, sandbox) -> None:
    """A value being *configured* rather than exported makes it no less a secret."""
    ctx.settings.set("auth.anthropic.api_key", "sk-ant-not-a-real-key")
    ctx.settings.origins["auth.anthropic.api_key"] = f"file:{sandbox}/custom.toml"
    facts, screen = invocation(ctx, "run")
    shown = {item["setting"]: item["value"] for item in facts["settings"]}
    assert shown["auth.anthropic.api_key"] != "sk-ant-not-a-real-key"
    assert "sk-ant-not-a-real-key" not in screen


def test_the_name_of_a_setting_is_not_mistaken_for_a_secret(ctx, sandbox) -> None:
    """Masking goes by key name, and "key" is one of those names -- so the dotted
    name travels under a field that rule does not touch."""
    from initialize_claude_ai.core.report import RunReport

    ctx.settings.set("auth.anthropic.api_key", "sk-ant-not-a-real-key")
    ctx.settings.origins["auth.anthropic.api_key"] = f"file:{sandbox}/custom.toml"
    facts, _screen = invocation(ctx, "run")
    report = RunReport()
    report.invocation = facts
    payload = report.to_dict()
    names = [item["setting"] for item in payload["invocation"]["settings"]]
    assert "auth.anthropic.api_key" in names
