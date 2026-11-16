"""Configuration layering, interpolation and coercion."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from initialize_claude_ai.core.config import Settings, coerce, load_settings, parse_env_file


def test_packaged_defaults_load(settings: Settings) -> None:
    assert settings.str_("meta.name") == "initialize-claude-ai"
    assert settings.bool_("provision.enabled") is True
    assert any(s.startswith("packaged:") for s in settings.sources)


def test_file_layer_overrides_packaged(sandbox: Path) -> None:
    override = sandbox / "custom.toml"
    override.write_text("[provision]\nstrict_model_check = true\n", encoding="utf-8")
    settings = load_settings(config_files=[override], environ=dict(os.environ))
    assert settings.bool_("provision.strict_model_check") is True


def test_env_override_beats_file(sandbox: Path) -> None:
    override = sandbox / "custom.toml"
    override.write_text("[run]\ncommand_timeout_seconds = 11\n", encoding="utf-8")
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_RUN__COMMAND_TIMEOUT_SECONDS="22")
    settings = load_settings(config_files=[override], environ=environ)
    assert settings.int_("run.command_timeout_seconds") == 22


def test_set_override_beats_env(sandbox: Path) -> None:
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_RUN__COMMAND_TIMEOUT_SECONDS="22")
    settings = load_settings(overrides=["run.command_timeout_seconds=33"], environ=environ)
    assert settings.int_("run.command_timeout_seconds") == 33


def test_env_override_keeps_the_existing_type(sandbox: Path) -> None:
    """'false' must land as a bool, not as the truthy string 'false'."""
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_PROVISION__DISCOVER_MODELS="false")
    settings = load_settings(environ=environ)
    assert settings.bool_("provision.discover_models") is False


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("false", False), ("7", 7), ("1.5", 1.5), ("a,b", "a,b"), ('["a"]', ["a"])],
)
def test_coerce_without_a_model_value(raw: str, expected: object) -> None:
    """With nothing to imitate, only unambiguous literals are converted."""
    assert coerce(raw) == expected


@pytest.mark.parametrize(
    "raw,like,expected",
    [
        ("yes", False, True),
        ("off", True, False),
        ("a, b", [], ["a", "b"]),
        ('["a"]', [], ["a"]),
        ("12", 0, 12),
        ("oops", 0, "oops"),
    ],
)
def test_coerce_follows_the_replaced_value(raw: str, like: object, expected: object) -> None:
    assert coerce(raw, like) == expected


def test_interpolation_reads_the_environment(sandbox: Path) -> None:
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_META__DESCRIPTION="${HOME}/x")
    settings = load_settings(environ=environ)
    assert settings.str_("meta.description") == f"{os.environ['HOME']}/x"


def test_interpolation_defaults_may_nest(sandbox: Path) -> None:
    """``${A:-${B:-c}/d}`` must resolve innermost-first, not literally."""
    environ = {k: v for k, v in os.environ.items() if k not in ("OUTER", "INNER")}
    settings = Settings(data={"k": "${OUTER:-${INNER:-fallback}/tail}"}, environ=environ)
    assert settings.str_("k") == "fallback/tail"


def test_cfg_reference_resolves_another_key(sandbox: Path) -> None:
    settings = Settings(data={"a": {"b": "left"}, "c": "${cfg:a.b}-right"}, environ={})
    assert settings.str_("c") == "left-right"


def test_state_dir_is_a_real_path(settings: Settings) -> None:
    """A regression guard: unresolved '${...}' once became a directory name."""
    state_dir = settings.str_("paths.state_dir")
    assert "${" not in state_dir
    assert Path(state_dir).is_absolute()


def test_parse_env_file(sandbox: Path) -> None:
    path = sandbox / ".env"
    path.write_text(
        "# comment\nexport TOKEN_REF=env:MY_TOKEN\nQUOTED='single'\nDQUOTED=\"double\"\nEMPTY=\n\n",
        encoding="utf-8",
    )
    assert parse_env_file(path) == {
        "TOKEN_REF": "env:MY_TOKEN",
        "QUOTED": "single",
        "DQUOTED": "double",
        "EMPTY": "",
    }


#: The plain UPPER_CASE names an operator is most likely to already have in a
#: shared env file -- exported by a colleague's shell, a CI job or a previous
#: install -- paired with the setting each one has to reach. No
#: INITIALIZE_CLAUDE_AI_ prefix and no flag: an env file alone must be able to
#: drive the whole run, because that is the file people actually keep.
ENV_FILE_SETTINGS = [
    ("GOOGLE_CLOUD_USER_ACCOUNT", "me@example.com", "auth.google_cloud.account"),
    ("GOOGLE_CLOUD_PROJECT", "env-file-project", "auth.google_cloud.project"),
    ("ANTHROPIC_VERTEX_PROJECT_ID", "env-file-project", "provision.vertex.project"),
    ("GOOGLE_CLOUD_LOCATION", "us-east5", "provision.vertex.region"),
    ("CLOUD_ML_REGION", "europe-west4", "provision.vertex.region"),
    ("CLAUDE_CODE_USE_VERTEX", "0", "provision.vertex.enabled"),
    ("DEFAULT_MODEL", "claude-opus-5", "provision.settings.model"),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "pin-opus", "provision.vertex.model_aliases.opus"),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "pin-sonnet", "provision.vertex.model_aliases.sonnet"),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "pin-haiku", "provision.vertex.model_aliases.haiku"),
    ("AWS_REGION", "us-east-1", "provision.bedrock.region"),
]


@pytest.mark.parametrize(("name", "value", "dotted"), ENV_FILE_SETTINGS)
def test_an_env_file_alone_can_supply_a_setting(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, dotted: str
) -> None:
    """The argument that was never passed is looked for here, and found.

    No flag carries a default in the parser precisely so that this layer gets a
    turn: an omitted flag is silent rather than assertive, and the ``${VAR}``
    chains in the packaged defaults read the file's variables by their own
    ordinary names.
    """
    # The real environment wins over the file, so it must not be holding an
    # answer of its own -- and on a machine that has run phase 3, it is.
    for other, _value, _dotted in ENV_FILE_SETTINGS:
        monkeypatch.delenv(other, raising=False)
    path = sandbox / "global.env"
    path.write_text(f"{name}={value}\n", encoding="utf-8")

    loaded = load_settings(environ=dict(os.environ), env_files=[path])
    assert loaded.str_(dotted) == value


def test_a_real_environment_variable_still_beats_the_env_file(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is a floor, not a ceiling: an exported value is more deliberate."""
    monkeypatch.setenv("CLOUD_ML_REGION", "from-the-shell")
    path = sandbox / "global.env"
    path.write_text("CLOUD_ML_REGION=from-the-file\n", encoding="utf-8")
    loaded = load_settings(environ=dict(os.environ), env_files=[path])
    assert loaded.str_("provision.vertex.region") == "from-the-shell"


def test_every_source_named_in_the_table_is_one_the_defaults_actually_read(
    settings: Settings,
) -> None:
    """``[sources]`` is quoted verbatim in failure messages, so it has to be true.

    A variable listed there but expanded nowhere would send somebody off to set
    something that cannot possibly work -- worse than saying nothing at all.
    """
    from initialize_claude_ai.core import config as config_mod

    packaged = "\n".join(path.read_text(encoding="utf-8") for path in sorted(config_mod._DATA_DIR.glob("*.toml")))
    unread = [
        f"{dotted}: {name}"
        for dotted, entry in settings.dict_("sources").items()
        for name in entry.get("env", [])
        if f"${{{name}" not in packaged
    ]
    assert unread == []


def test_every_required_coordinate_can_be_told_how_to_supply_it(settings: Settings) -> None:
    """A value that stops the run must come with somewhere to put it.

    ``provision.required`` decides what is fatal and ``[sources]`` supplies the
    remediation, and they are separate tables. A backend field listed in the
    first but missing from the second would fail the run with nothing to offer
    but "set it directly", which is the least useful of the answers.
    """
    sources = settings.dict_("sources")
    orphaned = [
        f"provision.{backend}.{field}"
        for backend, fields in settings.dict_("provision.required").items()
        for field in fields
        if f"provision.{backend}.{field}" not in sources
    ]
    assert orphaned == []


def test_the_suite_knows_which_host_variables_to_ignore() -> None:
    """Guard on the isolation itself, which is silent when it stops working.

    ``conftest.host_variables`` is derived from the packaged defaults, so a
    typo in the glob or a move of the data directory would return an empty set
    and every test would quietly start reading the developer's environment
    again -- passing here, failing on somebody else's machine.
    """
    from conftest import host_variables

    names = host_variables()
    assert {name for name, _v, _d in ENV_FILE_SETTINGS} <= names


# ---------------------------------------------------------------------------
# The echo layer: our own last answer, handed back as "the environment".
# ---------------------------------------------------------------------------


def echoed(sandbox: Path, monkeypatch: pytest.MonkeyPatch, **block: str) -> None:
    """Write a Claude settings file and export its env block, as Claude Code does."""
    path = sandbox / "home" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": block, "model": "opus"}), encoding="utf-8")
    for name, value in block.items():
        monkeypatch.setenv(name, value)


def test_the_echo_of_our_own_answer_loses_to_the_configured_default(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop this breaks: a region written once would otherwise be permanent."""
    echoed(sandbox, monkeypatch, CLOUD_ML_REGION="us")
    loaded = load_settings(environ=dict(os.environ))
    assert loaded.str_("provision.vertex.region") == "global"
    assert loaded.demoted == {"CLOUD_ML_REGION": "us"}
    assert "echo:CLOUD_ML_REGION" in loaded.sources


def test_a_variable_that_disagrees_with_the_file_is_somebody_elses_answer(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an exact match can be an echo; anything else was set on purpose."""
    echoed(sandbox, monkeypatch, CLOUD_ML_REGION="us")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    loaded = load_settings(environ=dict(os.environ))
    assert loaded.str_("provision.vertex.region") == "us-east5"
    assert loaded.demoted == {}


def test_an_env_file_beats_the_echo(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason the demotion happens before the env files are read at all."""
    echoed(sandbox, monkeypatch, CLOUD_ML_REGION="us")
    path = sandbox / "global.env"
    path.write_text("CLOUD_ML_REGION=europe-west4\n", encoding="utf-8")
    loaded = load_settings(environ=dict(os.environ), env_files=[path])
    assert loaded.str_("provision.vertex.region") == "europe-west4"


def test_the_demotion_can_be_switched_off(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And the switch obeys the same precedence as everything else: --set wins.

    Which means it has to be read before the layer it governs -- the point of
    the early, subtree-only override pass in ``load_settings``.
    """
    echoed(sandbox, monkeypatch, CLOUD_ML_REGION="us")
    loaded = load_settings(environ=dict(os.environ), overrides=["environment.demote_echo=false"])
    assert loaded.str_("provision.vertex.region") == "us"
    assert loaded.demoted == {}


def test_the_switch_is_also_reachable_from_the_environment(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    echoed(sandbox, monkeypatch, CLOUD_ML_REGION="us")
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_ENVIRONMENT__DEMOTE_ECHO", "false")
    loaded = load_settings(environ=dict(os.environ))
    assert loaded.str_("provision.vertex.region") == "us"


def test_a_credential_in_the_settings_file_is_never_demoted(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Demoting it would not correct a stale answer; it would lose the only one."""
    echoed(sandbox, monkeypatch, ANTHROPIC_API_KEY="sk-ant-not-a-real-key", CLOUD_ML_REGION="us")
    loaded = load_settings(environ=dict(os.environ))
    assert "ANTHROPIC_API_KEY" not in loaded.demoted
    assert loaded.environ["ANTHROPIC_API_KEY"] == "sk-ant-not-a-real-key"
    assert "CLOUD_ML_REGION" in loaded.demoted


def test_a_variable_this_program_never_writes_is_left_alone(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's own addition to the env block is not our echo to demote."""
    echoed(sandbox, monkeypatch, SOME_TOOL_HOME="/opt/tool")
    loaded = load_settings(environ=dict(os.environ))
    assert loaded.demoted == {}
    assert loaded.environ["SOME_TOOL_HOME"] == "/opt/tool"


def test_the_demoted_values_are_kept_for_a_setting_with_nothing_else(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last is still a source -- and a section view has to be able to see it."""
    echoed(sandbox, monkeypatch, ANTHROPIC_VERTEX_PROJECT_ID="old-project")
    loaded = load_settings(environ=dict(os.environ))
    assert loaded.str_("provision.vertex.project") == ""
    assert loaded.section("provision.vertex").demoted["ANTHROPIC_VERTEX_PROJECT_ID"] == ("old-project")


def test_section_is_a_view_with_relative_keys(settings: Settings) -> None:
    vertex = settings.section("provision.vertex")
    assert vertex.str_("publisher") == "anthropic"


def test_env_var_for_builds_the_documented_name() -> None:
    assert Settings.env_var_for("mcp.discovery.timeout") == "INITIALIZE_CLAUDE_AI_MCP__DISCOVERY__TIMEOUT"


# ---------------------------------------------------------------------------
# Where each value came from: what the run prints about itself before it starts.
# ---------------------------------------------------------------------------


def test_a_packaged_default_is_not_something_the_host_decided(settings: Settings) -> None:
    keys = {key for key, _value, _origin in settings.overrides()}
    assert "meta.name" not in keys
    assert settings.origins["meta.name"].startswith("packaged:")


def test_the_layer_that_had_the_last_word_is_the_one_reported(sandbox: Path) -> None:
    override = sandbox / "custom.toml"
    override.write_text("[run]\ncommand_timeout_seconds = 11\n", encoding="utf-8")
    loaded = load_settings(config_files=[override], environ=dict(os.environ))
    assert loaded.origins["run.command_timeout_seconds"] == f"file:{override}"

    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_RUN__COMMAND_TIMEOUT_SECONDS="22")
    loaded = load_settings(config_files=[override], environ=environ)
    assert loaded.origins["run.command_timeout_seconds"] == ("env:INITIALIZE_CLAUDE_AI_RUN__COMMAND_TIMEOUT_SECONDS")

    loaded = load_settings(
        config_files=[override],
        overrides=["run.command_timeout_seconds=33"],
        environ=environ,
    )
    assert loaded.origins["run.command_timeout_seconds"] == "cli"
    assert ("run.command_timeout_seconds", 33, "cli") in loaded.overrides()


def test_a_default_reading_a_variable_that_is_set_is_credited_to_it(sandbox: Path) -> None:
    """``region = "${CLOUD_ML_REGION:-global}"`` with the variable set is not a default.

    No layer replaced the line, so the origin is still packaged -- and the value
    on the screen all the same came from the environment. A run that printed
    only literal overrides would leave the commonest surprise of all unexplained.
    """
    environ = dict(os.environ, CLOUD_ML_REGION="us-east5")
    loaded = load_settings(environ=environ)
    found = {key: origin for key, _value, origin in loaded.overrides()}
    assert found["provision.vertex.region"] == "env:CLOUD_ML_REGION"
    assert loaded.str_("provision.vertex.region") == "us-east5"


def test_a_default_reading_a_variable_that_is_unset_is_still_a_default(
    sandbox: Path,
) -> None:
    loaded = load_settings(environ=dict(os.environ))
    keys = {key for key, _value, _origin in loaded.overrides()}
    assert "provision.vertex.region" not in keys


def test_the_noisy_variables_of_the_machine_can_be_left_out(sandbox: Path) -> None:
    """HOME is read by a dozen path defaults; naming it a dozen times explains nothing."""
    loaded = load_settings(environ=dict(os.environ, HOME=str(sandbox)))
    named = {key for key, _v, origin in loaded.overrides() if origin == "env:HOME"}
    assert named, "HOME is expected to drive at least one packaged path default"
    assert not {key for key, _v, origin in loaded.overrides(ignore_env=["HOME"]) if origin == "env:HOME"}


def test_a_variable_that_misses_the_separator_is_not_reported_as_a_setting(
    sandbox: Path,
) -> None:
    """``INITIALIZE_CLAUDE_AI_MCP_AGENTIC`` is read by interpolation, not by this.

    One underscore is not the ``__`` separator, so the generic mechanism also
    lands it as a top-level ``mcp_agentic`` that nothing reads. Listing that as a
    setting of this program would send a reader looking for a key that does not
    exist.
    """
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_MCP_AGENTIC="false")
    loaded = load_settings(environ=environ)
    assert "mcp_agentic" not in loaded.origins
    assert "mcp_agentic" not in {key for key, _v, _o in loaded.overrides()}
    # Still applied, and still in the audit trail -- it did happen.
    assert loaded.get("mcp_agentic") is False
    assert "env:INITIALIZE_CLAUDE_AI_MCP_AGENTIC->mcp_agentic" in loaded.sources
    # And the setting it was meant for is reported, under the name that exists.
    found = {key: origin for key, _value, origin in loaded.overrides()}
    assert found["mcp.agentic"] == "env:INITIALIZE_CLAUDE_AI_MCP_AGENTIC"


def test_a_key_no_packaged_file_declares_can_still_be_set_outright(sandbox: Path) -> None:
    """Spelled with the separator, the intent is unambiguous and is honoured."""
    environ = dict(os.environ, INITIALIZE_CLAUDE_AI_CUSTOM__THING="7")
    loaded = load_settings(environ=environ)
    assert loaded.int_("custom.thing") == 7
    assert loaded.origins["custom.thing"] == "env:INITIALIZE_CLAUDE_AI_CUSTOM__THING"


def test_flatten_can_show_the_value_as_written(settings: Settings) -> None:
    raw = settings.flatten(interpolate=False)
    assert "${" in str(raw["provision.vertex.region"])
    assert "${" not in str(settings.flatten()["provision.vertex.region"])
