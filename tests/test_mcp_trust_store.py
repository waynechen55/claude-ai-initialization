"""A target behind an internal CA, and a runtime that ships its own trust store.

``build.example.net`` connected, was registered, and then::

    build-example-net . failed . Connection closed

Nothing in that mentions a certificate. The diagnosis went to ``API_HEADERS``
-- the one variable in the entry anybody could suspect -- and that was wrong
twice over: the package parses ``API_HEADERS`` as comma-separated ``Name:value``
pairs and *silently ignores* a segment without a colon, so the value in the
entry produced no headers and no error. It was never involved.

What actually happened, measured::

    curl https://build.example.net/apidocs/OBS-v2.10.50.yaml   -> HTTP 200
    node fetch(same url)                                    -> UNABLE_TO_GET_ISSUER_CERT_LOCALLY

    issuer = CN=Example CA 2023.1, O=Example Linux Products GmbH

The host trusts Example's internal CA; Node ships its own store and does not. The
OpenAPI spec could not be fetched, the server exited during start-up, and the
client reported a closed connection.

The argument for naming the host's bundle was already in this program, for its
own generated bridges -- "it never weakens verification: it is the set of
authorities this host has been told to trust, which is a superset of a vendored
one". It was wired to the one kind of entry the program writes itself, and
every published package it registers -- which is most of them -- got nothing.

Three facts here were measured rather than assumed, because the design turns on
them:

* ``NODE_EXTRA_CA_CERTS=<system bundle>`` makes the fetch succeed.
* ``NODE_EXTRA_CA_CERTS=""`` behaves exactly like unset -- inert, not harmful.
* an unexpanded ``${NODE_EXTRA_CA_CERTS}`` prints one warning to stderr and the
  process carries on. That is why the lever is written even when no bundle was
  found: silence is what made this cost days.

``CA_CERT_PATH``, which the package documents, is **not** sufficient -- it is
applied to the API client, while the spec loader uses plain ``fetch``. Tested.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.mcp_auto_discovery import trust
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def npx(env: dict[str, str] | None = None) -> Candidate:
    """The real build-example-net entry, as phase 4 wrote it."""
    return Candidate(
        kind="stdio",
        name="build-example-net",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ivotoby/openapi-mcp-server@1.16.1"],
        env={
            "API_BASE_URL": "https://build.example.net",
            "OPENAPI_SPEC_PATH": "https://build.example.net/apidocs/OBS-v2.10.50.yaml",
            **(env or {}),
        },
    )


def bundle(ctx, tmp_path):
    path = tmp_path / "ca-bundle.pem"
    path.write_text("")
    ctx.settings.set("mcp.trust.bundle_candidates", str(path))
    return str(path)


# --------------------------------------------------------------------------- #
# the entry that failed
# --------------------------------------------------------------------------- #


def test_a_node_launcher_is_told_what_this_host_trusts(ctx, tmp_path) -> None:
    path = bundle(ctx, tmp_path)
    entry = McpWriter(ctx).entry_for(npx(), "build-example-net")

    # This host's bundle, through the one per-machine trust name.
    assert entry["env"]["NODE_EXTRA_CA_CERTS"] == f"${{ICA_MCP_CA_BUNDLE:-{path}}}"


def test_a_python_launcher_gets_the_variables_python_reads(ctx, tmp_path) -> None:
    path = bundle(ctx, tmp_path)
    candidate = Candidate(
        kind="stdio",
        name="x",
        transport=Transport.STDIO,
        command=["uvx", "mcp-bugzilla"],
        env={"BASE": "https://bugzilla.example.com"},
    )
    entry = McpWriter(ctx).entry_for(candidate, "bugzilla-example-com")

    # Never ${SSL_CERT_FILE:-…}: a login shell exported SSL_CERT_FILE as a
    # deleted /tmp bundle and every tool of the entry failed.
    assert entry["env"]["SSL_CERT_FILE"] == f"${{ICA_MCP_CA_BUNDLE:-{path}}}"
    assert entry["env"]["REQUESTS_CA_BUNDLE"] == f"${{ICA_MCP_CA_BUNDLE:-{path}}}"
    assert "NODE_EXTRA_CA_CERTS" not in entry["env"], "not this runtime's variable"


def test_the_runtime_is_recognised_through_the_portability_rewriting(ctx, tmp_path) -> None:
    """By the time the entry is written the command is `${ICA_MCP_UVX:-/path/uvx}`.

    A lookup that could not see through that would apply to nothing at all --
    the same class of miss as matching a pattern instead of asking the rule.
    """
    bundle(ctx, tmp_path)
    assert trust.runtime_of("${ICA_MCP_UVX:-/home/linuxbrew/.linuxbrew/bin/uvx}") == "uvx"
    assert trust.runtime_of("${ICA_MCP_NPX}") == "npx"
    assert trust.runtime_of("/usr/bin/python3") == "python3"
    assert trust.runtime_of("npx") == "npx"


def test_a_remote_entry_gets_nothing(ctx, tmp_path) -> None:
    """Claude Code launches no process for a URL, so there is nothing to tell."""
    bundle(ctx, tmp_path)
    candidate = Candidate(
        kind="url",
        name="x",
        url="https://mcp.example.test/mcp",
        transport=Transport.STREAMABLE_HTTP,
    )
    entry = McpWriter(ctx).entry_for(candidate, "x")
    assert "NODE_EXTRA_CA_CERTS" not in entry.get("env", {})


def test_an_unknown_runtime_gets_nothing(ctx, tmp_path) -> None:
    """Guessing a variable for a runtime nobody has mapped would be noise."""
    bundle(ctx, tmp_path)
    candidate = Candidate(
        kind="stdio",
        name="x",
        transport=Transport.STDIO,
        command=["/opt/vendor/some-server"],
        env={"BASE": "https://x.test"},
    )
    entry = McpWriter(ctx).entry_for(candidate, "x")
    assert entry.get("env", {}).get("NODE_EXTRA_CA_CERTS") is None
    assert entry.get("env", {}).get("SSL_CERT_FILE") is None


# --------------------------------------------------------------------------- #
# what happens when this host has no bundle
# --------------------------------------------------------------------------- #


def test_with_no_bundle_the_lever_is_still_written(ctx) -> None:
    """Silence is what made this cost days.

    An unexpanded `${NODE_EXTRA_CA_CERTS}` costs one warning on stderr and the
    server keeps running -- measured against the real Node. A variable the
    operator can see in the file is worth that.
    """
    ctx.settings.set("mcp.trust.bundle_candidates", "")
    entry = McpWriter(ctx).entry_for(npx(), "build-example-net")
    assert entry["env"]["NODE_EXTRA_CA_CERTS"] == "${ICA_MCP_CA_BUNDLE}", "the lever, under the per-machine name"


def test_a_pinned_bundle_wins(ctx, tmp_path) -> None:
    pinned = tmp_path / "pinned.pem"
    pinned.write_text("")
    ctx.settings.set("mcp.trust.bundle", str(pinned))
    ctx.settings.set("mcp.trust.bundle_candidates", "/nonexistent/other.pem")
    assert trust.system_bundle(ctx.settings) == str(pinned)


def test_a_pinned_bundle_that_is_not_there_is_not_used(ctx, caplog) -> None:
    ctx.settings.set("mcp.trust.bundle", "/nonexistent/ca.pem")
    ctx.settings.set("mcp.trust.bundle_candidates", "")
    with caplog.at_level("WARNING"):
        assert trust.system_bundle(ctx.settings) == ""
    assert "ca.pem" in caplog.text


# --------------------------------------------------------------------------- #
# what discovery found beats what a table guessed
# --------------------------------------------------------------------------- #


def test_a_value_discovery_already_found_is_kept_as_the_default(ctx, tmp_path) -> None:
    """An agent that read the product's documentation knows more than this.

    Its answer is not replaced by the detected bundle -- it becomes the
    *default*, because the path-portability rule names every absolute path
    after the variable holding it. Both rules land on the same variable here
    and the result is the one worth having: the discovered bundle is what runs,
    and the operator can still point it elsewhere on another machine.
    """
    bundle(ctx, tmp_path)
    entry = McpWriter(ctx).entry_for(npx({"NODE_EXTRA_CA_CERTS": "/opt/corp/our-ca.pem"}), "build-example-net")
    # The discovered bundle stays the default, under the entry's own name.
    assert entry["env"]["NODE_EXTRA_CA_CERTS"] == "${BUILD_EXAMPLE_NET_NODE_EXTRA_CA_CERTS:-/opt/corp/our-ca.pem}"


def test_it_can_be_switched_off(ctx, tmp_path) -> None:
    bundle(ctx, tmp_path)
    ctx.settings.set("mcp.trust.enabled", False)
    entry = McpWriter(ctx).entry_for(npx(), "build-example-net")
    assert "NODE_EXTRA_CA_CERTS" not in entry["env"]


# --------------------------------------------------------------------------- #
# telling a runtime what to trust is the opposite of telling it not to check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_a_trust_variable_is_not_mistaken_for_a_weakening_one(ctx, name: str) -> None:
    """These must survive `_without_tls_weakening`, which strips by pattern.

    The stripper matches substrings -- NO_VERIFY, INSECURE, REJECT_UNAUTHORIZED
    -- precisely so a variable nobody has met yet is still caught. That kind of
    net is exactly what catches the wrong thing, and it would be silent: the
    fix would be stripped from the entry and the server would fail as before.
    """
    weakening = ctx.settings.str_list("mcp.registration.tls_weakening_env", [])
    patterns = ctx.settings.str_list("mcp.registration.tls_weakening_patterns", [])
    assert name not in weakening
    assert not any(p in name for p in patterns), f"{name} would be stripped from the entry"


def test_the_weakening_variable_goes_and_the_trust_variable_arrives(ctx, tmp_path) -> None:
    """Both halves in one entry: the wish refused, the need answered."""
    bundle(ctx, tmp_path)
    entry = McpWriter(ctx).entry_for(npx({"NODE_TLS_REJECT_UNAUTHORIZED": "0"}), "build-example-net")
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in entry["env"]
    assert entry["env"]["NODE_EXTRA_CA_CERTS"].startswith("${ICA_MCP_CA_BUNDLE:-")


def test_the_operator_is_told_what_the_variable_is_for(ctx, tmp_path) -> None:
    bundle(ctx, tmp_path)
    candidate = npx()
    McpWriter(ctx).entry_for(candidate, "build-example-net")
    assert any("trust store" in note for note in candidate.notes)
    assert any("never removes any" in note for note in candidate.notes)


# --------------------------------------------------------------------------- #
# one rule, both pipelines and both kinds of entry
# --------------------------------------------------------------------------- #


def test_the_bridge_builder_and_the_writer_detect_the_same_bundle(ctx, tmp_path) -> None:
    """Detection lived in BridgeBuilder alone, which is how this gap opened.

    The one kind of entry this program writes itself could reach a host behind
    an internal CA; every package it registered could not.
    """
    from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder

    path = bundle(ctx, tmp_path)
    assert BridgeBuilder(ctx).ca_bundle() == path
    assert trust.system_bundle(ctx.settings) == path
