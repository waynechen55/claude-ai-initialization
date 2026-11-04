"""Write → reuse → write is a fixed point: what a run writes, the next run writes again unchanged.

build-example-net's ``BUILD_EXAMPLE_NET_USERNAME: ${BUILD_EXAMPLE_NET_USERNAME:-}`` was
written by one run and deleted by the next: reuse expanded it to "" for the
trial, kept no reference, and building the entry drops empty values. Four
rounds of fixes repaired the slot downstream; none found where it was lost.
This test guards the class: every shape an entry can have, written, read back
the way the next run reads it, and written again, must come out the same --
and no key the entry passes may disappear.
"""

from __future__ import annotations

import json

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Stage

MANIFEST_PARTS = {"BUILD_EXAMPLE_TEST_USERNAME": "user = {value}", "BUILD_EXAMPLE_TEST_PASSWORD": "pass = {value}"}


def corpus(tmp_path) -> dict[str, tuple[str, dict]]:
    manifest = tmp_path / "build-example-test.basic.client.json"
    manifest.write_text(json.dumps({"client": "osc", "read": ["osc"], "parts": MANIFEST_PARTS, "optional_parts": []}))
    script = tmp_path / "mcp_client_bridge_build_example_test.py"
    script.write_text("#!/usr/bin/env python3\n")
    bridge_env = {"MCP_CLIENT_MANIFEST": str(manifest), "MCP_CLIENT_SERVICE": "https://build.example.test"}
    return {
        "optional login parts": (
            Stage.BRIDGE,
            {
                "command": "/usr/bin/python3",
                "args": [str(script)],
                "env": {
                    **bridge_env,
                    "BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-}",
                    "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD:-}",
                },
            },
        ),
        "required login parts": (
            Stage.BRIDGE,
            {
                "command": "/usr/bin/python3",
                "args": [str(script)],
                "env": {
                    **bridge_env,
                    "BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME}",
                    "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD}",
                },
            },
        ),
        "optional and defaulted settings": (
            Stage.PACKAGE,
            {
                "command": "uvx",
                "args": ["some-mcp"],
                "env": {
                    "EXAMPLE_TEST_URL": "https://example.test",
                    "EXAMPLE_TEST_MODE": "${EXAMPLE_TEST_MODE:-}",
                    "EXAMPLE_TEST_TIMEOUT": "${EXAMPLE_TEST_TIMEOUT:-30}",
                    "EXAMPLE_TEST_TOKEN": "${EXAMPLE_TEST_TOKEN}",
                },
            },
        ),
        "session variable": (
            Stage.BRIDGE,
            {
                "command": "/usr/bin/python3",
                "args": [str(script)],
                "env": {
                    **bridge_env,
                    "BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-}",
                    "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD:-}",
                    "SSH_AUTH_SOCK": "${SSH_AUTH_SOCK:-}",
                },
            },
        ),
        "remote with header": (
            Stage.ENDPOINT,
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${EXAMPLE_TEST_TOKEN}"},
            },
        ),
    }


def write_after_reuse(ctx, name: str, stage: str, entry: dict) -> dict:
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    candidate = ExistingRegistrations.for_run(ctx).candidate(
        Registration(name=name, entry=json.loads(json.dumps(entry)), source=PROJECT_FILE), stage
    )
    writer = McpWriter(ctx)
    writer.complete_slots(candidate)
    return writer.entry_for(candidate, name)


@pytest.mark.parametrize(
    "shape",
    [
        "optional login parts",
        "required login parts",
        "optional and defaulted settings",
        "session variable",
        "remote with header",
    ],
)
def test_an_entry_survives_the_next_run_unchanged(ctx, tmp_path, monkeypatch, shape) -> None:
    for variable in (
        "BUILD_EXAMPLE_TEST_USERNAME",
        "BUILD_EXAMPLE_TEST_PASSWORD",
        "EXAMPLE_TEST_MODE",
        "EXAMPLE_TEST_TIMEOUT",
        "EXAMPLE_TEST_TOKEN",
    ):
        monkeypatch.delenv(variable, raising=False)
    stage, entry = corpus(tmp_path)[shape]
    first = write_after_reuse(ctx, "example-test", stage, entry)
    second = write_after_reuse(ctx, "example-test", stage, first)
    assert second == first, "a second run must write what the first wrote"
    lost = set(entry.get("env") or {}) - set(first.get("env") or {})
    assert not lost, f"keys the entry passed were dropped: {sorted(lost)}"
    if entry.get("headers"):
        assert set(entry["headers"]) <= set(first.get("headers") or {})
