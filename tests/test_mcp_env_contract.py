"""Every variable a server reads is in its ``env`` block.

openqa.example.net's bridge read six variables and its entry declared one. The
manifest was found by a fallback in the source -- ``os.environ.get(...) or
os.path.join(HERE, ...)`` -- and on another machine the directory also held an
older manifest under a near-identical name. The bridge opened that one,
rejected its schema and exited; the operator saw ``Connection closed``, and
nothing in the entry said a manifest existed, where it was, or which variable
chose it.

These tests hold the contract from three sides: the scanner finds what a
script reads, the writer names all of it with the value the script would have
used anyway (so nothing changes here), and nothing it writes is a form Claude
Code expands differently from us -- no bare ``${NAME}``, no nested default.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery.env_contract import EnvContract
from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
from initialize_claude_ai.mcp_auto_discovery.portability import expand
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

#: The shape of the agent-written openQA bridge, trimmed to its reads.
OPENQA_BRIDGE = """\
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))

def load_manifest():
    path = os.environ.get("MCP_BRIDGE_MANIFEST") or os.path.join(
        HERE, "openqa_example_net.operations.json"
    )
    return json.load(open(path))

def base_url():
    return os.environ.get("MCP_BRIDGE_BASE_URL", "https://openqa.example.test")

def timeout_seconds():
    return float(os.environ.get("MCP_BRIDGE_TIMEOUT", "30"))

def ssl_context():
    ca_bundle = os.environ.get("MCP_BRIDGE_CA_BUNDLE")
    if not ca_bundle:
        sibling = os.path.join(HERE, "openqa-ca-chain.pem")
    home = os.environ.get("HOME")
    return ca_bundle

def auth():
    return os.environ.get("MCP_BRIDGE_API_KEY"), os.environ.get("MCP_BRIDGE_API_SECRET")
"""


def bridge_dir(tmp_path: Path) -> Path:
    here = tmp_path / "bridge"
    here.mkdir()
    (here / "mcp_bridge_openqa.py").write_text(OPENQA_BRIDGE)
    (here / "openqa_example_net.operations.json").write_text('{"schema": 1, "operations": []}')
    (here / "openqa-ca-chain.pem").write_text("")
    return here


def reads(ctx, argv) -> dict[str, str | None]:
    return {r.name: r.default for r in EnvContract(ctx.settings).reads_of(argv)}


# -- the scanner ---------------------------------------------------------------


def test_every_read_is_found_with_the_default_the_script_uses(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    found = reads(ctx, [sys.executable, str(here / "mcp_bridge_openqa.py")])

    assert set(found) == {
        "MCP_BRIDGE_MANIFEST",
        "MCP_BRIDGE_BASE_URL",
        "MCP_BRIDGE_TIMEOUT",
        "MCP_BRIDGE_CA_BUNDLE",
        "MCP_BRIDGE_API_KEY",
        "MCP_BRIDGE_API_SECRET",
    }, "and HOME is a fact about the host, not a setting"
    assert found["MCP_BRIDGE_BASE_URL"] == "https://openqa.example.test"
    assert found["MCP_BRIDGE_TIMEOUT"] == "30"
    assert found["MCP_BRIDGE_API_KEY"] is None


def test_a_fallback_file_is_the_default_by_absolute_path(ctx, tmp_path) -> None:
    """The file ``or os.path.join(HERE, ...)`` would open -- the one that broke."""
    here = bridge_dir(tmp_path)
    found = reads(ctx, [str(here / "mcp_bridge_openqa.py")])

    assert found["MCP_BRIDGE_MANIFEST"] == str((here / "openqa_example_net.operations.json").resolve())
    assert found["MCP_BRIDGE_CA_BUNDLE"] == str((here / "openqa-ca-chain.pem").resolve())


def test_a_fallback_that_names_no_existing_file_is_no_default(ctx, tmp_path) -> None:
    """A quoted string that looks like a filename is not evidence of anything."""
    here = bridge_dir(tmp_path)
    (here / "openqa-ca-chain.pem").unlink()
    assert reads(ctx, [str(here / "mcp_bridge_openqa.py")])["MCP_BRIDGE_CA_BUNDLE"] is None


def test_other_languages_are_read_too(ctx, tmp_path) -> None:
    script = tmp_path / "server.js"
    script.write_text(
        'const url = process.env.VENDOR_URL || "https://v.example.test";\nconst key = process.env["VENDOR_KEY"];\n'
    )
    shell = tmp_path / "run.sh"
    shell.write_text('exec server --level "${VENDOR_LEVEL:-info}" --path "$1"\n')

    found = reads(ctx, ["node", str(script), str(shell)])
    assert found == {
        "VENDOR_URL": "https://v.example.test",
        "VENDOR_KEY": None,
        "VENDOR_LEVEL": "info",
    }


def test_binaries_and_package_names_are_not_scanned(ctx, tmp_path) -> None:
    binary = tmp_path / "tool"
    binary.write_bytes(b"\x7fELF\0\0os.environ.get('NOT_A_READ')")
    assert reads(ctx, [str(binary), "@scope/some-mcp-package", "--flag"]) == {}


def test_trust_and_passthrough_variables_are_left_to_their_owners(ctx, tmp_path) -> None:
    script = tmp_path / "s.py"
    script.write_text('os.environ.get("SSL_CERT_FILE")\nos.environ.get("PATH")\nos.environ.get("REAL_ONE")\n')
    assert reads(ctx, [str(script)]) == {"REAL_ONE": None}


def test_the_scanner_can_be_switched_off(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    ctx.settings.set("mcp.env_contract.enabled", False)
    assert reads(ctx, [str(here / "mcp_bridge_openqa.py")]) == {}


def test_the_config_patterns_match_the_code_defaults() -> None:
    """Two copies of one list drift; this is the tripwire."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the dependency pyproject declares for it
        import tomli as tomllib

    from initialize_claude_ai.mcp_auto_discovery import env_contract

    data = Path(env_contract.__file__).parents[1] / "data" / "40-mcp.toml"
    table = tomllib.loads(data.read_text())["mcp"]["env_contract"]
    assert list(table["read_patterns"]) == list(env_contract.READ_PATTERNS)
    assert list(table["ignore"]) == list(env_contract.IGNORE)
    assert table["credential_in_default"] == env_contract.CREDENTIAL_IN_DEFAULT
    assert list(table["fallback_literal_patterns"]) == list(env_contract.FALLBACK_LITERAL_PATTERNS)


def test_a_literal_fallback_after_the_read_is_its_default(ctx, tmp_path) -> None:
    """``get("X") or "lit"`` -- on the same line or the next -- falls back to that literal.

    The generated gitlab.example.net bridge read its User-Agent this way. With the
    default unseen, the writer took a value equal to it for a credential and
    wrote a reference nothing sets.
    """
    script = tmp_path / "bridge.py"
    script.write_text(
        "import os\n"
        'timeout = os.environ.get("MCP_BRIDGE_TIMEOUT") or "30"\n'
        "agent = (\n"
        '    os.environ.get("MCP_BRIDGE_USER_AGENT")\n'
        '    or "mcp-bridge-example/1"\n'
        ")\n"
        'mode = os.getenv("MODE", "") or "fast"\n'
        'chain = os.environ.get("A") or os.environ.get("B") or "b-default"\n'
    )
    found = reads(ctx, [str(script)])

    assert found["MCP_BRIDGE_TIMEOUT"] == "30"
    assert found["MCP_BRIDGE_USER_AGENT"] == "mcp-bridge-example/1"
    assert found["MODE"] == "fast", "an empty default is unset; the literal after it is what is used"
    assert found["A"] is None, "A falls back to another read, not to a literal"
    assert found["B"] == "b-default"


def test_an_empty_literal_still_looks_for_the_fallback_file(ctx, tmp_path) -> None:
    """``or ""`` means unset: the file the next lines open is still the default."""
    here = tmp_path / "b"
    here.mkdir()
    (here / "ops.json").write_text("{}")
    script = here / "bridge.py"
    script.write_text(
        "import os\n"
        'path = os.environ.get("MCP_BRIDGE_MANIFEST") or ""\n'
        "if not path:\n"
        '    path = os.path.join(os.path.dirname(__file__), "ops.json")\n'
    )
    assert reads(ctx, [str(script)])["MCP_BRIDGE_MANIFEST"] == str((here / "ops.json").resolve())


# -- the entry -----------------------------------------------------------------


def openqa_candidate(here: Path) -> Candidate:
    """What the agent reported: one variable of six."""
    return Candidate(
        kind="stdio",
        name="openqa-example-net",
        transport=Transport.STDIO,
        command=[sys.executable, str(here / "mcp_bridge_openqa.py")],
        env={"MCP_BRIDGE_BASE_URL": "https://openqa.example.test"},
    )


def test_the_entry_names_every_read(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    candidate = openqa_candidate(here)
    env = McpWriter(ctx).entry_for(candidate, "openqa-example-net")["env"]

    for name in ("MANIFEST", "BASE_URL", "TIMEOUT", "CA_BUNDLE", "API_KEY", "API_SECRET"):
        assert f"MCP_BRIDGE_{name}" in env, name
    assert any("launched script reads them" in note for note in candidate.notes)


def test_each_read_carries_the_value_the_script_already_used(ctx, tmp_path) -> None:
    """Unset, the entry behaves exactly as the script did on its own."""
    here = bridge_dir(tmp_path)
    env = McpWriter(ctx).entry_for(openqa_candidate(here), "openqa-example-net")["env"]
    resolved = {key: expand(value, {}) for key, value in env.items()}

    assert resolved["MCP_BRIDGE_MANIFEST"] == str((here / "openqa_example_net.operations.json").resolve())
    assert resolved["MCP_BRIDGE_TIMEOUT"] == "30"
    assert resolved["MCP_BRIDGE_API_KEY"] == "", "empty, never the literal ${...}"
    assert env["MCP_BRIDGE_BASE_URL"] == "https://openqa.example.test", "declared values win"


def test_each_read_can_be_changed_by_exporting_one_namespaced_variable(ctx, tmp_path) -> None:
    """Two bridges both read MCP_BRIDGE_MANIFEST; they must not share one export."""
    here = bridge_dir(tmp_path)
    env = McpWriter(ctx).entry_for(openqa_candidate(here), "openqa-example-net")["env"]

    assert "OPENQA_EXAMPLE_NET_MCP_BRIDGE_MANIFEST" in env["MCP_BRIDGE_MANIFEST"]
    moved = expand(env["MCP_BRIDGE_MANIFEST"], {"OPENQA_EXAMPLE_NET_MCP_BRIDGE_MANIFEST": "/elsewhere/m.json"})
    assert moved == "/elsewhere/m.json"


def test_a_read_under_a_configured_root_moves_with_the_root(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    ctx.settings.set("mcp.registration.path_variables", {"ICA_MCP_BRIDGES": str(here.resolve())})
    env = McpWriter(ctx).entry_for(openqa_candidate(here), "openqa-example-net")["env"]

    assert env["MCP_BRIDGE_MANIFEST"] == (f"${{ICA_MCP_BRIDGES:-{here.resolve()}}}/openqa_example_net.operations.json")


def test_nothing_written_is_a_form_claude_code_expands_differently(ctx, tmp_path) -> None:
    """Checked against Claude Code, not assumed.

    An unset bare ``${NAME}`` reaches the server as those literal characters;
    a nested default is not expanded at all. The writer's own reads must use
    neither.
    """
    here = bridge_dir(tmp_path)
    candidate = openqa_candidate(here)
    env = McpWriter(ctx).entry_for(candidate, "openqa-example-net")["env"]

    for key, value in env.items():
        assert not re.search(r"\$\{[^}]*\$\{", value), f"{key} nests a default: {value}"
        assert not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value), f"{key} is bare"


def test_a_deferred_credential_is_written_empty_while_unset(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    candidate = openqa_candidate(here)
    candidate.secret_env = {"MCP_BRIDGE_API_KEY": "env:MCP_BRIDGE_API_KEY"}
    candidate.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        round_trip=True,
        tools=["t"],
        credential_pending=["MCP_BRIDGE_API_KEY"],
    )
    env = McpWriter(ctx).entry_for(candidate, "openqa-example-net")["env"]
    assert env["MCP_BRIDGE_API_KEY"] == "${OPENQA_EXAMPLE_NET_MCP_BRIDGE_API_KEY:-}"


def test_the_briefing_lists_each_setting_and_how_to_change_it(ctx, tmp_path) -> None:
    here = bridge_dir(tmp_path)
    entry = McpWriter(ctx).entry_for(openqa_candidate(here), "openqa-example-net")
    lines = "\n".join(McpWriter._settings_in(entry))

    assert "MCP_BRIDGE_MANIFEST: " in lines and "export OPENQA_EXAMPLE_NET_MCP_BRIDGE_MANIFEST" in lines
    assert "MCP_BRIDGE_API_KEY: export OPENQA_EXAMPLE_NET_MCP_BRIDGE_API_KEY" in lines
    assert "MCP_BRIDGE_BASE_URL = https://openqa.example.test (fixed)" in lines


# -- the entry still starts ----------------------------------------------------


def test_the_entry_as_written_starts_the_bridge(ctx, tmp_path) -> None:
    """Launched with the entry's own env and nothing else, like Claude Code."""
    import subprocess

    from initialize_claude_ai.mcp_auto_discovery.portability import launch_of

    here = bridge_dir(tmp_path)
    (here / "mcp_bridge_openqa.py").write_text(
        OPENQA_BRIDGE
        + "\nm = load_manifest()\nassert m['schema'] == 1, m\n"
        + "assert timeout_seconds() == 30.0\nprint('ok')\n"
    )
    entry = McpWriter(ctx).entry_for(openqa_candidate(here), "openqa-example-net")
    argv, env, _unresolved = launch_of(entry, {"PATH": "/usr/bin:/bin"})
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert done.stdout.strip() == "ok", done.stderr


# -- the repair for a server that minds "set but empty" ------------------------


def test_withdraw_takes_only_the_empty_slots(ctx) -> None:
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    candidate = Candidate(kind="stdio", command=["s"])
    entry = {
        "command": "s",
        "env": {"A": "${X_A:-}", "B": "${X_B:-30}", "C": "${X_C}", "D": "fixed"},
    }
    changed = doctor._withdraw_optional_reads(candidate, json.loads(json.dumps(entry)))

    assert changed["env"] == {"B": "${X_B:-30}", "C": "${X_C}", "D": "fixed"}
    # Held until the repair is proven: a repair that did not help leaves no claim.
    assert "withdrawn" in doctor._note
    assert not any("withdrawn" in note for note in candidate.notes)


def test_withdraw_has_nothing_to_do_without_empty_slots(ctx) -> None:
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    assert doctor._withdraw_optional_reads(Candidate(kind="stdio"), {"env": {"B": "x"}}) is None


def test_withdraw_is_a_configured_repair(ctx) -> None:
    doctor = EntryDoctor(ctx, McpWriter(ctx), None)
    assert "withdraw_optional_reads" in doctor.strategies()
    assert "withdraw_optional_reads" in ctx.settings.str_list("mcp.healing.repairs", [])


# -- and the traditional bridge ------------------------------------------------


def test_the_generated_bridge_names_its_manifest_and_every_read(ctx, tmp_path) -> None:
    """The traditional half: the generator names the files, the scanner the rest."""
    from initialize_claude_ai.mcp_auto_discovery.bridge import ENV_MANIFEST, ENV_TIMEOUT, BridgeBuilder

    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://target.example.test/api"}],
        "paths": {"/items": {"get": {"operationId": "list_items", "responses": {"200": {}}}}},
    }
    candidate, artifacts, detail = BridgeBuilder(ctx).generate_from_openapi(
        "target.example.test", spec, base_url="https://target.example.test"
    )
    assert candidate is not None, detail
    env = McpWriter(ctx).entry_for(candidate, "target-example-test")["env"]

    assert expand(env[ENV_MANIFEST], {}) == str(artifacts.manifest)
    assert ENV_TIMEOUT in env, f"{ENV_TIMEOUT} is read by the generated script"
    # No authentication method was declared or asked for, so no credential
    # variable is: the script names none in its source -- the manifest names
    # the parts of the method the API uses (see bridge_auth).
    assert not any("AUTH" in name for name in env), sorted(env)


# -- and never a secret --------------------------------------------------------


def test_a_default_that_is_a_credential_never_reaches_the_entry(ctx, tmp_path) -> None:
    """A token hard-coded as a fallback is a bug in the script, not a value to publish."""
    script = tmp_path / "s.py"
    token = "ghp_" + "A1b2C3d4" * 5
    script.write_text(
        f'os.environ.get("VENDOR_TOKEN", "{token}")\n'
        'os.environ.get("VENDOR_URL", "https://user:pa55word@v.example.test")\n'
        'os.environ.get("VENDOR_AUTH_MODE", "anonymous")\n'
    )
    candidate = Candidate(kind="stdio", command=[sys.executable, str(script)])
    entry = McpWriter(ctx).entry_for(candidate, "v-example-test")
    text = json.dumps(entry)

    assert token not in text and "pa55word" not in text
    assert entry["env"]["VENDOR_TOKEN"].endswith(":-}")
    assert entry["env"]["VENDOR_URL"].endswith(":-}")
    assert entry["env"]["VENDOR_AUTH_MODE"].endswith(":-anonymous}"), "a selector keeps its value; it is not a secret"
