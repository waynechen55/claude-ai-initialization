"""Model catalogue parsing, tier selection and Vertex endpoint choice."""

from __future__ import annotations

import os

from initialize_claude_ai.core.config import Settings, load_settings
from initialize_claude_ai.provision.models import (
    DEFAULT_TIERS,
    ModelCatalog,
    ModelInfo,
    _vertex_base_url,
    _vertex_model,
)


def entry(name: str, version: str = "", stage: str = "GA") -> dict:
    return {
        "name": f"publishers/anthropic/models/{name}",
        "versionId": version,
        "launchStage": stage,
    }


def test_vertex_entry_becomes_a_qualified_id() -> None:
    model = _vertex_model(entry("claude-sonnet-4-5", "20250929"), publisher="anthropic", separator="@")
    assert model is not None
    assert model.id == "claude-sonnet-4-5"
    assert model.qualified == "claude-sonnet-4-5@20250929"


def test_placeholder_version_is_dropped() -> None:
    """Verified live: newer models report versionId 'default', which is not an
    address the backend accepts -- they are used by bare id."""
    model = _vertex_model(
        entry("claude-opus-5", "default"),
        publisher="anthropic",
        separator="@",
        placeholders=frozenset({"default"}),
    )
    assert model is not None and model.qualified == "claude-opus-5"


def test_malformed_entries_are_ignored() -> None:
    assert _vertex_model({}, publisher="anthropic", separator="@") is None
    assert _vertex_model("not a dict", publisher="anthropic", separator="@") is None


def test_tier_detection() -> None:
    assert ModelInfo(id="claude-opus-5").tier(DEFAULT_TIERS) == "opus"
    assert ModelInfo(id="claude-haiku-4-5").tier(DEFAULT_TIERS) == "haiku"
    assert ModelInfo(id="gemini-3").tier(DEFAULT_TIERS) == ""


def test_best_for_tier_prefers_the_newer_generation() -> None:
    catalog = ModelCatalog(
        backend="vertex",
        models=[
            ModelInfo(id="claude-sonnet-4-5", version="20250929", launch_stage="GA"),
            ModelInfo(id="claude-sonnet-5", launch_stage="GA"),
        ],
    )
    catalog.models.sort(key=ModelInfo.sort_key, reverse=True)
    best = catalog.best_for_tier("sonnet")
    assert best is not None and best.id == "claude-sonnet-5"


def test_general_availability_outranks_preview() -> None:
    preview = ModelInfo(id="claude-opus-5", launch_stage="PUBLIC_PREVIEW")
    ga = ModelInfo(id="claude-opus-4-8", launch_stage="GA")
    assert max([preview, ga], key=ModelInfo.sort_key) is ga


def test_find_matches_with_or_without_the_version_suffix() -> None:
    catalog = ModelCatalog(backend="vertex", models=[ModelInfo(id="claude-sonnet-4-5", version="20250929")])
    assert catalog.find("claude-sonnet-4-5") is not None
    assert catalog.find("claude-sonnet-4-5@20250929") is not None
    assert catalog.find("claude-sonnet-9") is None


def test_multi_region_locations_use_the_global_host(sandbox) -> None:
    """Verified live: us-aiplatform.googleapis.com 404s every route, while the
    global host serves the catalogue for the 'us' location."""
    config = load_settings(environ=dict(os.environ)).section("provision.vertex")
    assert _vertex_base_url(config, "us") == "https://aiplatform.googleapis.com"
    assert _vertex_base_url(config, "europe") == "https://aiplatform.googleapis.com"
    assert _vertex_base_url(config, "global") == "https://aiplatform.googleapis.com"
    assert _vertex_base_url(config, "us-east5") == "https://us-east5-aiplatform.googleapis.com"


def test_endpoint_templates_are_configuration_not_code() -> None:
    config = Settings(
        data={
            "endpoint_template": "https://{region}.example.test",
            "global_endpoint_template": "https://example.test",
            "global_regions": ["everywhere"],
        },
        environ={},
    )
    assert _vertex_base_url(config, "here") == "https://here.example.test"
    assert _vertex_base_url(config, "everywhere") == "https://example.test"


def test_the_code_tier_lists_are_the_configured_one(settings) -> None:
    """Both exist only because they are needed before settings load."""
    from initialize_claude_ai.cli import MODEL_TIER_FLAGS

    assert tuple(settings.str_list("provision.tiers")) == DEFAULT_TIERS
    assert set(MODEL_TIER_FLAGS) <= set(settings.str_list("provision.tiers"))
