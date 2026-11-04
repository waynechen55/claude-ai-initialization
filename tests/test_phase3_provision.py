"""Phase 3: which backend, which project it bills, and which models it writes.

Three questions whose answers have to be defensible rather than merely
plausible. ``--use-vertex`` decides whether Vertex is even in the running, the
project id decides whose Google Cloud account the tokens are charged to, and
the tier ids decide what Claude Code will try to call. None of them may be
invented, and where an answer had to be guessed the run has to say so.
"""

from __future__ import annotations

import json
import os

import pytest

from initialize_claude_ai.authenticate.base import AuthOutcome
from initialize_claude_ai.core.config import Settings, load_settings
from initialize_claude_ai.core.errors import MissingSettingError
from initialize_claude_ai.core.report import PhaseResult, Safety, Status
from initialize_claude_ai.phases.phase3_provision import ProvisionPhase
from initialize_claude_ai.provision.models import ModelCatalog, ModelInfo
from initialize_claude_ai.provision.settings_writer import settings_path


def phase(ctx, monkeypatch, **provision) -> ProvisionPhase:
    """A phase on a bare environment, with ``[provision]`` overlaid.

    ``vertex__project=...`` writes ``provision.vertex.project``.

    The bare environment is not arranged here: ``conftest`` unsets every
    variable the packaged defaults interpolate, for the whole suite. It has to
    be the whole suite, because the subject of these tests is what this program
    resolves when it is told nothing, and on a machine that has already run
    phase 3 the environment is full of answers -- ``ANTHROPIC_DEFAULT_OPUS_MODEL``
    is exactly what a successful run exports.
    """
    data = dict(load_settings(environ=dict(os.environ)).data)
    section = {**data.get("provision", {})}
    for dotted, value in provision.items():
        head, _, tail = dotted.partition("__")
        if tail:
            section[head] = {**section.get(head, {}), tail: value}
        else:
            section[head] = value
    data["provision"] = section
    ctx.settings = Settings(data=data, environ=dict(os.environ))
    built = ProvisionPhase(ctx)
    built.settings = ctx.settings
    # gcloud is the last resort for the project, and asking the developer's own
    # machine would make the precedence tests depend on who ran them.
    monkeypatch.setattr(ProvisionPhase, "_gcloud_value", lambda self, gcloud, key: "")
    return built


def result() -> PhaseResult:
    return PhaseResult(phase="provision", title="Provision")


def step(res: PhaseResult, name: str):
    return next(s for s in res.steps if s.name == name)


def signed_in_as(ctx, project: str) -> None:
    """What phase 2 leaves behind after a Google Cloud sign-in."""
    outcome = AuthOutcome(backend="google-cloud", status=Status.OK, verified=True)
    outcome.data["project"] = project
    ctx.state["auth_outcomes"] = {"google-cloud": outcome}


# -- --use-vertex --------------------------------------------------------------


def test_vertex_is_in_the_running_by_default(ctx, monkeypatch) -> None:
    """ "Default to 1" lives in the packaged configuration, not in argparse."""
    assert phase(ctx, monkeypatch)._backend_enabled("vertex") is True


@pytest.mark.parametrize("off", ["0", 0, False, "false"])
def test_use_vertex_zero_takes_vertex_out_of_the_automatic_walk(ctx, monkeypatch, off) -> None:
    """``auto`` must fall through rather than pick a backend that was switched off.

    The flag compiles to a string override, so the setting has to read as false
    whether it arrived as ``"0"`` from the command line or ``false`` from TOML.
    """
    built = phase(ctx, monkeypatch, vertex__enabled=off, backend="auto")
    res = result()
    assert built._select_backend(res, built.settings.section("provision")) != "vertex"


def test_asking_for_a_switched_off_vertex_is_refused_by_name(ctx, monkeypatch) -> None:
    """Silently provisioning something else would be the worse failure.

    The remediation names the flag, not the config key the flag compiles to,
    because the flag is what the operator typed to get here.
    """
    built = phase(ctx, monkeypatch, vertex__enabled="0", backend="vertex")
    res = result()
    assert built._select_backend(res, built.settings.section("provision")) == ""
    assert step(res, "backend").status == Status.FAILED
    assert any("--use-vertex 1" in line for line in step(res, "backend").remediation)


# -- --gcloud-project ----------------------------------------------------------


def test_a_configured_project_wins(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch, vertex__project="from-the-flag")
    signed_in_as(ctx, "from-phase-2")
    cfg = built.settings.section("provision.vertex")
    assert built._vertex_project(cfg, "/gcloud") == ("from-the-flag", "configured")


def test_the_project_falls_back_to_what_phase_2_signed_in_against(ctx, monkeypatch) -> None:
    """The credential that was verified belongs to a project; use that one.

    Asking gcloud again would be a second opinion on a question phase 2 has
    already answered -- and the sign-in may itself have changed which project
    is active.
    """
    built = phase(ctx, monkeypatch, vertex__project="")
    signed_in_as(ctx, "from-phase-2")
    project, source = built._vertex_project(built.settings.section("provision.vertex"), "/gcloud")
    assert project == "from-phase-2"
    assert "phase 2" in source


def test_no_project_is_invented(ctx, monkeypatch) -> None:
    """Nothing plausible is substituted: a wrong project bills somebody else.

    It then fails with a permission error naming a project the operator never
    chose, which is a far harder thing to debug than an empty one.
    """
    built = phase(ctx, monkeypatch, vertex__project="")
    cfg = built.settings.section("provision.vertex")
    assert built._vertex_project(cfg, "") == ("", "")


def test_the_project_falls_back_last_to_the_file_being_rewritten(ctx, monkeypatch) -> None:
    """Our own previous answer is the weakest source -- but it is still a source.

    Unlike the region, a project id has no default worth inventing, so keeping
    the one already in settings.json beats failing a host with nothing else to
    say. ``demoted`` is where that value lands once it has been taken back out
    of the environment.
    """
    built = phase(ctx, monkeypatch, vertex__project="")
    built.settings.demoted = {"ANTHROPIC_VERTEX_PROJECT_ID": "old-project"}
    project, source = built._vertex_project(built.settings.section("provision.vertex"), "")
    assert project == "old-project"
    assert "settings.json" in source


def test_anything_else_at_all_beats_the_file_being_rewritten(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch, vertex__project="")
    built.settings.demoted = {"ANTHROPIC_VERTEX_PROJECT_ID": "old-project"}
    signed_in_as(ctx, "from-phase-2")
    project, _source = built._vertex_project(built.settings.section("provision.vertex"), "")
    assert project == "from-phase-2"


def test_the_report_says_where_the_project_came_from(ctx, monkeypatch) -> None:
    """An id with no provenance is indistinguishable from a guess."""
    built = phase(ctx, monkeypatch, vertex__project="")
    signed_in_as(ctx, "from-phase-2")
    res = result()
    built._vertex_profile(res, built.settings.section("provision.vertex"))
    detail = step(res, "project").detail
    assert "from-phase-2" in detail and "phase 2" in detail


# -- --gcloud-region -----------------------------------------------------------


def test_the_region_defaults_to_global(ctx, monkeypatch) -> None:
    """The one location reachable without knowing where the quota lives."""
    cfg = phase(ctx, monkeypatch).settings.section("provision.vertex")
    assert cfg.str_("region", "global") == "global"
    # ...and 'global' has to be one of the locations served by the global host,
    # or the default would address a hostname that does not resolve.
    assert "global" in cfg.str_list("global_regions")


def test_the_report_names_the_variable_the_region_came_from(ctx, monkeypatch) -> None:
    """'Why that region' should not need an archaeology session.

    The answer is derived from the ``[sources]`` table, the same one the
    remediation quotes, so it cannot drift from the ``${VAR}`` chain that did
    the reading.
    """
    built = phase(ctx, monkeypatch)
    built.settings.environ["GOOGLE_CLOUD_LOCATION"] = "us-east5"
    res = result()
    built._vertex_profile(res, built.settings.section("provision.vertex"))
    assert step(res, "region").detail == "us-east5 (GOOGLE_CLOUD_LOCATION)"


def test_a_region_nothing_asked_for_is_reported_as_the_default(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch)
    res = result()
    built._vertex_profile(res, built.settings.section("provision.vertex"))
    assert step(res, "region").detail == "global (default)"


def test_the_old_settings_file_cannot_supply_the_region(ctx, monkeypatch) -> None:
    """The loop, at the level phase 3 sees it: the default wins, and says so.

    A region echoed back at us is not an answer -- there is a configured
    default for it, and the default outranks our own last output.
    """
    built = phase(ctx, monkeypatch)
    built.settings.demoted = {"CLOUD_ML_REGION": "us"}
    res = result()
    built._vertex_profile(res, built.settings.section("provision.vertex"))
    assert step(res, "region").detail == "global (default)"


# -- --default-model and the tier pins -----------------------------------------

TIERS = ("opus", "sonnet", "haiku")


def catalogue(*ids: str) -> ModelCatalog:
    """A backend that answered, offering one dated model per named tier."""
    return ModelCatalog(
        backend="vertex",
        ok=True,
        models=[ModelInfo(id=model_id, version="20250101") for model_id in ids],
    )


def offline() -> ModelCatalog:
    """A backend that could not be asked: the case the fallbacks exist for."""
    return ModelCatalog(backend="vertex", ok=False, detail="connection refused")


def select(built: ProvisionPhase, catalog: ModelCatalog):
    return built._select_models(
        result(),
        built.settings.section("provision"),
        built.settings.section("provision.vertex"),
        catalog,
        TIERS,
    )


def test_the_catalogue_beats_the_fallback_ids(ctx, monkeypatch) -> None:
    """The fallbacks are a guess; the catalogue knows. It must win.

    Shipping literal ids as ordinary defaults would have switched discovery off
    for every tier -- and they would be wrong on any backend that spells model
    ids differently, which Vertex does.
    """
    built = phase(ctx, monkeypatch)
    chosen = select(built, catalogue("claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5"))
    assert chosen["tiers"]["opus"] == "claude-opus-4-1@20250101"
    assert chosen["guessed"] == []
    assert sorted(chosen["inferred"]) == sorted(TIERS)


def test_the_fallbacks_fill_in_when_there_was_no_catalogue_to_ask(ctx, monkeypatch) -> None:
    """An unset tier leaves Claude Code with nothing to switch to, so guess.

    But say that it is a guess: the step drops to PARTIAL and names the tiers.
    """
    built = phase(ctx, monkeypatch)
    res = result()
    chosen = built._select_models(
        res,
        built.settings.section("provision"),
        built.settings.section("provision.vertex"),
        offline(),
        TIERS,
    )
    assert chosen["tiers"] == {
        "opus": "claude-opus-5",
        "sonnet": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5",
    }
    assert sorted(chosen["guessed"]) == sorted(TIERS)
    assert step(res, "models").status == Status.PARTIAL
    assert "fell back" in step(res, "models").detail


def test_a_pin_beats_both(ctx, monkeypatch) -> None:
    """--default-opus-model is taken at its word, catalogue or no catalogue."""
    built = phase(ctx, monkeypatch, vertex__model_aliases={"opus": "pinned-opus"})
    chosen = select(built, catalogue("claude-opus-4-1", "claude-sonnet-4-5"))
    assert chosen["tiers"]["opus"] == "pinned-opus"
    assert "opus" not in chosen["inferred"] and "opus" not in chosen["guessed"]


def test_the_primary_model_defaults_to_the_opus_tier(ctx, monkeypatch) -> None:
    """Regression: it used to be tiers[1], the middle one, whatever that was.

    Two answers come out of this, and they are not interchangeable: the tier
    name is what settings.json "model" is written as, and the id it resolved to
    is what the catalogue check and the report need.
    """
    built = phase(ctx, monkeypatch)
    chosen = select(built, catalogue("claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5"))
    assert chosen["primary_setting"] == "opus"
    assert chosen["primary"] == "claude-opus-4-1@20250101"
    assert chosen["primary_requested"] is False


def test_the_default_tier_is_configurable(ctx, monkeypatch) -> None:
    """A cheaper everyday default is one setting away, not a code change."""
    built = phase(ctx, monkeypatch, default_tier="haiku")
    chosen = select(built, catalogue("claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5"))
    assert chosen["primary_setting"] == "haiku"
    assert chosen["primary"] == "claude-haiku-4-5@20250101"


def test_asking_for_a_tier_by_name_does_not_look_like_an_unknown_model(ctx, monkeypatch) -> None:
    """--default-model opus asks for a tier, and gets that tier's live id.

    Taking "opus" as an id would have it checked against the catalogue, which
    of course does not list it, and the run would report the model the operator
    just asked for as unavailable.
    """
    built = phase(ctx, monkeypatch, settings={"model": "opus"})
    res = result()
    chosen = built._select_models(
        res,
        built.settings.section("provision"),
        built.settings.section("provision.vertex"),
        catalogue("claude-opus-4-1", "claude-sonnet-4-5"),
        TIERS,
    )
    assert chosen["primary_setting"] == "opus"
    assert chosen["primary"] == "claude-opus-4-1@20250101"
    assert chosen["primary_requested"] is True
    assert not any(s.name == "model check" for s in res.steps)


def test_an_id_is_written_as_the_tier_it_belongs_to(ctx, monkeypatch) -> None:
    """The id still drives the catalogue check; settings.json gets the tier.

    Backends spell the same model three different ways, so a bare tier name is
    the only spelling that stays right when the backend changes underneath.
    """
    built = phase(ctx, monkeypatch, settings={"model": "claude-sonnet-4-5@20250929"})
    chosen = select(built, catalogue("claude-opus-4-1", "claude-sonnet-4-5"))
    assert chosen["primary_setting"] == "sonnet"
    assert chosen["primary"] == "claude-sonnet-4-5@20250929"


def test_a_model_that_matches_no_tier_is_written_as_given(ctx, monkeypatch) -> None:
    """A fine-tune or a preview name is a deliberate choice, not a mistake."""
    built = phase(ctx, monkeypatch, settings={"model": "my-private-tune"})
    chosen = select(built, catalogue("claude-opus-4-1"))
    assert chosen["primary_setting"] == "my-private-tune"


# -- the rest of settings.json -------------------------------------------------
#
# A settings.json with nothing but an env block is not a configured machine:
# Claude Code still has to be told which model to open on, how to render itself
# and what to do about a tool call nobody has ruled on. Those live in
# [provision.settings], and the rule for them is seed-do-not-stomp, because
# Claude Code writes this same file when the operator runs /theme or
# /permissions.


def wrote(ctx, built, existing: dict | None = None, **selection) -> dict:
    """Run _write_settings over ``existing`` and hand back the merged document."""
    path = settings_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        path.write_text(json.dumps(existing))
    built._write_settings(
        result(),
        built.settings.section("provision"),
        {"CLAUDE_CODE_USE_VERTEX": "1"},
        {"primary": "", "primary_requested": False, **selection},
        [],
    )
    return ctx.state["provision_settings"].merged


def test_a_fresh_settings_json_gets_more_than_an_env_block(ctx, monkeypatch) -> None:
    """The defaults ship in the TOML, so this is what the packaged run writes."""
    merged = wrote(ctx, phase(ctx, monkeypatch), primary_setting="opus")
    assert merged["model"] == "opus"
    assert merged["theme"] == "auto"
    assert merged["tui"] == "fullscreen"
    assert merged["permissions"]["defaultMode"] == "default"
    assert merged["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"


def test_an_answer_the_operator_already_gave_is_left_alone(ctx, monkeypatch) -> None:
    """/theme writes to this same file; re-running phase 3 must not undo it."""
    merged = wrote(ctx, phase(ctx, monkeypatch), {"theme": "dark", "tui": "relaxed"})
    assert merged["theme"] == "dark"
    assert merged["tui"] == "relaxed"


def test_a_permission_mode_is_added_without_disturbing_the_rules(ctx, monkeypatch) -> None:
    """Only the leaf is compared: an allow list is not an answer about the mode.

    Comparing the ``permissions`` table as a whole would mean a machine with any
    rule at all never gets a default mode -- and the whole table is exactly what
    must not be replaced.
    """
    merged = wrote(ctx, phase(ctx, monkeypatch), {"permissions": {"allow": ["Bash(ls:*)"]}})
    assert merged["permissions"] == {"allow": ["Bash(ls:*)"], "defaultMode": "default"}


def test_an_existing_permission_mode_wins(ctx, monkeypatch) -> None:
    """Loosening or tightening what a machine already decided is not phase 3's call."""
    merged = wrote(ctx, phase(ctx, monkeypatch), {"permissions": {"defaultMode": "plan"}})
    assert merged["permissions"]["defaultMode"] == "plan"


def test_the_configured_values_can_be_made_authoritative(ctx, monkeypatch) -> None:
    """One setting turns seeding into enforcement, for a fleet with a house style."""
    built = phase(ctx, monkeypatch, overwrite_existing_settings=True)
    merged = wrote(ctx, built, {"theme": "dark", "permissions": {"defaultMode": "plan"}})
    assert merged["theme"] == "auto"
    assert merged["permissions"]["defaultMode"] == "default"


def test_emptying_a_setting_leaves_the_key_out_entirely(ctx, monkeypatch) -> None:
    """How a key is switched off: no value, no key -- not a written empty string."""
    built = phase(ctx, monkeypatch, settings={"theme": "auto", "tui": ""})
    merged = wrote(ctx, built)
    assert merged["theme"] == "auto"
    assert "tui" not in merged


def test_the_exported_model_is_the_tier_so_a_re_run_agrees_with_itself(ctx, monkeypatch) -> None:
    """INITIALIZE_CLAUDE_AI_MODEL is read back as provision.settings.model.

    Exporting the id would have the next run treat it as an explicit request and
    write an id into settings.json, undoing the tier on every second run.
    """
    built = phase(ctx, monkeypatch)
    built._export_env(
        result(),
        built.settings.section("provision"),
        "vertex",
        {"project": "a-project"},
        {"primary": "claude-opus-5@20251101", "primary_setting": "opus"},
        {"CLAUDE_CODE_USE_VERTEX": "1"},
        [],
    )
    assert ctx.env.get("INITIALIZE_CLAUDE_AI_MODEL") == "opus"


def test_a_key_added_to_the_toml_needs_no_code_change(ctx, monkeypatch) -> None:
    """[provision.settings] is the whole vocabulary; nothing here is listed in Python."""
    built = phase(ctx, monkeypatch, settings={"cleanupPeriodDays": 30, "spinnerTipsEnabled": False})
    merged = wrote(ctx, built)
    assert merged["cleanupPeriodDays"] == 30
    assert merged["spinnerTipsEnabled"] is False


# -- retiring the previous backend ---------------------------------------------
#
# These flags decide where Claude Code sends its traffic, so they have to be
# able to *stop* sending it somewhere. A merge alone cannot delete, which meant
# that switching backend left the old one's variables in settings.json and
# Claude Code went on using them -- from a file nobody remembers editing.

# What a machine provisioned against Vertex has in settings.json, plus two keys
# that came from somewhere else entirely.
PREVIOUSLY_PROVISIONED = {
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLOUD_ML_REGION": "us-east5",
    "ANTHROPIC_VERTEX_PROJECT_ID": "old-project",
    "MY_OWN_VARIABLE": "please-do-not-touch",
    "HTTPS_PROXY": "http://proxy.internal:3128",
}

ANTHROPIC_API_BLOCK = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}


def test_only_variables_this_phase_writes_are_managed(ctx, monkeypatch) -> None:
    """The ownership rule, stated directly: the set is derived, not listed.

    Every backend's ``[env]`` keys and ``[env_names]`` values, with ``{TIER}``
    expanded -- so a backend added to the TOML is covered without touching the
    Python, and nothing else is ever a candidate for removal.
    """
    built = phase(ctx, monkeypatch)
    managed = built._managed_env_names(built.settings.section("provision"), TIERS)

    assert {"CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID"} <= managed
    assert "ANTHROPIC_BASE_URL" in managed
    assert {"CLAUDE_CODE_USE_BEDROCK", "AWS_REGION"} <= managed
    assert {f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL" for tier in TIERS} <= managed
    assert not managed & {"MY_OWN_VARIABLE", "HTTPS_PROXY"}


def test_the_previous_backends_variables_are_taken_out_of_settings(ctx, monkeypatch) -> None:
    """Switching to anthropic-api has to stop the Vertex routing, not add to it.

    Without the retraction this file still said CLAUDE_CODE_USE_VERTEX = "1"
    afterwards, and Claude Code kept going to Vertex despite the flag.
    """
    built = phase(ctx, monkeypatch)
    path = settings_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": dict(PREVIOUSLY_PROVISIONED), "model": "opus"}))

    config = built.settings.section("provision")
    stale = built._stale_env_names(config, ANTHROPIC_API_BLOCK, TIERS)
    built._write_settings(result(), config, ANTHROPIC_API_BLOCK, {"primary": "", "primary_requested": False}, stale)

    changes = {change.key: change for change in ctx.state["provision_settings"].changes}
    removed = {key for key, change in changes.items() if change.is_removal}
    assert removed == {
        "env.CLAUDE_CODE_USE_VERTEX",
        "env.CLOUD_ML_REGION",
        "env.ANTHROPIC_VERTEX_PROJECT_ID",
    }
    assert changes["env.CLAUDE_CODE_USE_VERTEX"].describe().endswith("-> (removed)")


def test_settings_the_operator_wrote_themselves_are_never_touched(ctx, monkeypatch) -> None:
    """The guarantee that makes the retraction safe to leave switched on.

    A variable outside the managed set is not this phase's to reason about,
    however much it looks like configuration -- and neither is anything outside
    the ``env`` block.
    """
    built = phase(ctx, monkeypatch)
    path = settings_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": dict(PREVIOUSLY_PROVISIONED), "cleanupPeriodDays": 30, "model": "opus"}))

    config = built.settings.section("provision")
    stale = built._stale_env_names(config, ANTHROPIC_API_BLOCK, TIERS)
    built._write_settings(result(), config, ANTHROPIC_API_BLOCK, {"primary": "", "primary_requested": False}, stale)

    merged = ctx.state["provision_settings"].merged
    assert merged["env"]["MY_OWN_VARIABLE"] == "please-do-not-touch"
    assert merged["env"]["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert merged["cleanupPeriodDays"] == 30
    assert merged["model"] == "opus"


def test_retirement_can_be_switched_off(ctx, monkeypatch) -> None:
    """``retire_stale_env = false`` puts phase 3 back to purely additive."""
    built = phase(ctx, monkeypatch, retire_stale_env=False)
    config = built.settings.section("provision")
    assert built._stale_env_names(config, ANTHROPIC_API_BLOCK, TIERS) == []


def test_the_env_files_retire_the_same_names_as_settings(ctx, monkeypatch) -> None:
    """Otherwise an already-open shell that sourced env.sh keeps the old backend.

    settings.json and the env files are two copies of one answer; a retraction
    that reached only one of them would be the harder half to find.
    """
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    built = phase(ctx, monkeypatch)
    ctx.env.set("CLAUDE_CODE_USE_VERTEX", "1", passthrough=True, apply=False)

    res = result()
    built._export_env(
        res,
        built.settings.section("provision"),
        "anthropic-api",
        {"endpoint": "https://api.anthropic.com"},
        {"primary": ""},
        ANTHROPIC_API_BLOCK,
        ["CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION"],
    )

    assert ctx.env.get("CLAUDE_CODE_USE_VERTEX") == ""
    # Only what was actually there is reported as retired.
    assert step(res, "environment").data["retired"] == ["CLAUDE_CODE_USE_VERTEX"]
    assert "1 retired" in step(res, "environment").detail


# -- values there is no honest default for -------------------------------------
#
# The counterpart to reading an env file: when every layer has been asked and
# the answer is still nothing, the run has to stop. Writing a plausible guess
# would produce a settings.json that looks finished, fails later against a
# project nobody chose, and is read back by whoever debugs it as deliberate.


def test_vertex_without_a_project_stops_the_phase(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch, vertex__project="")
    res = result()
    with pytest.raises(MissingSettingError):
        built._require_profile(res, built.settings.section("provision"), "vertex", {"project": ""})
    assert step(res, "project").status == Status.FAILED
    assert step(res, "project").error == "missing_setting"


def test_bedrock_without_a_region_stops_the_phase(ctx, monkeypatch) -> None:
    """Every Bedrock call is addressed to a region and there is no default one."""
    built = phase(ctx, monkeypatch)
    res = result()
    with pytest.raises(MissingSettingError):
        built._require_profile(res, built.settings.section("provision"), "bedrock", {"region": ""})
    assert step(res, "region").status == Status.FAILED


def test_the_failure_names_every_way_to_supply_the_value(ctx, monkeypatch) -> None:
    """Being told a key is empty is not help; being told what to type is.

    The flag, the environment variables -- explicitly including the env file --
    and the raw setting, all read out of the [sources] table so they cannot
    drift from the ${VAR} chains that do the reading.
    """
    built = phase(ctx, monkeypatch, vertex__project="")
    res = result()
    with pytest.raises(MissingSettingError):
        built._require_profile(res, built.settings.section("provision"), "vertex", {"project": ""})

    advice = "\n".join(step(res, "project").remediation)
    assert "--gcloud-project" in advice
    assert "ANTHROPIC_VERTEX_PROJECT_ID" in advice and "GOOGLE_CLOUD_PROJECT" in advice
    assert "--env-file" in advice
    assert "--set provision.vertex.project=" in advice
    # The sources this program can reach that are not variables at all.
    assert "phase 2" in advice and "gcloud config set project" in advice


def test_a_present_value_is_returned_and_nothing_is_reported(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch)
    res = result()
    built._require_profile(res, built.settings.section("provision"), "vertex", {"project": "  a-project  "})
    assert res.steps == []


def test_no_model_at_all_stops_the_phase(ctx, monkeypatch) -> None:
    """A settings.json with no model leaves Claude Code nothing to call."""
    built = phase(ctx, monkeypatch, model_fallbacks={}, vertex__model_aliases={})
    with pytest.raises(MissingSettingError):
        select(built, offline())


def test_requiring_a_model_can_be_switched_off(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch, model_fallbacks={}, vertex__model_aliases={}, require_model=False)
    chosen = select(built, offline())
    assert chosen["tiers"] == {} and chosen["primary"] == ""


def test_a_fatal_phase_is_failed_outright_not_partial() -> None:
    """The whole point of the flag: PARTIAL lets the run go on, and can exit 0.

    Backend selection and endpoint resolution pass before the missing value is
    reached, and roll_up would otherwise read those successes as "some of it
    landed" -- which is true, and beside the point.
    """
    res = result()
    res.add("backend", Status.OK)
    res.add("project", Status.FAILED, error="missing_setting")
    assert res.finalize().status == Status.PARTIAL

    res.fatal = True
    assert res.finalize().status == Status.FAILED


def test_the_verdict_says_what_was_missing_and_that_nothing_was_written(ctx, monkeypatch) -> None:
    built = phase(ctx, monkeypatch)
    res = result()
    res.add("backend", Status.OK)
    res.add("project", Status.FAILED, "vertex needs a project", error="missing_setting")
    res.fatal = True
    level, reason = built.safety(res)
    assert level == Safety.BLOCKED
    assert "project" in reason and "Nothing was written" in reason
