"""gitlab-example-net lost its token slot and bugzilla-example-com its API key; neither login could be used.

* The agent proposed mcp-graphql with ``HEADERS: ${GITLAB_EXAMPLE_NET_HEADERS:-}`` --
  a JSON object nobody exports. Empty, mcp-graphql exited at start-up ("HEADERS
  must be a valid JSON string"), and the ``withdraw_optional_reads`` repair then
  removed the slot: nowhere was left for the token. The held
  GITLAB_EXAMPLE_NET_TOKEN was never given, because the key "was already set".
  Credential slots are now written in the form the server takes them, around
  the credential itself, and a repair never removes one.
* bugzilla-example-com's new entry worked as anybody, and the claude CLI copy
  carrying the API key was replaced by it. A login an existing entry supplies
  is carried into the new entry, optional, and a CLI copy is replaced only once
  the file's entry carries the slot.
* The old gitlab entry named a bridge three folders deep (the nesting bug),
  and was judged broken. The final file check now reads every entry as Claude
  Code will run it here, and the report says what travels with the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from initialize_claude_ai.core.paths_home import artifacts_home
from initialize_claude_ai.mcp_auto_discovery import api_kinds
from initialize_claude_ai.mcp_auto_discovery.env_contract import EnvContract
from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def _graphql(**env) -> Candidate:
    return Candidate(
        kind="stdio",
        name="gitlab-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"ENDPOINT": "https://gitlab.example.test/api/graphql", **env},
    )


# -- the form the server takes a credential in ----------------------------------------------------


def test_a_bare_reference_in_a_credential_key_is_given_the_held_credential(ctx, monkeypatch) -> None:
    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "GITLAB_EXAMPLE_TEST_TOKEN")
    candidate = _graphql(HEADERS="${GITLAB_EXAMPLE_TEST_HEADERS:-}")
    candidate.secret_env = {"HEADERS": "env:GITLAB_EXAMPLE_TEST_HEADERS"}
    assert api_kinds.give_held_credential(ctx, candidate, "gitlab.example.test")
    assert candidate.env["HEADERS"] == '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN}"}'
    assert "HEADERS" not in candidate.secret_env, "no longer a slot asking for the whole object"
    already = _graphql(HEADERS='{"Authorization": "Bearer ${X}"}')
    assert not api_kinds.give_held_credential(ctx, already, "gitlab.example.test"), "a form is left alone"


def test_an_unheld_credential_is_written_in_the_form_around_the_credential_itself(ctx, monkeypatch) -> None:
    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "")
    monkeypatch.delenv("GITLAB_EXAMPLE_TEST_HEADERS", raising=False)
    candidate = _graphql()
    candidate.secret_env = {"HEADERS": "env:HEADERS"}
    candidate.handshake = Handshake(ok=True, tools=["introspect-schema"], credential_optional=["HEADERS"])
    entry = McpWriter(ctx).entry_for(candidate, "gitlab-example-test")
    assert entry["env"]["HEADERS"] == '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN:-}"}', (
        "optional stays optional, and the value is valid JSON with the variable empty"
    )
    assert json.loads(entry["env"]["HEADERS"].replace("${GITLAB_EXAMPLE_TEST_TOKEN:-}", ""))


def test_a_whole_value_the_operator_already_exports_is_kept(ctx, monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_EXAMPLE_TEST_HEADERS", '{"Authorization": "Bearer abc"}')
    env = {"HEADERS": "${GITLAB_EXAMPLE_TEST_HEADERS}"}
    assert api_kinds.credential_in_taken_form(ctx, _graphql(), env, "gitlab-example-test") == []
    assert env["HEADERS"] == "${GITLAB_EXAMPLE_TEST_HEADERS}"


def test_a_server_that_is_no_adapter_is_left_alone(ctx) -> None:
    other = Candidate(kind="stdio", name="x", command=["python3", "bridge.py"])
    env = {"HEADERS": "${X_HEADERS:-}"}
    assert api_kinds.credential_in_taken_form(ctx, other, env, "x") == [] and env == {"HEADERS": "${X_HEADERS:-}"}


def test_a_login_kept_in_claude_code_s_settings_counts_as_held(ctx, monkeypatch) -> None:
    """The operator keeps credentials in settings.json env; the shell need not export them."""
    from initialize_claude_ai.mcp_auto_discovery import claude_check, inventory

    monkeypatch.delenv("GITLAB_EXAMPLE_TEST_TOKEN", raising=False)
    monkeypatch.setattr(claude_check, "_claude_settings_env", lambda ctx: {"GITLAB_EXAMPLE_TEST_TOKEN": "glpat-x"})
    assert inventory.held("gitlab.example.test", ctx.settings, inventory.known_environ(ctx)) == {
        "token": {"TOKEN": "GITLAB_EXAMPLE_TEST_TOKEN"}
    }
    assert api_kinds.held_credential_variable(ctx, "gitlab.example.test") == "GITLAB_EXAMPLE_TEST_TOKEN"
    ctx.settings.set("mcp.discovery.auth.inventory.include_claude_settings", False)
    assert inventory.held("gitlab.example.test", ctx.settings, inventory.known_environ(ctx)) == {}


# -- a repair never removes a credential slot -----------------------------------------------------


def test_withdrawing_empty_slots_keeps_every_credential_slot(ctx) -> None:
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    candidate = Candidate(kind="stdio", name="x", command=["s"], secret_env={"X_LOGIN": "env:X_LOGIN"})
    entry = {"command": "s", "env": {"UA": "${X_UA:-}", "X_LOGIN": "${X_LOGIN:-}", "API_TOKEN": "${X_TOKEN:-}"}}
    changed = doctor._withdraw_optional_reads(candidate, json.loads(json.dumps(entry)))
    assert changed["env"] == {"API_TOKEN": "${X_TOKEN:-}", "X_LOGIN": "${X_LOGIN:-}"}
    assert "kept" in doctor._note
    only = {"command": "s", "env": {"API_TOKEN": "${X_TOKEN:-}"}}
    assert doctor._withdraw_optional_reads(candidate, only) is None, "nothing else to try: reported, not stripped"


def test_a_credential_slot_that_breaks_the_start_is_rewritten_in_its_form(ctx, monkeypatch) -> None:
    monkeypatch.setattr(api_kinds, "held_credential_variable", lambda ctx, host: "")
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    entry = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {"ENDPOINT": "https://gitlab.example.test/api/graphql", "HEADERS": "${GITLAB_EXAMPLE_TEST_HEADERS:-}"},
    }
    changed = doctor._withdraw_optional_reads(_graphql(), entry)
    assert changed["env"]["HEADERS"] == '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN:-}"}'


def test_switching_it_off_restores_the_old_repair(ctx) -> None:
    ctx.settings.set("mcp.healing.keep_credential_slots", False)
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    entry = {"command": "s", "env": {"API_TOKEN": "${X_TOKEN:-}"}}
    assert "env" not in doctor._withdraw_optional_reads(Candidate(kind="stdio", command=["s"]), entry)


# -- a supplied credential is never dropped -------------------------------------------------------


class _Existing:
    def __init__(self, entries):
        self._entries = entries

    def registrations(self):
        return [SimpleNamespace(name=n, entry=e, source="claude_cli") for n, e in self._entries.items()]

    def _about(self, registration, host):
        return host in json.dumps(registration.entry)


BUGZILLA = ["uvx", "mcp-bugzilla", "--bugzilla-server", "https://bugzilla.example.test", "--read-only"]


def test_a_login_an_existing_entry_supplies_is_carried_into_the_new_one(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    old = {
        "command": "uvx",
        "args": BUGZILLA[1:],
        "env": {"BUGZILLA_API_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCD"},
    }
    monkeypatch.setattr(ExistingRegistrations, "for_run", classmethod(lambda cls, ctx: _Existing({"b": old})))
    candidate = Candidate(kind="stdio", name="bugzilla-example-test", command=list(BUGZILLA))
    env: dict[str, str] = {}
    carried = McpWriter(ctx).carry_supplied_credentials(
        candidate, "bugzilla-example-test", env, "bugzilla.example.test"
    )
    assert carried == ["BUGZILLA_API_KEY"]
    assert env["BUGZILLA_API_KEY"].startswith("${") and env["BUGZILLA_API_KEY"].endswith(":-}")
    assert "abcdefghij" not in json.dumps(env), "never a value in the file"
    assert any(str(getattr(v, "reveal", lambda: v)()).startswith("abcdefghij") for v in candidate.kept_out.values())


def test_a_reference_is_carried_as_the_variable_it_read(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    old = {"command": "uvx", "args": BUGZILLA[1:], "env": {"BUGZILLA_API_KEY": "${BUGZILLA_EXAMPLE_KEY}"}}
    monkeypatch.setattr(ExistingRegistrations, "for_run", classmethod(lambda cls, ctx: _Existing({"b": old})))
    env: dict[str, str] = {}
    McpWriter(ctx).carry_supplied_credentials(
        Candidate(kind="stdio", command=list(BUGZILLA)), "b", env, "bugzilla.example.test"
    )
    assert env == {"BUGZILLA_API_KEY": "${BUGZILLA_EXAMPLE_KEY:-}"}


def test_another_server_s_credential_is_not_carried(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    old = {
        "command": "python3",
        "args": ["/x/bridge.py", "https://gitlab.example.test"],
        "env": {"GITLAB_EXAMPLE_TEST_TOKEN": "${GITLAB_EXAMPLE_TEST_TOKEN}"},
    }
    monkeypatch.setattr(ExistingRegistrations, "for_run", classmethod(lambda cls, ctx: _Existing({"g": old})))
    env: dict[str, str] = {"ENDPOINT": "https://gitlab.example.test/api/graphql"}
    assert McpWriter(ctx).carry_supplied_credentials(_graphql(), "g", env, "gitlab.example.test") == []


def test_a_cli_copy_is_replaced_only_once_the_file_carries_its_login(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.dry_run = False
    path = sandbox / ".mcp.json"
    ctx.settings.set("mcp.mcp_json_path", str(path))
    ours = {"type": "stdio", "command": "uvx", "args": BUGZILLA[1:]}
    theirs = dict(ours, env={"BUGZILLA_API_KEY": "${BUGZILLA_EXAMPLE_KEY:-}"})
    path.write_text(json.dumps({"mcpServers": {"bugzilla-example-test": ours}}))
    monkeypatch.setattr(
        ExistingRegistrations, "cli_config_entries", lambda self: {"bugzilla-example-test": (theirs, ["local"])}
    )
    ran: list = []
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setattr(
        ctx.runner,
        "run",
        lambda argv, **kw: (
            ran.append(list(argv)) or SimpleNamespace(ok=True, simulated=False, returncode=0, stdout="", stderr="")
        ),
    )
    writer = McpWriter(ctx)
    writer.published = []
    monkeypatch.setattr(writer, "align_entry_slots", lambda entry: (None, "unread"))
    rows = writer.verify_written({"bugzilla-example-test"})
    written = json.loads(path.read_text())["mcpServers"]["bugzilla-example-test"]
    assert written["env"]["BUGZILLA_API_KEY"] == "${BUGZILLA_EXAMPLE_KEY:-}"
    assert any("kept BUGZILLA_API_KEY" in r["change"] for r in rows)
    added = [a for a in ran if a[:3] == ["/usr/bin/claude", "mcp", "add-json"]]
    assert not added or "BUGZILLA_API_KEY" in added[0][4], "the CLI copy keeps the login too"


# -- the file read as Claude Code runs it ---------------------------------------------------------


def test_a_reference_claude_code_cannot_read_is_reported(ctx) -> None:
    fixed, problems = McpWriter(ctx).check_as_run({"command": "uvx", "env": {"TOKEN": "${A_TOKEN"}})
    assert fixed is None and problems and "cannot read" in problems[0][0] and problems[0][1] is False


def test_a_missing_launched_file_is_pointed_at_the_one_in_the_servers_home(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    home = artifacts_home(ctx.settings) / "gitlab-example-test"
    home.mkdir(parents=True)
    (home / "bridge.py").write_text("print()\n")
    nested = home / "gitlab-example-test" / "gitlab-example-test" / "bridge.py"
    fixed, problems = McpWriter(ctx).check_as_run({"command": "python3", "args": ["-I", str(nested)]})
    assert fixed is not None and problems[0][1] is True
    assert Path(McpWriter(ctx).resolve_program_variable("ICA_MCP_SERVERS")) == artifacts_home(ctx.settings)
    assert (
        fixed["args"][1].endswith("gitlab-example-test/bridge.py")
        and "gitlab-example-test/gitlab" not in fixed["args"][1]
    )


def test_a_missing_file_with_nothing_to_point_at_names_its_variable(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    entry = {"command": "node", "env": {"NODE_EXTRA_CA_CERTS": "${ICA_EXAMPLE_CA:-/nowhere/example-ca.pem}"}}
    fixed, problems = McpWriter(ctx).check_as_run(entry)
    assert fixed is None and "ICA_EXAMPLE_CA" in problems[0][0] and problems[0][1] is False
    assert McpWriter(ctx).check_as_run({"command": "x", "env": {"PATH": "/nowhere:/bin"}}) == (None, [])


def test_the_report_says_what_travels_with_the_file(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / "bridges").mkdir()
    (sandbox / "bridges" / "b.py").write_text("print()\n")
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "b": {
                        "command": "${ICA_EXAMPLE_PY:-/opt/example/python3}",
                        "args": [str(sandbox / "bridges" / "b.py")],
                    }
                }
            }
        )
    )
    beside, machine = McpWriter(ctx).travelling_files()
    assert beside == ["bridges/"] and machine == {"/opt/example/python3": ["ICA_EXAMPLE_PY"]}


# -- a name read through a constant ---------------------------------------------------------------


def test_a_variable_read_through_a_constant_is_read(settings, tmp_path) -> None:
    script = tmp_path / "bridge.py"
    script.write_text('import os\nTOKEN_VAR = "GITLAB_EXAMPLE_TEST_TOKEN"\nTOKEN = os.environ.get(TOKEN_VAR, "")\n')
    _reader, reads, _files = EnvContract(settings).all_reads(["python3", str(script)])
    assert "GITLAB_EXAMPLE_TEST_TOKEN" in reads


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import env_contract

    assert settings.bool_("mcp.healing.keep_credential_slots") is True
    assert settings.bool_("mcp.discovery.reuse.credential_in_taken_form") is True
    assert settings.str_("mcp.discovery.reuse.credential_variable") == api_kinds.DEFAULT_CREDENTIAL_VARIABLE
    assert settings.bool_("mcp.registration.carry_supplied_credentials") is True
    assert settings.bool_("mcp.registration.check_as_run") is True
    assert settings.bool_("mcp.registration.report_what_travels") is True
    assert settings.bool_("mcp.discovery.auth.inventory.include_claude_settings") is True
    assert settings.bool_("mcp.registration.best_copy_speaks_for_name") is True
    assert list(settings.list_("mcp.env_contract.constant_patterns")) == list(env_contract.CONSTANT_PATTERNS)
    assert list(settings.list_("mcp.env_contract.indirect_read_patterns")) == list(env_contract.INDIRECT_READ_PATTERNS)


# -- two copies of one name ----------------------------------------------------------------------


def test_the_best_proven_of_two_same_named_copies_speaks_for_the_name(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome

    anonymous = _graphql()
    anonymous.handshake = Handshake(
        ok=True,
        tools=["query-graphql"],
        contract=[{"task": "describe the API", "tool": "query-graphql", "passed": True}],
    )
    logged_in = Candidate(kind="stdio", name="gitlab-example-net", command=["npx", "-y", "@zereight/mcp-gitlab"])
    logged_in.handshake = Handshake(
        ok=True,
        tools=["whoami"],
        contract=[
            {"task": "describe the API", "tool": "whoami", "passed": True},
            {"task": "read the account's own record", "tool": "whoami", "passed": True},
        ],
    )
    origin = {
        id(anonymous): SimpleNamespace(name="gitlab-example-test", source="project"),
        id(logged_in): SimpleNamespace(name="gitlab-example-test", source="claude_cli"),
    }
    monkeypatch.setattr(
        ExistingRegistrations,
        "for_run",
        classmethod(lambda cls, ctx: SimpleNamespace(registration_for=lambda c: origin.get(id(c)))),
    )
    outcome = TargetOutcome(target="https://gitlab.example.test")
    outcome.stages.append(StageOutcome(stage="package", candidates=[anonymous, logged_in]))
    writer = McpWriter(ctx)
    assert writer._existing_candidate(outcome, "gitlab-example-test") is logged_in
    ctx.settings.set("mcp.registration.best_copy_speaks_for_name", False)
    assert McpWriter(ctx)._existing_candidate(outcome, "gitlab-example-test") is anonymous


def test_a_kept_copy_proved_with_a_credential_is_written_with_its_slot() -> None:
    proved = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {
            "ENDPOINT": "https://gitlab.example.test/api/graphql",
            "HEADERS": '{"Authorization": "Bearer ${GITLAB_EXAMPLE_TEST_TOKEN}"}',
        },
    }
    held = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {"ENDPOINT": "https://gitlab.example.test/api/graphql"},
    }
    assert McpWriter._credential_slots_missing(proved, held) == ["HEADERS"]
    assert McpWriter._credential_slots_missing(proved, proved) == []
    assert McpWriter._credential_slots_missing({"env": {"TIMEOUT": "30"}}, held) == []
