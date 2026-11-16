"""A generated bridge has to start again later, under someone else's PATH.

Stage 3 proves a bridge by launching it and handshaking with it, and then
records the command in ``.mcp.json`` for Claude Code to run in another process,
minutes or months later. Anything in that command still waiting to be resolved
can resolve differently by then -- which is not a theoretical worry: a bridge
registered as ``["python3", ...]`` was proved under ``/usr/bin/python3`` and
started under Homebrew's, whose certificate store does not carry the site's
internal CA, and every call failed TLS verification.

So these tests pin the two things that must be decided at generation time and
written out in full: which interpreter, and which trust store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery.bridge import (
    ENV_BASE_URL,
    ENV_CA_BUNDLE,
    ENV_MANIFEST,
    BridgeBuilder,
)

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://target.example.test/api"}],
    "paths": {
        "/things": {
            "get": {
                "operationId": "listThings",
                "summary": "List the things",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            }
        },
        "/things/{id}": {"get": {"operationId": "getThing", "summary": "One thing"}},
    },
}


@pytest.fixture
def make(ctx):
    """Build a :class:`BridgeBuilder` *after* the test has set its settings.

    A builder snapshots its configuration section when it is constructed, so a
    setting changed afterwards would not be seen -- which is the same reason
    production code builds one per run rather than reusing it.

    Trust-store detection is off unless a test asks for it: it is on by default
    and finds whatever the machine running the suite happens to have, which is
    not a thing to assert against.
    """
    ctx.dry_run = False
    # Detection lives in `trust`, shared with the writer, so that is the
    # off-switch. `mcp.bridge.generated.ca_bundle` still pins a file for
    # bridges alone, and the tests below exercise it.
    ctx.settings.set("mcp.trust.bundle_candidates", "")

    def build(**settings: str) -> BridgeBuilder:
        for key, value in settings.items():
            ctx.settings.set(f"mcp.bridge.generated.{key}", value)
        return BridgeBuilder(ctx)

    return build


@pytest.fixture
def builder(make):
    return make()


# -- the interpreter -----------------------------------------------------------


def test_the_interpreter_is_an_absolute_path(builder) -> None:
    resolved = builder.interpreter()
    assert os.path.isabs(resolved), resolved
    assert resolved != "python3", "a bare name is resolved again at launch, by someone else"
    assert Path(resolved).exists()


def test_it_defaults_to_the_interpreter_this_program_is_running_under(builder) -> None:
    """Absolute by construction, and known to exist -- it is running."""
    assert builder.interpreter() == os.path.realpath(sys.executable)


def test_a_configured_interpreter_wins(make) -> None:
    builder = make(interpreter="/usr/bin/python3")
    assert builder.interpreter() == os.path.realpath("/usr/bin/python3")


def test_a_configured_interpreter_that_is_not_there_does_not_win(make, caplog) -> None:
    """Falling back beats registering a command that cannot start."""
    builder = make(interpreter="/nonexistent/python9")
    with caplog.at_level("WARNING"):
        resolved = builder.interpreter()
    assert resolved == os.path.realpath(sys.executable)
    assert "python9" in caplog.text


# -- the trust store -----------------------------------------------------------


def test_no_bundle_found_means_the_interpreter_keeps_its_own(builder) -> None:
    assert builder.ca_bundle() == "", "empty is 'whatever this Python already trusts'"


def test_the_first_candidate_that_exists_wins(ctx, make, tmp_path) -> None:
    missing, present = tmp_path / "no.pem", tmp_path / "yes.pem"
    present.write_text("")
    ctx.settings.set("mcp.trust.bundle_candidates", f"{missing},{present}")
    assert make().ca_bundle() == str(present)


def test_a_pinned_bundle_beats_the_candidates(ctx, make, tmp_path) -> None:
    pinned, other = tmp_path / "pinned.pem", tmp_path / "other.pem"
    pinned.write_text("")
    other.write_text("")
    ctx.settings.set("mcp.trust.bundle_candidates", str(other))
    assert make(ca_bundle=str(pinned)).ca_bundle() == str(pinned)


def test_a_pinned_bundle_that_is_not_there_is_not_used(make, caplog) -> None:
    """Better the default store than a path urlopen would reject outright."""
    builder = make(ca_bundle="/nonexistent/ca.pem")
    with caplog.at_level("WARNING"):
        assert builder.ca_bundle() == ""
    assert "ca.pem" in caplog.text


# -- what ends up in the candidate ---------------------------------------------


def generate(builder):
    candidate, artifacts, detail = builder.generate_from_openapi(
        "target.example.test", SPEC, base_url="https://target.example.test"
    )
    assert candidate is not None, detail
    return candidate, artifacts


def test_the_generated_candidate_carries_the_resolved_interpreter(builder) -> None:
    candidate, artifacts = generate(builder)
    assert candidate.command[0] == os.path.realpath(sys.executable)
    assert os.path.isabs(candidate.command[1]), "the script path too"
    assert artifacts.tool_count == 2
    assert candidate.env[ENV_BASE_URL] == "https://target.example.test/api"
    # Written beside the script *and* named in env. The script can find it on
    # its own, but a setting the entry does not name is one nobody can find --
    # openqa.example.net opened a stale manifest that way on another machine.
    assert candidate.env[ENV_MANIFEST] == str(artifacts.manifest)
    assert artifacts.manifest.parent == artifacts.script.parent


def test_the_bundle_is_passed_to_the_bridge_when_there_is_one(make, tmp_path) -> None:
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text("")
    candidate, _ = generate(make(ca_bundle=str(bundle)))
    assert candidate.env[ENV_CA_BUNDLE] == str(bundle)
    assert any(str(bundle) in note for note in candidate.notes)


def test_a_bundle_inside_the_bridge_directory_is_still_named(ctx, make, tmp_path) -> None:
    """A PEM that travels with the bridge is named too.

    It used to be left to the script's beside-itself lookup, to keep one more
    state-directory path out of the entry. The writer now rewrites such paths
    through a configured root, so naming it costs nothing in portability and
    tells whoever copies the entry that a bundle exists and which variable
    chooses it.
    """
    out = tmp_path / "bridge-here"
    out.mkdir(parents=True)
    bundle = out / "chain.pem"
    bundle.write_text("")

    ctx.settings.set("mcp.bridge.output_dir", str(out))
    candidate, artifacts = generate(make(ca_bundle=str(bundle)))

    assert candidate.env[ENV_CA_BUNDLE] == str(bundle)
    assert artifacts.script.parent == bundle.parent


def test_no_bundle_means_no_variable_rather_than_an_empty_one(builder) -> None:
    """An empty value would build a context from a file named '' and fail."""
    candidate, _ = generate(builder)
    assert ENV_CA_BUNDLE not in candidate.env


def test_nothing_in_the_generated_file_depends_on_the_environment_that_made_it(
    builder,
) -> None:
    """The script is generic; everything host-specific travels in env."""
    _candidate, artifacts = generate(builder)
    source = artifacts.script.read_text()
    assert "target.example.test" not in source
    assert str(artifacts.manifest) not in source


# -- the generated bridge itself -----------------------------------------------


def drive(script: Path, env: dict[str, str], *requests: dict) -> tuple[list[dict], str]:
    """Speak MCP to a generated bridge over stdio; return its replies and stderr.

    Both halves matter: stdout must carry JSON-RPC and nothing else, or the
    client disconnects, so anything the bridge has to say about itself has to
    come back on stderr.
    """
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    done = subprocess.run(  # noqa: S603 - argv list, no shell, generated by us
        [sys.executable, str(script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **env},
    )
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()], done.stderr


def test_the_bridge_finds_its_own_manifest_with_no_variable_set(builder) -> None:
    """The fallback, tested by running it rather than by reading the entry.

    The manifest and the script are written together and are useless apart, so
    the script resolves the manifest against its own ``__file__`` when no
    variable names it. The entry always names it now; this is what still works
    for somebody who launches the script by hand.
    """
    candidate, artifacts = generate(builder)
    # The entry names it, but the script must still cope when nothing does.
    env = {k: v for k, v in candidate.env.items() if k != ENV_MANIFEST}

    replies, _ = drive(
        artifacts.script,
        env,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert replies[0]["result"]["serverInfo"]["name"].endswith("-bridge")
    assert len(replies[1]["result"]["tools"]) == 2, "it read the manifest beside itself"


def test_moving_the_bridge_directory_moves_the_bridge(builder, tmp_path) -> None:
    """The point of the whole change: relocation without editing anything."""
    import shutil

    candidate, artifacts = generate(builder)
    moved = tmp_path / "somewhere-else"
    shutil.copytree(artifacts.script.parent, moved)

    replies, stderr = drive(
        moved / artifacts.script.name,
        candidate.env,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert replies and replies[0]["result"]["tools"], stderr


def test_a_bridge_prefers_the_manifest_named_for_it(builder, tmp_path) -> None:
    """Two bridges in one directory is ordinary, and a glob gets it wrong.

    "The first *.operations.json" is right for a bridge that owns its
    directory. Collect several in a working directory -- which operators do --
    and the openqa.opensuse.org bridge picks up `openqa_example_net.operations.json`:
    a real manifest, for a different service, loaded in silence. A bridge
    answering confidently about the wrong host is worse than one that will not
    start.
    """
    import shutil

    _candidate, artifacts = generate(builder)
    shared = tmp_path / "several"
    shared.mkdir()
    shutil.copy(artifacts.script, shared / artifacts.script.name)
    shutil.copy(artifacts.manifest, shared / artifacts.manifest.name)
    (shared / "someone_elses.operations.json").write_text('{"operations": []}')

    replies, stderr = drive(shared / artifacts.script.name, {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert replies and replies[0]["result"]["tools"], stderr


def test_a_bridge_that_cannot_tell_which_manifest_is_its_own_refuses(builder, tmp_path) -> None:
    """Better to stop with a sentence than to guess which service to be."""
    import shutil

    _candidate, artifacts = generate(builder)
    shared = tmp_path / "ambiguous"
    shared.mkdir()
    shutil.copy(artifacts.script, shared / artifacts.script.name)
    (shared / "one.operations.json").write_text('{"operations": []}')
    (shared / "two.operations.json").write_text('{"operations": []}')

    replies, stderr = drive(shared / artifacts.script.name, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "ambiguous" in stderr and "MCP_BRIDGE_MANIFEST" in stderr


def test_the_manifest_is_stamped_with_the_generation_that_wrote_it(builder) -> None:
    """The two files are produced together, so the pair has to be checkable."""
    _candidate, artifacts = generate(builder)
    manifest = json.loads(artifacts.manifest.read_text())
    assert manifest["schema"] >= 1
    assert manifest["script"] == artifacts.script.name


def test_a_manifest_from_another_generation_is_refused_by_name(builder, tmp_path) -> None:
    """The openQA failure, and what it looked like before.

    A script regenerated against a manifest that was not produced
    `AttributeError: 'list' object has no attribute 'items'` from the middle of
    building a tool schema -- which the operator saw as `Connection closed`,
    naming neither the file nor the field.
    """
    _candidate, artifacts = generate(builder)
    manifest = json.loads(artifacts.manifest.read_text())
    manifest["schema"] = manifest["schema"] + 1
    artifacts.manifest.write_text(json.dumps(manifest))

    replies, stderr = drive(artifacts.script, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "written for schema" in stderr
    assert "regenerate the bridge" in stderr


def test_a_manifest_of_the_wrong_shape_is_refused_before_it_is_used(builder) -> None:
    """The exact shape that crashed: a list where a mapping was expected."""
    _candidate, artifacts = generate(builder)
    manifest = json.loads(artifacts.manifest.read_text())
    manifest["operations"][0]["path_params"] = ["id"]
    artifacts.manifest.write_text(json.dumps(manifest))

    replies, stderr = drive(artifacts.script, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "path_params" in stderr, "name the field, not the line it blew up on"
    assert "expected an object" in stderr
    assert "Traceback" not in stderr, "a traceback reaches the operator as 'Connection closed'"


def test_an_unstamped_manifest_still_works(builder) -> None:
    """Every bridge already on disk predates the stamp; refusing them is worse."""
    _candidate, artifacts = generate(builder)
    manifest = json.loads(artifacts.manifest.read_text())
    manifest.pop("schema")
    artifacts.manifest.write_text(json.dumps(manifest))

    replies, stderr = drive(artifacts.script, {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert replies and replies[0]["result"]["tools"], stderr


def test_a_manifest_that_is_not_json_says_so(builder) -> None:
    _candidate, artifacts = generate(builder)
    artifacts.manifest.write_text("{ not json")
    replies, stderr = drive(artifacts.script, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "not valid JSON" in stderr


def test_anything_that_escapes_is_still_one_sentence(builder) -> None:
    """The backstop: whatever fails, the operator gets a cause and not a trace.

    A bridge that dies with an unhandled exception is shown by the client as
    "Connection closed" -- the process exited, and that is all it knows.
    """
    _candidate, artifacts = generate(builder)
    source = artifacts.script.read_text().replace(
        "def main() -> int:", "def main() -> int:\n    raise RuntimeError('boom')", 1
    )
    artifacts.script.write_text(source)

    replies, stderr = drive(artifacts.script, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "RuntimeError: boom" in stderr
    assert "generated as a pair" in stderr


def test_a_bridge_with_no_manifest_beside_it_says_so(builder, tmp_path) -> None:
    """An ENOENT deep in json.load is the failure this program keeps fixing."""
    import shutil

    _candidate, artifacts = generate(builder)
    alone = tmp_path / "script-only"
    alone.mkdir()
    shutil.copy(artifacts.script, alone / artifacts.script.name)

    replies, stderr = drive(alone / artifacts.script.name, {}, {"jsonrpc": "2.0", "id": 1})
    assert replies == []
    assert "operations manifest" in stderr and "copied whole" in stderr


def test_the_generated_bridge_handshakes_and_lists_its_tools(builder) -> None:
    candidate, artifacts = generate(builder)
    replies, _stderr = drive(
        artifacts.script,
        candidate.env,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert len(replies) == 2, replies
    assert replies[0]["result"]["protocolVersion"]
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"listthings", "getthing"}


def test_a_bundle_that_is_not_a_bundle_stops_the_bridge_rather_than_the_check(make, tmp_path) -> None:
    """The one outcome that must never happen is quietly reaching the network
    with verification off.

    A file that exists but holds no certificate is the operator's mistake, and
    the bridge refuses to start on it -- loudly, on stderr, with stdout left
    clean so nothing downstream mistakes the complaint for a JSON-RPC message.
    """
    bundle = tmp_path / "not-a-bundle.pem"
    bundle.write_text("this is not a certificate\n")
    candidate, artifacts = generate(make(ca_bundle=str(bundle)))

    replies, stderr = drive(
        artifacts.script,
        candidate.env,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )

    assert replies == [], "it must not answer as though TLS were configured"
    assert "SSL" in stderr or "certificate" in stderr.lower()
