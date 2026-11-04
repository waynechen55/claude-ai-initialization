"""A run on a machine holding no credentials replaced three working servers and left gitlab with no slot.

On a machine with nothing in Claude Code's settings env, nothing could be
proved by logging in, so order and appearance decided:

* mcp-openapi-proxy answers "401 Client Error: Unauthorized for url: ..." as an
  ordinary result; it was judged accepted, "passed" the account's own record
  anonymously, and replaced build-opensuse-org's working client bridge.
* build-example-net's SSH-signing bridge was trialled without its key file
  (``${…_OBS_SSH_KEY:-/root/.ssh/id_ed25519}`` taken for a secret), and the
  login plan ("Signature@api.example.net; chosen: client") did not rank the waiting
  candidates: a header proxy on the web host won.
* sd.example.com's customer API was detected, yet a waiting mcp-atlassian settled
  the address; its working bridge was called broken for a path one folder deep.
* gitlab-example-net was kept with no credential slot at all.
* The trust bundle and a repair's config folder stayed in the state and scratch
  areas, and a copied .mcp.json named paths that exist nowhere else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from initialize_claude_ai.core.paths_home import artifacts_home
from initialize_claude_ai.mcp_auto_discovery import contract, evidence, login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def _reply(text: str, error: bool = False) -> dict:
    return {"result": {"content": [{"type": "text", "text": text}], "isError": error}}


# -- a refusal in words is a refusal ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("401 Client Error: Unauthorized for url: https://api.example.test/my/notifications", login_check.REJECTED),
        ("403 Client Error: Forbidden for url: https://api.example.test/x", login_check.NO_ACCESS),
        ("503 Server Error: Service Unavailable for url: https://api.example.test/about", login_check.TEMPORARY),
        ("Error: API request failed: Request failed with status code 404 (404: <!DOCTYPE html>", login_check.UNCHECKED),
    ],
)
def test_an_http_failure_stated_in_text_is_a_failure(settings, text, verdict) -> None:
    assert login_check.judge(_reply(text), settings)[0] == verdict
    passed, _detail = contract.judge(_reply(text), {"expect": []}, settings)
    assert passed is not True, "never a passed usage task"


def test_a_listing_that_mentions_a_status_is_still_an_answer(settings) -> None:
    text = json.dumps({"bugs": [{"id": 1, "summary": "status 404 seen after upgrade"}]})
    assert login_check.judge(_reply(text), settings)[0] == login_check.ACCEPTED


# -- a path is a setting ----------------------------------------------------------------------------


def test_a_key_file_path_is_launched_with_not_withheld(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration

    key = tmp_path / "id_ed25519"
    key.write_text("key\n")
    entry = {
        "command": "python3",
        "args": ["/x/obs-bridge.py"],
        "env": {"OBS_SSH_KEY": f"${{BUILD_EXAMPLE_OBS_SSH_KEY:-{key}}}", "OBS_API_URL": "https://api.example.test"},
    }
    candidate = ExistingRegistrations(ctx).candidate(Registration("build-example-test", entry, PROJECT_FILE), "package")
    assert candidate.env["OBS_SSH_KEY"] == str(key)
    assert "OBS_SSH_KEY" not in candidate.secret_env
    assert candidate.references["OBS_SSH_KEY"] == entry["env"]["OBS_SSH_KEY"], "written back as the reference"
    ctx.settings.set("mcp.discovery.reuse.paths_are_settings", False)
    again = ExistingRegistrations(ctx).candidate(Registration("build-example-test", entry, PROJECT_FILE), "package")
    assert "OBS_SSH_KEY" in again.secret_env


# -- the login plan ranks waiting candidates ----------------------------------------------------


def test_the_scheme_the_api_host_asks_for_wins_and_an_unoffered_one_goes_last(ctx) -> None:
    plan = {
        "offered": [
            {"scheme": "basic", "host": "build.example.test"},
            {"scheme": "Signature", "host": "api.example.test"},
        ]
    }
    ctx.settings.set("mcp.discovery.auth.api_host_prefixes", ["api."])
    signing = Candidate(kind="stdio", name="s", command=["python3", "b.py"], env={"OBS_SSH_KEY": "/k"})
    proxy = Candidate(kind="stdio", name="p", command=["npx", "proxy"], secret_env={"API_TOKEN": "env:X"})
    ctx.settings.set("mcp.discovery.auth.evidence.scheme_patterns", {**evidence.SCHEME_PATTERNS, "bearer": ["TOKEN$"]})
    assert evidence.scheme_of(signing, ctx.settings) == "signature"
    first = evidence.key(ctx, signing, "build.example.test", plan)
    last = evidence.key(ctx, proxy, "build.example.test", plan)
    assert last[1] == 3, "a scheme the address offers nowhere"
    assert first < last


# -- what was detected stays detected -------------------------------------------------------------


def test_an_api_kind_one_pipeline_found_is_known_to_every_other(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    outcome = TargetOutcome(target="https://sd.example.test")
    first = TraditionalPipeline(ctx)
    first._api_kinds[outcome.target] = {
        "jira_service_management": {"url": "https://sd.example.test/rest/servicedeskapi/info"}
    }
    assert "jira_service_management" in TraditionalPipeline(ctx)._api_kinds[outcome.target]
    outcome.api_kinds = {
        "jira_service_management": {
            "url": "https://sd.example.test/rest/servicedeskapi/info",
            "label": "Jira Service Management customer",
        }
    }
    assert "jira_service_management" in TraditionalPipeline(ctx).untried_bridges(outcome)


# -- files that moved are found before the trial ----------------------------------------------------


def test_a_moved_file_is_found_again_before_the_entry_is_judged(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    home = artifacts_home(ctx.settings) / "sd-example-test"
    (home / "bridge2").mkdir(parents=True)
    (home / "bridge2" / "sd_bridge.py").write_text("print()\n")
    nested = home / "sd-example-test" / "bridge2" / "sd_bridge.py"
    candidate = Candidate(kind="stdio", name="sd-example-test", command=["python3", str(nested)])
    moved = McpWriter(ctx).repoint_missing(candidate)
    assert moved and candidate.command[1] == str((home / "bridge2" / "sd_bridge.py").resolve())


# -- a slot for the login, even with nothing held -----------------------------------------------------


def _graphql(contract_rows) -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="gitlab-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"ENDPOINT": "https://gitlab.example.test/api/graphql"},
    )
    candidate.handshake = Handshake(ok=True, tools=["query-graphql"], contract=contract_rows)
    return candidate


def test_an_answering_adapter_short_of_a_login_is_given_an_optional_slot(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import api_kinds

    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "")
    monkeypatch.delenv("GITLAB_EXAMPLE_TEST_TOKEN", raising=False)
    short = _graphql(
        [
            {
                "task": "read the current user",
                "tool": "query-graphql",
                "passed": False,
                "detail": "the answer lacks 1 of the 1 expected value(s)",
            }
        ]
    )
    entry = McpWriter(ctx).entry_for(short, "gitlab-example-test")
    assert entry["env"]["HEADERS"] == '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN:-}"}'
    done = _graphql([{"task": "describe the API", "tool": "query-graphql", "passed": True}])
    assert "HEADERS" not in McpWriter(ctx).entry_for(done, "smelt-example-test").get("env", {}), (
        "a public server that did its job gets no slot"
    )


# -- the files an entry names travel with the file -----------------------------------------------------


def test_files_in_the_state_area_are_copied_beside_the_file(ctx, sandbox) -> None:
    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / "project" / ".mcp.json"))
    (sandbox / "project").mkdir()
    state = Path(ctx.state_dir)
    (state / "trust").mkdir(parents=True, exist_ok=True)
    (state / "trust" / "ca-bundle.pem").write_text("pem\n")
    (state / "isolated" / ".config").mkdir(parents=True, exist_ok=True)
    venv = state / "venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "python").write_text("#!/bin/sh\n")
    entry = {
        "command": str(venv / "python"),
        "args": [str(venv / "python")],
        "env": {
            "NODE_EXTRA_CA_CERTS": str(state / "trust" / "ca-bundle.pem"),
            "XDG_CONFIG_HOME": str(state / "isolated" / ".config"),
            "MODE": "readonly",
        },
    }
    fixed = McpWriter(ctx).own_entry_files(entry, "build-example-test")
    home = artifacts_home(ctx.settings) / "build-example-test"
    assert (home / "ca-bundle.pem").is_file() and (home / ".config").is_dir()
    assert str(state) not in json.dumps(fixed["env"]).replace(str(home), ""), "no state-area path left in env"
    assert fixed["command"] == str(venv / "python") and fixed["args"] == [str(venv / "python")], (
        "programs are phase 1's, never copied"
    )
    assert fixed["env"]["MODE"] == "readonly"


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import writer

    section = settings.section("mcp.verification.login_check")
    assert list(section.str_list("failure_text_patterns", [])) == list(login_check.DEFAULT_FAILURE_TEXT_PATTERNS)
    assert settings.bool_("mcp.discovery.reuse.paths_are_settings") is True
    assert settings.dict_("mcp.discovery.auth.evidence.scheme_patterns") == evidence.SCHEME_PATTERNS
    assert settings.bool_("mcp.discovery.auth.evidence.unoffered_last") is True
    assert settings.bool_("mcp.registration.repoint_before_trial") is True
    assert settings.bool_("mcp.discovery.reuse.slot_when_short_of_login") is True
    assert settings.bool_("mcp.registration.entry_files_beside_file") is True
    assert settings.int_("mcp.registration.entry_files_max_bytes") == writer.ENTRY_FILES_MAX_BYTES
    from initialize_claude_ai.mcp_auto_discovery import env_contract

    assert list(settings.list_("mcp.env_contract.launcher_names")) == list(env_contract.LAUNCHER_NAMES)
    assert settings.bool_("mcp.secret_policy.path_is_never_a_secret") is True


def test_the_final_file_check_gives_a_kept_adapter_short_of_a_login_its_slot(ctx, monkeypatch) -> None:
    """Whichever branch kept it: gitlab-example-net's mcp-graphql was left "as it was", with no HEADERS."""
    from initialize_claude_ai.mcp_auto_discovery import api_kinds

    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "")
    short = _graphql([{"task": "read the current user", "tool": "query-graphql", "passed": False, "detail": "lacks"}])
    api_kinds.note_short_of_login(ctx, short)
    entry = {
        "type": "stdio",
        "command": "${ICA_MCP_NPX:-/usr/bin/npx}",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {"ENDPOINT": "https://gitlab.example.test/api/graphql"},
    }
    fixed, added = api_kinds.slot_entry(ctx, entry, "gitlab-example-test")
    assert added == ["HEADERS"] and fixed["env"]["HEADERS"].endswith('${GITLAB_EXAMPLE_TEST_TOKEN:-}"}')
    other = dict(entry, env={"ENDPOINT": "https://smelt.example.test/graphql/"})
    assert api_kinds.slot_entry(ctx, other, "smelt-example-test") == (None, []), "another address's launch"


def test_a_launcher_is_never_read_as_the_server(settings, tmp_path) -> None:
    """Homebrew's npx is a JavaScript file; its own process.env[key] renamed gitlab's HEADERS to `key`."""
    from initialize_claude_ai.mcp_auto_discovery.env_contract import EnvContract

    npx = tmp_path / "npx"
    npx.write_text("#!/usr/bin/env node\nconst v = process.env[key];\n")
    assert EnvContract(settings).scripts_in([str(npx), "-y", "mcp-graphql@2.0.4"]) == []
    bridge = tmp_path / "bridge.py"
    bridge.write_text("import os\n")
    assert EnvContract(settings).scripts_in(["/usr/bin/python3", str(bridge)]) == [bridge]


def test_an_adapter_s_declared_credential_key_is_never_renamed(ctx, tmp_path) -> None:
    npx = tmp_path / "launcher.js"
    npx.write_text("const v = process.env.SOME_OTHER_TOKEN;\n")
    entry = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4", str(npx)],
        "env": {"HEADERS": '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN:-}"}'},
    }
    fixed, change = McpWriter(ctx).align_entry_slots(entry)
    assert fixed is None and "HEADERS" not in change


def test_a_passed_task_only_the_account_can_pass_proves_access() -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import proves_access

    candidate = _graphql(
        [{"task": "read the current user", "tool": "query-graphql", "passed": True, "needs_login": True}]
    )
    candidate.handshake.login = login_check.UNCHECKED
    assert proves_access(candidate)
    candidate.handshake.contract[0]["needs_login"] = False
    assert not proves_access(candidate) or candidate.handshake.login not in login_check.UNPROVED


def test_account_tasks_are_marked_as_needing_the_login(settings) -> None:
    names = {t.name: t.needs_login for t in contract.tasks_for(settings, "gitlab.example.test", ["gitlab", "github"])}
    assert names.get("read the current user") is True
