"""Endpoint guessing: the first thing phase 4 does for a target."""

from __future__ import annotations

import os

from initialize_claude_ai.core.config import Settings, load_settings
from initialize_claude_ai.mcp_auto_discovery.address import configure_local_zones
from initialize_claude_ai.mcp_auto_discovery.pathgen import base_urls, expand_paths, generate, mine_html
from initialize_claude_ai.mcp_auto_discovery.types import Transport, slugify


def discovery(sandbox) -> Settings:
    return load_settings(environ=dict(os.environ)).section("mcp.discovery")


def test_version_templates_expand(sandbox) -> None:
    assert expand_paths(["/mcp", "/api/{version}/mcp"], ["v1", "v2"]) == [
        "/mcp",
        "/api/v1/mcp",
        "/api/v2/mcp",
    ]


def test_expansion_normalises_and_deduplicates(sandbox) -> None:
    # A leading slash is added and repeats collapse, but a trailing slash is
    # kept: "/mcp" and "/mcp/" are different routes on enough frameworks.
    assert expand_paths(["mcp//v1", "/mcp/", "/mcp/"], ["v1"]) == ["/mcp/v1", "/mcp/"]


def test_bare_hostname_becomes_https(sandbox) -> None:
    assert base_urls("example.test", discovery(sandbox))[0] == "https://example.test"


def test_loopback_also_gets_plain_http_and_the_usual_ports(sandbox) -> None:
    origins = base_urls("localhost", discovery(sandbox))
    assert "http://localhost" in origins
    assert any(o.startswith("http://localhost:") for o in origins)


def test_an_explicit_port_is_not_second_guessed(sandbox) -> None:
    # The port the caller gave is the port we use -- no 8080, no 3000. Step B of
    # the checklist still asks for the other scheme on that same port, and it
    # comes second, because the caller's own scheme is the better guess.
    origins = base_urls("http://127.0.0.1:9000", discovery(sandbox))
    assert origins == ["http://127.0.0.1:9000", "https://127.0.0.1:9000"]


def test_both_schemes_are_tried_for_step_b(sandbox) -> None:
    origins = base_urls("https://example.test", discovery(sandbox))
    assert "https://example.test" in origins
    assert "http://example.test" in origins
    assert origins.index("https://example.test") < origins.index("http://example.test")


def test_the_alternate_scheme_can_be_turned_off(sandbox) -> None:
    config = Settings(data={"try_both_schemes": False}, environ={})
    assert base_urls("https://example.test", config) == ["https://example.test"]


def test_the_supplied_path_is_probed_first(sandbox) -> None:
    candidates = generate("https://example.test/custom/mcp", discovery(sandbox))
    assert candidates[0].url == "https://example.test/custom/mcp"
    assert "supplied" in candidates[0].reason


def test_generated_candidates_cover_both_transports(sandbox) -> None:
    candidates = generate("https://example.test", discovery(sandbox))
    urls = [c.url for c in candidates]
    assert "https://example.test/mcp" in urls
    transports = {c.transport for c in candidates}
    assert Transport.STREAMABLE_HTTP in transports
    assert Transport.SSE in transports


def test_candidate_list_is_capped(sandbox) -> None:
    config = Settings(
        data={
            "path_templates": [f"/p{i}" for i in range(50)],
            "sse_path_templates": ["/sse"],
            "version_segments": ["v1"],
            "max_candidates": 5,
        },
        environ={},
    )
    assert len(generate("https://example.test", config)) == 5


def test_html_mining_finds_relative_and_absolute_links(sandbox) -> None:
    patterns = discovery(sandbox).str_list("html_link_patterns")
    body = """
      <a href="/api/mcp">endpoint</a>
      <script>const url = "https://other.test/v2/mcp";</script>
      <a href="/docs">not this one</a>
    """
    found = mine_html(body, "https://example.test", patterns)
    assert "https://example.test/api/mcp" in found
    assert "https://other.test/v2/mcp" in found
    assert not any(u.endswith("/docs") for u in found)


def test_slugify_produces_a_usable_server_name() -> None:
    assert slugify("https://mcp.deepwiki.com/mcp") == "mcp-deepwiki-com-mcp"
    assert slugify("!!!", fallback="target") == "target"


def test_a_declared_zone_is_guessed_at_like_any_other_local_host(sandbox) -> None:
    """An internal appliance is on cleartext and on a developer port as often as
    a container is. Probing .corp as though it were a CDN finds nothing."""
    config = discovery(sandbox)
    assert base_urls("mcp.corp", config) == ["https://mcp.corp", "http://mcp.corp"]

    configure_local_zones([".corp"])
    origins = base_urls("mcp.corp", config)
    assert origins[0] == "http://mcp.corp"
    assert any(o.startswith("http://mcp.corp:") for o in origins), "local ports probed"
