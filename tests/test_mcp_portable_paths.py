"""Paths this run owns are named by variable, so the entry can be moved.

A generated bridge entry names three files by absolute path -- the script, its
manifest, its CA chain -- inside this program's own state directory:

    "command": "/usr/bin/python3",
    "args": [".../artifacts/openqa-example-net/mcp_bridge_openqa_example_net.py"],
    "env": {"MCP_BRIDGE_CA_BUNDLE": ".../openqa-example-net-chain.pem",
            "MCP_BRIDGE_MANIFEST":  ".../openqa-example-net.operations.json"}

Correct here, meaningless anywhere else, and `.mcp.json` exists to be copied.
Each is now written as ``${VARIABLE:-<the path it has here>}``.

That form was verified against the installed Claude Code before being adopted,
in ``command``, ``args`` **and** ``env``: with the variable unset the default
applies and the server connects; with it set to a relocated copy it connects
from there; with it set to a path that does not exist it fails. So the entry is
unchanged in effect on the machine that wrote it, and relocatable on any other.
"""

from __future__ import annotations

import pathlib

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

WORKS = Handshake(ok=True, tools=["a"], round_trip=True)


def bridge(ctx, artifacts: str) -> Candidate:
    """The real openqa-example-net entry, as the bridge builder writes it."""
    return Candidate(
        kind="stdio",
        name="openqa-example-net",
        transport=Transport.STDIO,
        command=["/usr/bin/python3", f"{artifacts}/openqa-example-net/mcp_bridge.py"],
        env={
            "MCP_BRIDGE_BASE_URL": "https://openqa.example.net",
            "MCP_BRIDGE_CA_BUNDLE": f"{artifacts}/openqa-example-net/chain.pem",
            "MCP_BRIDGE_MANIFEST": f"{artifacts}/openqa-example-net/ops.json",
        },
        handshake=WORKS,
    )


def artifacts_root(ctx) -> str:
    roots = dict(McpWriter(ctx).path_roots())
    return roots["ICA_MCP_ARTIFACTS"]


def test_our_own_files_are_named_by_variable(ctx) -> None:
    root = artifacts_root(ctx)
    entry = McpWriter(ctx).entry_for(bridge(ctx, root), "openqa-example-net")

    assert entry["args"][0] == f"${{ICA_MCP_ARTIFACTS:-{root}}}/openqa-example-net/mcp_bridge.py"
    assert entry["env"]["MCP_BRIDGE_CA_BUNDLE"].startswith(f"${{ICA_MCP_ARTIFACTS:-{root}}}/")
    assert entry["env"]["MCP_BRIDGE_MANIFEST"].startswith(f"${{ICA_MCP_ARTIFACTS:-{root}}}/")


def test_the_default_is_exactly_the_path_it_replaces(ctx) -> None:
    """The guarantee that makes this safe: nothing changes where it was written."""
    root = artifacts_root(ctx)
    entry = McpWriter(ctx).entry_for(bridge(ctx, root), "openqa-example-net")
    # Only the paths this bridge owns. The entry also carries a trust-store
    # variable defaulting to the host's CA bundle, which is correct and is
    # deliberately not under the artifacts root -- see test_mcp_trust_store.py.
    owned = [entry["args"][0], *(v for k, v in entry["env"].items() if k.startswith("MCP_BRIDGE_"))]
    for value in owned:
        if value.startswith("${"):
            variable, _, default = value[2:].partition(":-")
            resolved = default.split("}")[0] + value.split("}", 1)[1]
            assert resolved.startswith(root), f"{variable} does not default to the real path"


def test_even_a_system_file_gets_a_way_to_be_moved(ctx, monkeypatch) -> None:
    """This asserted the opposite, on the grounds that a variable per system
    file would be noise in a file people read.

    The evidence went the other way. `/etc/ssl/ca-bundle.pem` is openSUSE's
    spelling; Debian keeps it at `/etc/ssl/certs/ca-certificates.crt`, so "the
    system's, exists on the next machine" was not true even for the example the
    old rule was argued from. And an entry with four variables is one an
    operator can move, while an entry with four absolute paths is one they
    cannot without first working out which files matter.

    It costs nothing where it was written: an unset `${VAR:-default}` yields
    exactly the original string.
    """
    from initialize_claude_ai.core import trust_store

    # The machine's bundle is whatever this host has; the test fixes it so it
    # asserts the same thing on an openSUSE and a Debian/Ubuntu runner.
    monkeypatch.setattr(trust_store, "system_bundle", lambda settings: "/etc/ssl/ca-bundle.pem")
    candidate = bridge(ctx, artifacts_root(ctx))
    candidate.env["SYSTEM_CA"] = "/etc/ssl/ca-bundle.pem"
    entry = McpWriter(ctx).entry_for(candidate, "openqa-example-net")
    # This machine's own bundle: read through the one per-machine name phase 1 sets.
    assert entry["env"]["SYSTEM_CA"] == "${ICA_MCP_CA_BUNDLE:-/etc/ssl/ca-bundle.pem}"


def test_a_stray_path_is_named_after_the_variable_that_holds_it(ctx) -> None:
    """The fourth report: a PEM under no root, matching no binary.

    `OPENQA_VERIFY` is `openqa-mcp`'s own variable, documented by that package,
    so it is a better name than anything derived from the filename -- and the
    operator does not have to learn a name this program invented. Prefixed
    with the entry's own name (own_reference_names): another openQA instance's
    OPENQA_VERIFY, exported in a shell, must not replace this one's.
    """
    candidate = bridge(ctx, artifacts_root(ctx))
    candidate.env["OPENQA_VERIFY"] = "/home/someone/openqa-example-net-ca-chain.pem"
    entry = McpWriter(ctx).entry_for(candidate, "openqa-example-net")
    assert entry["env"]["OPENQA_VERIFY"] == (
        "${OPENQA_EXAMPLE_NET_OPENQA_VERIFY:-/home/someone/openqa-example-net-ca-chain.pem}"
    )


def test_a_path_in_the_command_is_named_after_itself(ctx) -> None:
    """No env key to borrow, so the basename is what is left."""
    writer = McpWriter(ctx)
    value = writer.portable("/opt/vendor/helper-tool", writer.path_roots())
    assert value == "${ICA_MCP_HELPER_TOOL:-/opt/vendor/helper-tool}"


def test_a_configured_root_still_wins_over_the_general_rule(ctx) -> None:
    """Rule 1 before rule 3, so a bundle of files moves by one export."""
    root = artifacts_root(ctx)
    writer = McpWriter(ctx)
    value = writer.portable(f"{root}/b/chain.pem", writer.path_roots(), key="SOME_CA")
    assert value.startswith("${ICA_MCP_ARTIFACTS:-"), value
    assert "SOME_CA" not in value


def test_the_briefing_names_every_variable_the_entry_actually_carries(ctx, tmp_path) -> None:
    """A variable an operator must export needs a name they can use.

    The relocation advice used to be recomputed from `path_roots()`, which is a
    different answer from what was written the moment a path is named after the
    env key holding it -- no root predicts `OPENQA_VERIFY`. So the briefing is
    read back out of the entries instead.
    """
    import sys

    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    root = artifacts_root(ctx)
    real = pathlib.Path(root) / "openqa-example-net"
    real.mkdir(parents=True, exist_ok=True)
    for name in ("mcp_bridge.py", "chain.pem", "ops.json"):
        (real / name).write_text("")
    pem = tmp_path / "vendor-chain.pem"
    pem.write_text("")

    candidate = bridge(ctx, root)
    # The interpreter this run would actually pin, so it is named by
    # ICA_MCP_PYTHON rather than falling through to its basename.
    candidate.command = [sys.executable, str(real / "mcp_bridge.py")]
    candidate.env["OPENQA_VERIFY"] = str(pem)

    writer = McpWriter(ctx)
    registrations = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[candidate]))
    advice = " ".join(writer.briefing(registrations).get("how_to_move_them_to_another_machine", []))

    entry = registrations[0].entry
    for name in writer._path_variables_in(entry):
        assert name in advice, f"{name} is in the entry and nowhere in the instructions"
    assert "OPENQA_VERIFY" in advice, "the one no root would have predicted"


def test_a_credential_slot_is_not_listed_as_something_to_copy(ctx) -> None:
    """`${TOKEN}` is something to obtain; `${VAR:-/path}` is something to move."""
    writer = McpWriter(ctx)
    found = writer._path_variables_in({"command": "uvx", "env": {"TOKEN": "${GITLAB_TOKEN}", "P": "${ICA_X:-/opt/x}"}})
    assert found == {"ICA_X": "/opt/x"}


def test_naming_can_be_switched_off(ctx) -> None:
    """The older, narrower behaviour is still reachable."""
    ctx.settings.set("mcp.registration.name_every_absolute_path", False)
    writer = McpWriter(ctx)
    assert writer.portable("/opt/vendor/helper-tool", writer.path_roots()) == ("/opt/vendor/helper-tool")


def test_a_url_is_not_a_path(ctx) -> None:
    entry = McpWriter(ctx).entry_for(bridge(ctx, artifacts_root(ctx)), "openqa-example-net")
    assert entry["env"]["MCP_BRIDGE_BASE_URL"] == "https://openqa.example.net"


def test_the_most_specific_root_wins(ctx) -> None:
    """A file under artifacts is named by the artifacts variable, not the state dir."""
    writer = McpWriter(ctx)
    roots = writer.path_roots()
    names = [variable for variable, _ in roots]
    assert names.index("ICA_MCP_ARTIFACTS") < names.index("ICA_STATE_DIR")
    under = f"{dict(roots)['ICA_MCP_ARTIFACTS']}/x/y.py"
    assert writer.portable(under, roots).startswith("${ICA_MCP_ARTIFACTS:-")


def test_the_interpreter_keeps_its_pin_and_gains_an_override(ctx) -> None:
    """Defect #1 was a bare `python3` resolving elsewhere. The default keeps the pin."""
    import sys

    writer = McpWriter(ctx)
    value = writer.portable(sys.executable, writer.path_roots())
    assert value == f"${{ICA_MCP_PYTHON:-{sys.executable}}}"


def test_turning_the_table_off_restores_plain_absolute_paths(ctx) -> None:
    ctx.settings.set("mcp.registration.path_variables", {})
    ctx.settings.set("mcp.registration.interpreter_variable", "")
    ctx.settings.set("mcp.registration.launcher_variable_template", "")
    ctx.settings.set("mcp.registration.name_every_absolute_path", False)
    writer = McpWriter(ctx)
    assert writer.path_roots() == []
    assert writer.portable("/some/where/x.py", []) == "/some/where/x.py"


# --------------------------------------------------------------------------- #
# the launcher binary -- the commonest absolute path of all
# --------------------------------------------------------------------------- #


def test_a_launcher_found_on_this_host_becomes_a_variable(ctx) -> None:
    """The first failure ever reported against this program, still in the file.

    An entry naming `/root/.local/bin/uvx` is correct on the machine that wrote
    it and `ENOENT` on the next -- which Claude Code shows as
    `failed - Connection closed`, saying nothing about a missing binary. The
    interpreter already had this treatment; the launcher, which appears far
    more often, did not.
    """
    import shutil

    uvx = shutil.which("uvx")
    if not uvx:
        import pytest

        pytest.skip("uvx is not installed here, so there is no path to rewrite")

    writer = McpWriter(ctx)
    assert writer.portable(uvx, writer.path_roots()) == f"${{ICA_MCP_UVX:-{uvx}}}"


def test_the_launcher_list_comes_from_the_launch_templates(ctx) -> None:
    """Derived, not listed again, so the two cannot drift apart."""
    names = McpWriter(ctx).launcher_names()
    assert "uvx" in names and "npx" in names
    assert not any(n.startswith("{") for n in names), "a placeholder is not a binary"


def test_a_launcher_this_host_does_not_have_is_not_named(ctx) -> None:
    """No path means no default to write, and a bare name is already portable."""
    ctx.runner.which = lambda name, **_: None  # type: ignore[assignment]
    assert McpWriter(ctx).launcher_roots() == []


def test_a_bare_launcher_name_is_left_alone(ctx) -> None:
    """`uvx` resolved through PATH needs no help; only absolute paths do."""
    writer = McpWriter(ctx)
    assert writer.portable("uvx", writer.path_roots()) == "uvx"


def test_the_interpreter_still_wins_for_the_interpreter(ctx) -> None:
    """Both are whole-path matches, so adding launchers must not shadow it."""
    import sys

    writer = McpWriter(ctx)
    assert writer.portable(sys.executable, writer.path_roots()).startswith("${ICA_MCP_PYTHON:-")


def test_the_briefing_says_what_to_copy_and_what_to_export(ctx, tmp_path) -> None:
    """ "It will not travel" with a remedy attached, rather than only a warning."""
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    root = artifacts_root(ctx)
    script = tmp_path / "mcp_bridge.py"
    script.write_text("")
    candidate = bridge(ctx, root)
    # Point it at a file that really exists, so portability grades it host-bound
    # rather than refusing it outright.
    candidate.command = ["/usr/bin/python3", str(script)]
    candidate.env["MCP_BRIDGE_MANIFEST"] = f"{root}/openqa-example-net/ops.json"

    writer = McpWriter(ctx)
    registrations = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[candidate]))
    briefing = writer.briefing(registrations)

    if "these_entries_only_work_on_this_machine" in briefing:
        assert "how_to_move_them_to_another_machine" in briefing
        advice = " ".join(briefing["how_to_move_them_to_another_machine"])
        assert "export" in advice
