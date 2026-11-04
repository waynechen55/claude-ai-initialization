"""Publishing a verified server: naming, entry shape and credential handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def url_candidate(**kwargs) -> Candidate:
    base = {"kind": "url", "url": "https://example.test/mcp", "transport": Transport.STREAMABLE_HTTP}
    return Candidate(**{**base, **kwargs})


def test_http_entry_shape(ctx) -> None:
    entry = McpWriter(ctx).entry_for(url_candidate())
    assert entry == {"type": "http", "url": "https://example.test/mcp"}


def test_sse_transport_is_labelled_separately(ctx) -> None:
    entry = McpWriter(ctx).entry_for(url_candidate(transport=Transport.SSE))
    assert entry["type"] == "sse"


def test_stdio_entry_splits_command_and_args(ctx) -> None:
    candidate = Candidate(kind="stdio", command=["npx", "-y"], args=["mcp-remote", "https://x.test"])
    entry = McpWriter(ctx).entry_for(candidate)
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "mcp-remote", "https://x.test"]


def test_a_credential_is_written_as_a_placeholder_never_a_value(ctx) -> None:
    """Claude Code expands ${VAR} itself, so the file never holds the secret."""
    candidate = Candidate(
        kind="stdio",
        command=["server"],
        env={"REGION": "us-east5"},
        secret_env={"API_TOKEN": "env:SOME_REAL_VAR"},
    )
    entry = McpWriter(ctx).entry_for(candidate)
    assert entry["env"] == {"API_TOKEN": "${API_TOKEN}", "REGION": "us-east5"}
    assert "SOME_REAL_VAR" not in json.dumps(entry)


def test_empty_environment_values_are_dropped(ctx) -> None:
    entry = McpWriter(ctx).entry_for(Candidate(kind="stdio", command=["s"], env={"EMPTY": ""}))
    assert "env" not in entry


def test_server_name_is_derived_from_the_target(ctx) -> None:
    name = McpWriter(ctx).server_name(url_candidate(), "https://mcp.deepwiki.com")
    assert name == "mcp-deepwiki-com"


def test_an_ipv6_literal_is_named_as_one(ctx) -> None:
    """--mcp-server takes any address, and "[::1]:8080" slugged on its
    punctuation alone comes out "1-8080": unreadable, and indistinguishable from
    a host genuinely called 1. These names exist to be read in `claude mcp list`."""
    writer = McpWriter(ctx)
    assert writer.server_name(url_candidate(), "[::1]:8080") == "ipv6-1-8080"
    assert writer.server_name(url_candidate(), "http://[fd00::2]/mcp") == "ipv6-fd00-2"


def test_an_ipv6_second_host_is_told_apart_as_one(ctx) -> None:
    writer = McpWriter(ctx)
    target = "https://mcp.example.test"
    first = writer.server_name(url_candidate(url="https://mcp.example.test/mcp"), target)
    elsewhere = writer.server_name(url_candidate(url="http://[fd00::2]:8080/mcp"), target, taken=[first])
    assert elsewhere == "mcp-example-test-ipv6-fd00-2"


def test_server_names_do_not_collide(ctx) -> None:
    writer = McpWriter(ctx)
    target = "https://example.test"
    first = writer.server_name(url_candidate(), target)
    second = writer.server_name(url_candidate(url="https://example.test/other"), target, taken=[first])
    third = writer.server_name(Candidate(kind="stdio", command=["srv"]), target, taken=[first, second])
    assert len({first, second, third}) == 3
    assert all(n.startswith("example-test") for n in (first, second, third))


def verified_candidate(**kwargs) -> Candidate:
    candidate = url_candidate(**kwargs)
    candidate.handshake = Handshake(ok=True, server_name="Example", tools=["a", "b"], round_trip=True)
    return candidate


def test_a_dry_run_registration_writes_nothing(ctx, sandbox) -> None:
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    registrations = McpWriter(ctx).publish(outcome)
    assert registrations, "a verified candidate must produce a registration record"
    assert all(r.simulated for r in registrations)
    assert all(not r.written and not r.registered for r in registrations)
    assert not (sandbox / ".mcp.json").exists()


# -- where the file goes -------------------------------------------------------


def test_the_file_is_written_where_claude_code_will_look_for_it(ctx, sandbox) -> None:
    """The working directory, because that is the project Claude Code will open."""
    writer = McpWriter(ctx)
    assert writer.json_path == sandbox / ".mcp.json"
    assert writer.display_path == "./.mcp.json"


def test_a_configured_path_is_taken_as_given(ctx, sandbox) -> None:
    """``mcp.mcp_json_path`` overrides the default, absolute or not."""
    inside = sandbox / "config" / "servers.json"
    ctx.settings.set("mcp.mcp_json_path", str(inside))
    assert McpWriter(ctx).json_path == inside
    assert McpWriter(ctx).display_path == "./config/servers.json"

    ctx.settings.set("mcp.mcp_json_path", "/opt/claude/servers.json")
    outside = McpWriter(ctx)
    assert outside.json_path == Path("/opt/claude/servers.json")
    assert outside.display_path == "/opt/claude/servers.json", "a path elsewhere is shown in full"


def test_the_instructions_cover_here_another_project_and_every_project(ctx) -> None:
    instructions = McpWriter(ctx).usage_instructions()
    assert len(instructions) == 4
    assert "./.mcp.json" in instructions[0]
    assert "cp ./.mcp.json /path/to/project/.mcp.json" in instructions[1]
    assert "--scope user" in instructions[2]
    assert "claude mcp remove" in instructions[3]


# -- writing it for real -------------------------------------------------------


@pytest.fixture
def live(ctx, monkeypatch):
    """A context that really writes the file and still runs no commands.

    ``publish`` does two things; only the write is under test here, so the
    runner stays in dry-run mode and the CLI is out of reach -- registering is
    then skipped the way it is on a machine that has not installed Claude Code.
    """
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)
    ctx.dry_run = False
    return ctx


def read_servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_publishing_writes_the_server_into_the_working_directory(live, sandbox) -> None:
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    registrations = McpWriter(live).publish(outcome)

    assert read_servers(sandbox / ".mcp.json") == {"example-test": {"type": "http", "url": "https://example.test/mcp"}}
    assert [r.written for r in registrations] == [True]
    assert outcome.registered == ["example-test"], "written counts, even with no CLI to register with"
    assert "recorded in ./.mcp.json" in registrations[0].detail


def test_an_existing_file_keeps_the_servers_it_already_had(live, sandbox) -> None:
    """Somebody else's .mcp.json is a file to merge into, not one to replace."""
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"handwritten": {"type": "stdio", "command": "mine"}}}),
        encoding="utf-8",
    )
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    McpWriter(live).publish(outcome)

    assert sorted(read_servers(sandbox / ".mcp.json")) == ["example-test", "handwritten"]


def test_a_second_run_updates_the_entry_rather_than_adding_another(live, sandbox) -> None:
    for url in ("https://example.test/mcp", "https://example.test/mcp/v2"):
        outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate(url=url)])
        McpWriter(live).publish(outcome)

    servers = read_servers(sandbox / ".mcp.json")
    assert list(servers) == ["example-test"]
    assert servers["example-test"]["url"] == "https://example.test/mcp/v2"


def test_every_endpoint_of_one_address_is_written_and_told_apart(live, sandbox) -> None:
    """--exhaustive-mcp: several entries for one server, each named by what it is."""
    outcome = TargetOutcome(
        target="https://mcp.deepwiki.com",
        verified=[
            verified_candidate(url="https://mcp.deepwiki.com/mcp"),
            verified_candidate(url="https://mcp.deepwiki.com/sse", transport=Transport.SSE),
        ],
    )

    McpWriter(live).publish(outcome)

    servers = read_servers(sandbox / ".mcp.json")
    assert sorted(servers) == ["mcp-deepwiki-com-mcp", "mcp-deepwiki-com-sse"]
    assert servers["mcp-deepwiki-com-sse"]["type"] == "sse"
    assert outcome.registered == ["mcp-deepwiki-com-mcp", "mcp-deepwiki-com-sse"]


def test_one_endpoint_is_written_under_the_bare_fqdn(live, sandbox) -> None:
    """--the-best-mcp leaves one winner, and it is named after the address alone."""
    outcome = TargetOutcome(
        target="https://mcp.deepwiki.com",
        verified=[verified_candidate(url="https://mcp.deepwiki.com/mcp")],
    )

    McpWriter(live).publish(outcome)

    assert list(read_servers(sandbox / ".mcp.json")) == ["mcp-deepwiki-com"]


# -- the briefing, which is not in the file ------------------------------------


def read_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_file_holds_servers_and_nothing_else(live, sandbox) -> None:
    """.mcp.json is copied whole -- into another project, into a commit -- so a
    top-level key only this program understands turns every copy into an edit
    somebody has to remember to make. The briefing is reported instead."""
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    writer = McpWriter(live)
    writer.publish(outcome)

    assert list(read_document(sandbox / ".mcp.json")) == ["mcpServers"]
    block = writer.briefing()
    assert "nothing else" in block["read_this_first"]
    assert list(block["servers"]) == ["example-test"]
    assert block["how_to_use_this_file"] == writer.usage_instructions()


def test_the_briefing_covers_every_server_published_in_the_run(live, sandbox) -> None:
    """It is shown once, at the end of the phase, and every target's servers
    have to be in it -- publishing happens per address, the briefing does not."""
    writer = McpWriter(live)
    for host in ("one.test", "two.test"):
        writer.publish(
            TargetOutcome(target=f"https://{host}", verified=[verified_candidate(url=f"https://{host}/mcp")])
        )

    assert list(writer.briefing()["servers"]) == ["one-test", "two-test"]
    assert list(writer.briefing([])["servers"]) == [], "a caller can still brief on a subset"


def test_a_stale_instructions_key_from_an_older_run_is_swept_out(live, sandbox) -> None:
    """Earlier versions wrote the briefing into the file. A file carried over
    from one gets clean -- and nothing else in it is touched, because a
    top-level key this program never wrote belongs to the operator."""
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {}, "_instructions": {"read_this_first": "old"}, "mine": 1}),
        encoding="utf-8",
    )

    McpWriter(live).publish(TargetOutcome(target="https://example.test", verified=[verified_candidate()]))

    written = read_document(sandbox / ".mcp.json")
    assert "_instructions" not in written
    assert written["mine"] == 1
    assert list(written["mcpServers"]) == ["example-test"]


def test_the_file_is_tidied_even_when_the_run_publishes_nothing(live, sandbox) -> None:
    """The sweep inside publish only runs when there is something to write, so a
    file left by an older version would keep its prose for as long as discovery
    found nothing new -- and that is exactly the file somebody copies in the
    meantime."""
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {"kept": {"type": "stdio", "command": "srv"}},
                "_instructions": {"how_to_use_this_file": ["old"]},
                "mine": 1,
            }
        ),
        encoding="utf-8",
    )

    assert McpWriter(live).tidy() == ["_instructions"]

    written = read_document(sandbox / ".mcp.json")
    assert list(written) == ["mcpServers", "mine"], "only the key we wrote is taken out"
    assert written["mcpServers"] == {"kept": {"type": "stdio", "command": "srv"}}


def test_tidying_a_file_that_is_already_configuration_only_changes_nothing(live, sandbox) -> None:
    """Every run after the first: no rewrite, no step, nothing said."""
    path = sandbox / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    before = path.stat().st_mtime_ns

    assert McpWriter(live).tidy() == []
    assert path.stat().st_mtime_ns == before


def test_there_is_nothing_to_tidy_when_there_is_no_file(live, sandbox) -> None:
    assert McpWriter(live).tidy() == []
    assert not (sandbox / ".mcp.json").exists()


def test_a_file_that_cannot_be_parsed_is_left_for_its_owner(live, sandbox) -> None:
    """A rewrite of something we cannot read is a guess about somebody else's
    file; the next publish replaces it properly."""
    path = sandbox / ".mcp.json"
    path.write_text("{not json at all", encoding="utf-8")

    assert McpWriter(live).tidy() == []
    assert path.read_text(encoding="utf-8") == "{not json at all"


def test_a_dry_run_says_what_it_would_sweep_and_writes_nothing(ctx, sandbox) -> None:
    path = sandbox / ".mcp.json"
    original = json.dumps({"mcpServers": {}, "_instructions": {"read_this_first": "old"}})
    path.write_text(original, encoding="utf-8")

    assert McpWriter(ctx).tidy() == ["_instructions"]
    assert path.read_text(encoding="utf-8") == original


def test_which_keys_are_swept_out_is_configurable(live, sandbox) -> None:
    """Only the names configured are removed, so a deployment that used a
    different key can name it and one that uses none keeps everything."""
    live.settings.set("mcp.instructions.strip_keys", "_readme,_instructions")
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {}, "_readme": "old", "_notes": "mine"}), encoding="utf-8"
    )

    McpWriter(live).publish(TargetOutcome(target="https://example.test", verified=[verified_candidate()]))

    written = read_document(sandbox / ".mcp.json")
    assert "_readme" not in written
    assert written["_notes"] == "mine"


def test_every_variable_a_server_needs_is_named_in_the_briefing(live, sandbox) -> None:
    """A server registered without its variables listed fails at first use, in
    front of the user, with no explanation -- which is worse than not
    registering it."""
    # These fixtures are stub scripts, not MCP servers. This test is about
    # what the briefing says, so the start gate -- which launches the entry
    # and refuses one that will not come up -- is turned off rather than
    # satisfied. See tests/test_mcp_entry_as_written.py for the gate itself.
    live.settings.set("mcp.registration.verify_as_written", False)
    candidate = verified_candidate(
        kind="stdio",
        url="",
        command=["npx", "-y", "@vendor/mcp-server", "--host", "https://example.test"],
        env={"VENDOR_BASE_URL": ""},
        secret_env={"VENDOR_API_KEY": "env:REAL_VARIABLE_NAME"},
        install=[["npm", "install", "-g", "@vendor/mcp-server"]],
        source="official MCP registry",
        origin="package",
    )
    outcome = TargetOutcome(target="https://example.test", verified=[candidate])

    writer = McpWriter(live)
    writer.publish(outcome)

    block = writer.briefing()
    server = block["servers"]["example-test"]
    # The variable the entry reads, not the key the server reads: the entry
    # namespaces a generic key, so "export VENDOR_API_KEY" would set a variable
    # nothing reads. sd.example.com's report made exactly that mistake.
    exported = writer.published[0].entry["env"]["VENDOR_API_KEY"].strip("${}")
    assert exported == "EXAMPLE_TEST_VENDOR_API_KEY"
    assert server["required_environment"] == [exported, "VENDOR_BASE_URL"]
    assert server["what"] == "official MCP registry"
    assert server["found_by"] == "package"
    assert server["install_first"] == ["npm install -g @vendor/mcp-server"]
    assert block["required_environment_variables"] == [exported, "VENDOR_BASE_URL"]
    assert block["install_commands"] == ["npm install -g @vendor/mcp-server"]
    assert "must not be" in block["nothing_here_contains_a_secret"]


def test_neither_the_file_nor_the_briefing_ever_carries_a_value(live, sandbox) -> None:
    # These fixtures are stub scripts, not MCP servers. This test is about
    # what the briefing says, so the start gate -- which launches the entry
    # and refuses one that will not come up -- is turned off rather than
    # satisfied. See tests/test_mcp_entry_as_written.py for the gate itself.
    live.settings.set("mcp.registration.verify_as_written", False)
    candidate = verified_candidate(
        kind="stdio",
        url="",
        command=["server", "--endpoint", "https://example.test"],
        secret_env={"API_TOKEN": "env:REAL_VARIABLE_NAME"},
    )
    writer = McpWriter(live)
    writer.publish(TargetOutcome(target="https://example.test", verified=[candidate]))

    text = (sandbox / ".mcp.json").read_text(encoding="utf-8")
    assert "API_TOKEN" in text
    assert "REAL_VARIABLE_NAME" not in text, "the reference is resolved at run time, not written"
    shown = json.dumps(writer.briefing())
    assert "API_TOKEN" in shown
    assert "REAL_VARIABLE_NAME" not in shown, "naming the variable is the point; reading it is not"


def test_a_server_that_is_a_script_this_run_wrote_says_so(live, sandbox) -> None:
    """Nothing else will tell the next person that this server is a file on one
    machine with no package behind it, and that deleting it breaks the config."""
    # These fixtures are stub scripts, not MCP servers. This test is about
    # what the briefing says, so the start gate -- which launches the entry
    # and refuses one that will not come up -- is turned off rather than
    # satisfied. See tests/test_mcp_entry_as_written.py for the gate itself.
    live.settings.set("mcp.registration.verify_as_written", False)
    bridges = sandbox / "bridges"
    bridges.mkdir()
    script = bridges / "mcp_bridge_example.py"
    script.write_text("# generated\n", encoding="utf-8")
    live.settings.set("mcp.bridge.output_dir", str(bridges))
    # A generated bridge always carries the address it relays to; that is also
    # what keeps it registrable. See mcp_auto_discovery/grounding.py.
    candidate = verified_candidate(
        kind="stdio",
        url="",
        command=["python3", str(script)],
        origin="bridge",
        env={"MCP_BRIDGE_BASE_URL": "https://example.test"},
    )

    writer = McpWriter(live)
    writer.publish(TargetOutcome(target="https://example.test", verified=[candidate]))

    block = writer.briefing()
    server = block["servers"]["example-test"]
    assert server["this_server_is_a_script_written_by_this_run"] == [str(script)]
    assert "re-run initialize-claude-ai" in server["keep_it"]
    assert block["generated_scripts"] == [str(script)]
    assert "python3" not in block["generated_scripts"], "the interpreter is not ours to keep"


# -- duplicates ------------------------------------------------------------------


def existing_file(sandbox: Path, servers: dict) -> None:
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_a_second_entry_for_one_service_is_pointed_out(live, sandbox) -> None:
    """Nothing complains about a duplicate: Claude Code loads both, offers both
    sets of tools, and leaves the operator to work out which of two near-identical
    names the model just called. So it is said out loud, once, with the reason."""
    existing_file(sandbox, {"hand-written": {"type": "sse", "url": "https://example.test/sse"}})
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    writer = McpWriter(live)
    registrations = writer.publish(outcome)

    assert [d["name"] for d in registrations[0].duplicates] == ["hand-written"]
    assert outcome.duplicates == registrations[0].duplicates
    assert "same endpoint host" in registrations[0].duplicates[0]["why"]
    assert "claude mcp remove" in " ".join(registrations[0].remediation)
    block = writer.briefing()
    assert "Keep one and remove the other" in block["duplicate_warning"]
    assert block["servers"]["example-test"]["duplicates"][0]["name"] == "hand-written"


def test_two_launch_commands_that_are_the_same_command_are_duplicates(live, sandbox) -> None:
    existing_file(
        sandbox,
        {"mine": {"type": "stdio", "command": "npx", "args": ["-y", "@vendor/mcp-server"]}},
    )
    # The address lives in env, as it must for any entry to be registered at all
    # -- a launcher that never names the service it talks to is refused. See
    # mcp_auto_discovery/grounding.py.
    candidate = verified_candidate(
        kind="stdio",
        url="",
        command=["npx", "-y", "@vendor/mcp-server"],
        env={"ENDPOINT": "https://example.test/graphql"},
    )

    registrations = McpWriter(live).publish(TargetOutcome(target="https://example.test", verified=[candidate]))

    assert [d["name"] for d in registrations[0].duplicates] == ["mine"]
    assert "same launch command" in registrations[0].duplicates[0]["why"]


def test_a_different_service_on_the_same_file_is_not_a_duplicate(live, sandbox) -> None:
    existing_file(sandbox, {"elsewhere": {"type": "http", "url": "https://other.test/mcp"}})
    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])

    assert McpWriter(live).publish(outcome)[0].duplicates == []
    assert outcome.duplicates == []


def test_rewriting_this_run_s_own_entry_is_not_a_duplicate(live, sandbox) -> None:
    """Phase 4 is re-runnable, and a re-run updates its entry in place."""
    for _ in range(2):
        outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])
        registrations = McpWriter(live).publish(outcome)
    assert registrations[0].duplicates == []


def test_the_duplicate_warning_can_be_turned_off(live, sandbox) -> None:
    existing_file(sandbox, {"hand-written": {"type": "sse", "url": "https://example.test/sse"}})
    live.settings.set("mcp.instructions.warn_on_duplicates", "false")

    outcome = TargetOutcome(target="https://example.test", verified=[verified_candidate()])
    assert McpWriter(live).publish(outcome)[0].duplicates == []


def test_a_second_server_is_named_by_what_differs(ctx) -> None:
    """--exhaustive-mcp registers several servers per address, so their names sit
    next to each other: another host is named by its host, another endpoint on
    the same host by its path. Slugging the whole URL would bury both in the
    repetition of the target."""
    writer = McpWriter(ctx)
    target = "https://mcp.deepwiki.com"
    first = writer.server_name(url_candidate(url="https://mcp.deepwiki.com/mcp"), target)
    elsewhere = writer.server_name(url_candidate(url="https://mcp.devin.ai/mcp"), target, taken=[first])
    same_host = writer.server_name(url_candidate(url="https://mcp.deepwiki.com/sse"), target, taken=[first, elsewhere])
    assert first == "mcp-deepwiki-com"
    assert elsewhere == "mcp-deepwiki-com-mcp-devin-ai"
    assert same_host == "mcp-deepwiki-com-sse"


# -- an endpoint that would not talk anonymously -------------------------------


def walled_candidate(scheme: str = "Bearer", **kwargs) -> Candidate:
    """An endpoint that answered 401: real, listening, and not for strangers."""
    candidate = url_candidate(**kwargs)
    candidate.handshake = Handshake(
        ok=False,
        status=401,
        requires_auth=True,
        auth_evidence=["it answered application/json, not a web page"],
        auth_hint=f'{scheme} realm="api"' if scheme else "",
        error="HTTP 401",
    )
    return candidate


def test_an_authenticated_endpoint_is_written_with_an_empty_credential(live, sandbox) -> None:
    """A 401 is a find: the entry goes in with ${NAME} and no value anywhere."""
    outcome = TargetOutcome(target="https://example.test", provisional=[walled_candidate()])

    registrations = McpWriter(live).publish(outcome)

    assert read_servers(sandbox / ".mcp.json") == {
        "example-test": {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${EXAMPLE_TEST_TOKEN}"},
        }
    }
    assert [r.provisional for r in registrations] == [True]
    assert registrations[0].needs_env == ["EXAMPLE_TEST_TOKEN"]
    assert outcome.registered == ["example-test"]


def test_the_credential_variable_is_named_after_the_server(live, sandbox) -> None:
    """Two authenticated targets in one run must not claim the same variable."""
    writer = McpWriter(live)
    for target in ("https://one.test", "https://two.test"):
        writer.publish(TargetOutcome(target=target, provisional=[walled_candidate(url=f"{target}/mcp")]))
    servers = read_servers(sandbox / ".mcp.json")
    assert servers["one-test"]["headers"]["Authorization"] == "Bearer ${ONE_TEST_TOKEN}"
    assert servers["two-test"]["headers"]["Authorization"] == "Bearer ${TWO_TEST_TOKEN}"


def test_the_scheme_the_server_asked_for_is_the_one_sent(live, sandbox) -> None:
    """A server that challenges with Basic does not want a bearer token."""
    outcome = TargetOutcome(target="https://example.test", provisional=[walled_candidate(scheme="Basic")])
    McpWriter(live).publish(outcome)
    header = read_servers(sandbox / ".mcp.json")["example-test"]["headers"]["Authorization"]
    assert header == "Basic ${EXAMPLE_TEST_TOKEN}"


def test_an_operator_s_own_variable_name_wins(live, sandbox) -> None:
    live.settings.set("mcp.discovery.auth.bearer_token_ref", "env:MY_OWN_TOKEN")
    outcome = TargetOutcome(target="https://example.test", provisional=[walled_candidate()])

    registrations = McpWriter(live).publish(outcome)

    assert registrations[0].needs_env == ["MY_OWN_TOKEN"]
    header = read_servers(sandbox / ".mcp.json")["example-test"]["headers"]["Authorization"]
    assert header == "Bearer ${MY_OWN_TOKEN}"


def test_a_name_the_discovery_already_found_is_kept_for_this_instance(live, sandbox) -> None:
    """The vendor's name is kept -- prefixed with the entry's, so it is this instance's alone.

    GITLAB_TOKEN is every GitLab's; a shell exporting one for another instance
    would otherwise win over what this entry was given (own_reference_names).
    """
    candidate = walled_candidate()
    candidate.secret_env = {"GITLAB_TOKEN": "env:GITLAB_TOKEN"}
    outcome = TargetOutcome(target="https://example.test", provisional=[candidate])

    registrations = McpWriter(live).publish(outcome)

    assert registrations[0].needs_env == ["EXAMPLE_TEST_GITLAB_TOKEN"]
    header = read_servers(sandbox / ".mcp.json")["example-test"]["headers"]["Authorization"]
    assert header == "Bearer ${EXAMPLE_TEST_GITLAB_TOKEN}"


def test_a_remote_server_carries_its_credential_in_a_header_not_in_env(live, sandbox) -> None:
    """Claude Code launches no process for an http server, so `env` does nothing
    for one: a credential put there is silently never sent."""
    outcome = TargetOutcome(target="https://example.test", provisional=[walled_candidate()])
    entry = McpWriter(live).publish(outcome)[0].entry
    assert "env" not in entry


def test_registering_an_authenticated_endpoint_can_be_turned_off(live, sandbox) -> None:
    live.settings.set("mcp.discovery.auth.register_authenticated", "false")
    outcome = TargetOutcome(target="https://example.test", provisional=[walled_candidate()])

    assert McpWriter(live).publish(outcome) == []
    assert not (sandbox / ".mcp.json").exists()


def test_a_provisional_name_cannot_collide_with_a_verified_one(live, sandbox) -> None:
    outcome = TargetOutcome(
        target="https://example.test",
        verified=[verified_candidate(url="https://example.test/mcp")],
        provisional=[walled_candidate(url="https://example.test/api/mcp")],
    )
    names = [r.name for r in McpWriter(live).publish(outcome)]
    assert len(set(names)) == 2, names
    assert len(read_servers(sandbox / ".mcp.json")) == 2


def test_the_briefing_says_which_servers_are_still_waiting_on_a_token(live, sandbox) -> None:
    writer = McpWriter(live)
    writer.publish(TargetOutcome(target="https://example.test", provisional=[walled_candidate()]))

    briefing = writer.briefing()

    waiting = briefing["these_need_a_real_credential_before_they_work"]["example-test"]
    # One entry per variable, each carrying its own shape: two variables on one
    # server can want two different kinds of value, and describing both with
    # one sentence is how a bare token was pasted into a header list.
    assert any(line.startswith("EXAMPLE_TEST_TOKEN=") for line in waiting)
    # The name alone is half an instruction; with no profile for this product we
    # do not know the shape, and say so rather than implying "token".
    assert any("shape unknown" in line for line in waiting)
    assert "not asked" not in json.dumps(briefing["servers"]["example-test"]["what"])
    assert "NOT yet working" in briefing["servers"]["example-test"]["status"]
