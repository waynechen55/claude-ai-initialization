"""Both halves of a login are named alike, and an instruction names what the entry reads.

build-example-net's bridge reads ``OBS_USERNAME`` beside an ``OBS_SSH_KEY`` slot.
The role naming (``{SLUG}_{PART}``) written for exactly this kind of login
covered only the secret half, so the user name became
``BUILD_EXAMPLE_NET_OBS_USERNAME`` while the operator's settings held
``BUILD_EXAMPLE_NET_USERNAME``. And the report said "needs OBS_USERNAME" -- the key
the server reads -- while the entry read ``${BUILD_EXAMPLE_NET_OBS_USERNAME:-}``;
exporting what it said would have done nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

BRIDGE = """\
import os
user = os.environ.get("OBS_USERNAME")
key = os.environ.get("OBS_SSH_KEY")
"""


def build_bridge(tmp_path: Path) -> Candidate:
    script = tmp_path / "obs-mcp-bridge.py"
    script.write_text(BRIDGE)
    candidate = Candidate(kind="stdio", name="build-example-test", command=[sys.executable, "-I", str(script)])
    candidate.secret_env = {"OBS_SSH_KEY": "env:OBS_SSH_KEY"}
    return candidate


def test_the_user_name_beside_a_credential_slot_is_named_by_its_role(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    entry = writer.entry_for(build_bridge(tmp_path), "build-example-test")
    assert entry["env"]["OBS_USERNAME"] == "${BUILD_EXAMPLE_TEST_USERNAME:-}"


def test_a_name_the_operator_already_exports_is_never_renamed(ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_OBS_USERNAME", "alice")
    entry = McpWriter(ctx).entry_for(build_bridge(tmp_path), "build-example-test")
    assert entry["env"]["OBS_USERNAME"] == "${BUILD_EXAMPLE_TEST_OBS_USERNAME:-}"


def test_a_name_in_claude_codes_settings_counts_as_exported(ctx, tmp_path) -> None:
    import json

    from initialize_claude_ai.provision.settings_writer import settings_path

    path = settings_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"BUILD_EXAMPLE_TEST_OBS_USERNAME": "alice"}}))
    entry = McpWriter(ctx).entry_for(build_bridge(tmp_path), "build-example-test")
    assert entry["env"]["OBS_USERNAME"] == "${BUILD_EXAMPLE_TEST_OBS_USERNAME:-}"


def test_a_setting_that_is_no_half_of_a_login_keeps_its_namespaced_name(ctx, tmp_path) -> None:
    script = tmp_path / "bridge.py"
    script.write_text('import os\nmode = os.environ.get("OBS_MODE")\n')
    candidate = Candidate(kind="stdio", name="x", command=[sys.executable, str(script)])
    entry = McpWriter(ctx).entry_for(candidate, "build-example-test")
    assert entry["env"]["OBS_MODE"] == "${BUILD_EXAMPLE_TEST_OBS_MODE:-}"


def test_an_instruction_names_the_variable_the_entry_reads(ctx) -> None:
    candidate = Candidate(kind="stdio", name="x", command=["x"])
    entry = {
        "env": {"OBS_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-}", "PLAIN": "1"},
        "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"},
    }
    assert McpWriter.variable_read_for(entry, "OBS_USERNAME", candidate) == "BUILD_EXAMPLE_TEST_USERNAME"
    assert McpWriter.variable_read_for(entry, "Authorization", candidate) == "EXAMPLE_TOKEN"
    candidate.exported_names["ABSENT"] = "EXPORTED_ABSENT"
    assert McpWriter.variable_read_for(entry, "ABSENT", candidate) == "EXPORTED_ABSENT"
    assert McpWriter.variable_read_for(entry, "PLAIN", candidate) == "PLAIN"


def test_what_the_report_asks_for_is_what_the_entry_reads(ctx, tmp_path) -> None:
    """Through publish: every name in needs_env is a variable the written entry references."""
    candidate = build_bridge(tmp_path)
    [registration] = McpWriter(ctx).publish(TargetOutcome(target="https://build.example.test", provisional=[candidate]))
    text = str(registration.entry)
    assert registration.needs_env, "it waits for its login"
    for name in registration.needs_env:
        assert "${" + name in text, f"{name} is asked for but the entry does not read it"


def test_a_url_entrys_header_names_what_to_set_when_the_slot_name_is_read_nowhere(ctx) -> None:
    """openqa-example-net: the header read ${OPENQA_EXAMPLE_NET_AUTHORIZATION}; the report asked for another name."""
    candidate = Candidate(kind="url", name="x", url="https://openqa.example.test/mcp")
    entry = {
        "type": "http",
        "url": "https://openqa.example.test/mcp",
        "headers": {"Authorization": "Bearer ${OPENQA_EXAMPLE_TEST_AUTHORIZATION}"},
    }
    assert (
        McpWriter.variable_read_for(entry, "OPENQA_EXAMPLE_TEST_USERNAME_KEY_SECRET", candidate)
        == "OPENQA_EXAMPLE_TEST_AUTHORIZATION"
    )
    entry["headers"]["Authorization"] = "Bearer ${OPENQA_EXAMPLE_TEST_USERNAME_KEY_SECRET}"
    assert (
        McpWriter.variable_read_for(entry, "OPENQA_EXAMPLE_TEST_USERNAME_KEY_SECRET", candidate)
        == "OPENQA_EXAMPLE_TEST_USERNAME_KEY_SECRET"
    )
