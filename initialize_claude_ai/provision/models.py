"""Live model discovery for the provisioning backends.

Nothing about the model line-up is assumed: the catalogue is fetched from the
provider that will actually serve the requests, so a newly released model is
picked up without a code change and a retired one stops being offered.

Two providers are implemented:

``vertex``
    ``GET {region}-aiplatform.googleapis.com/v1beta1/publishers/{publisher}/models``
    Verified against the live API -- the ``v1`` route 404s, only ``v1beta1``
    serves the publisher catalogue.  Responses look like
    ``{"publisherModels": [{"name": "publishers/anthropic/models/claude-sonnet-4-5",
    "versionId": "20250929", "launchStage": "GA"}, ...], "nextPageToken": "..."}``.

``anthropic-api``
    ``GET {base_url}/v1/models`` -- ``{"data": [{"id", "display_name"}],
    "has_more", "last_id"}``.

The access token is obtained through :class:`~initialize_claude_ai.core.secrets.Secret`
so it is registered with the redaction filter the instant it exists, and it only
ever leaves this module inside an ``Authorization`` header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from ..core.config import Settings, fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger

log = get_logger("provision.models")

#: Digit runs inside a model id, e.g. "claude-sonnet-4-5" -> ("4", "5").
_DIGIT_RUN = re.compile(r"(\d+)")

#: Fallback tier vocabulary; the real list comes from ``provision.tiers``.
DEFAULT_TIERS = ("opus", "sonnet", "haiku")


@dataclass
class ModelInfo:
    """One model as the provider describes it."""

    id: str
    version: str = ""
    launch_stage: str = ""
    display_name: str = ""
    publisher: str = ""
    separator: str = "@"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """The id in the form the backend expects, version suffix included."""
        if self.version and self.separator:
            return f"{self.id}{self.separator}{self.version}"
        return self.id

    def tier(self, tiers: Sequence[str] = DEFAULT_TIERS) -> str:
        low = self.id.lower()
        for name in tiers:
            if name.lower() in low:
                return name
        return ""

    def sort_key(self) -> tuple:
        """Newest and most generally-available first when reversed."""
        ga = 1 if self.launch_stage.upper() in ("GA", "GENERAL_AVAILABILITY") else 0
        # Generation digits inside the id ("claude-sonnet-4-5" -> (4, 5)) beat the
        # release date, so a newer family always outranks an older one.
        digits = tuple(int(part) for part in _DIGIT_RUN.findall(self.id))
        return (ga, digits, self.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "qualified": self.qualified,
            "launch_stage": self.launch_stage,
            "display_name": self.display_name,
            "publisher": self.publisher,
        }


@dataclass
class ModelCatalog:
    """The result of asking a backend what it can serve."""

    backend: str
    endpoint: str = ""
    models: list[ModelInfo] = field(default_factory=list)
    ok: bool = False
    detail: str = ""
    remediation: list[str] = field(default_factory=list)
    tiers: tuple[str, ...] = DEFAULT_TIERS

    def __bool__(self) -> bool:
        return bool(self.models)

    def ids(self) -> list[str]:
        return [m.id for m in self.models]

    def qualified_ids(self) -> list[str]:
        return [m.qualified for m in self.models]

    def by_tier(self) -> dict[str, list[ModelInfo]]:
        grouped: dict[str, list[ModelInfo]] = {}
        for model in self.models:
            tier = model.tier(self.tiers)
            if tier:
                grouped.setdefault(tier, []).append(model)
        for models in grouped.values():
            models.sort(key=ModelInfo.sort_key, reverse=True)
        return grouped

    def best_for_tier(self, tier: str) -> ModelInfo | None:
        candidates = self.by_tier().get(tier, [])
        return candidates[0] if candidates else None

    def find(self, wanted: str) -> ModelInfo | None:
        """Match a model id with or without its version suffix."""
        if not wanted:
            return None
        target = wanted.strip().lower()
        bare = target.split("@", 1)[0]
        for model in self.models:
            if target in (model.id.lower(), model.qualified.lower()):
                return model
        for model in self.models:
            if bare == model.id.lower():
                return model
        return None

    def add_remediation(self, *lines: str) -> None:
        for line in lines:
            if line and line not in self.remediation:
                self.remediation.append(line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "endpoint": self.endpoint,
            "ok": self.ok,
            "detail": self.detail,
            "count": len(self.models),
            "models": [m.to_dict() for m in self.models],
        }


# ----------------------------------------------------------------------------
# Vertex AI
# ----------------------------------------------------------------------------


def discover_vertex_models(
    ctx: RunContext,
    config: Settings,
    *,
    project: str,
    region: str,
    tiers: Sequence[str] = DEFAULT_TIERS,
) -> ModelCatalog:
    """List the publisher models the configured Vertex region will serve."""
    catalog = ModelCatalog(backend="vertex", tiers=tuple(tiers))
    publisher = config.str_("publisher", "anthropic")

    base = _vertex_base_url(config, region)
    path = _render(
        config.str_("models_path_template", "/v1beta1/publishers/{publisher}/models"),
        {"publisher": publisher, "project": project, "region": region},
    )
    catalog.endpoint = f"{base}{path}"

    token = ctx.secrets.first_available(
        config.str_list("token_refs", ["gcloud-adc", "gcloud-token"]),
        label="Google Cloud access token",
    )
    if token is None:
        catalog.detail = "no Google Cloud access token available"
        catalog.add_remediation(
            "Authenticate first: initialize-claude-ai run --phases 2 --auth-backend google-cloud",
            "Or directly: gcloud auth application-default login --no-launch-browser",
        )
        return catalog

    headers = {"Authorization": f"Bearer {token.reveal()}", "accept": "application/json"}
    quota_project = config.str_("quota_project") or project
    if quota_project:
        # Without this the call is billed to the token's own project, which a
        # service account may not have.
        headers["x-goog-user-project"] = quota_project

    timeout = ctx.settings.float_("provision.model_discovery_timeout_seconds", 30)

    # A location that is not a regional endpoint (or one whose host does not
    # serve the catalogue) is answered by the global host instead.
    endpoints = [catalog.endpoint]
    if config.bool_("fallback_to_global_catalogue", True):
        fallback = (
            _render(config.str_("global_endpoint_template", "https://aiplatform.googleapis.com"), {}).rstrip("/") + path
        )
        if fallback not in endpoints:
            endpoints.append(fallback)

    collected: list[ModelInfo] = []
    with ctx.http(timeout=timeout) as client:
        while True:
            endpoint = endpoints.pop(0)
            collected, failure = _fetch_vertex_catalogue(
                client, endpoint, headers, config=config, publisher=publisher, timeout=timeout
            )
            catalog.endpoint = endpoint
            if failure is None:
                break
            # Only a missing route is worth re-asking elsewhere: 401/403/5xx say
            # the same thing on every host and retrying just doubles the wait.
            if not endpoints or getattr(failure, "status", 0) != 404:
                catalog.detail = _http_failure(failure, endpoint)
                catalog.add_remediation(*_vertex_remediation(failure, project, region))
                return catalog
            log.debug("no publisher catalogue at %s; trying %s", endpoint, endpoints[0])

    collected.sort(key=ModelInfo.sort_key, reverse=True)
    catalog.models = collected
    catalog.ok = True
    catalog.detail = f"{len(collected)} model(s) offered by {region}"
    if not collected:
        catalog.detail = f"{region} offers no {publisher} models to this project"
        catalog.add_remediation(
            f"Request access to {publisher} models in Vertex AI Model Garden, "
            "or choose a region that already serves them.",
        )
    return catalog


def _fetch_vertex_catalogue(
    client: Any,
    endpoint: str,
    headers: dict[str, str],
    *,
    config: Settings,
    publisher: str,
    timeout: float,
) -> tuple[list[ModelInfo], Any]:
    """Page through one publisher catalogue.

    Returns ``(models, None)`` on success, or ``([], response)`` for the
    response that ended it -- the caller decides whether another host is worth
    asking.
    """
    page_size = config.int_("models_page_size", 200)
    separator = config.str_("model_version_separator", "@")
    accepted = {s.upper() for s in config.str_list("accepted_launch_stages")}
    placeholders = frozenset(v.strip().lower() for v in config.str_list("placeholder_versions"))

    collected: list[ModelInfo] = []
    page_token = ""
    for _ in range(config.int_("max_pages", 10)):
        url = _with_params(endpoint, {"pageSize": page_size, "pageToken": page_token})
        response = client.get(url, headers=headers, timeout=timeout)
        if not response.ok:
            return [], response
        payload = response.json()
        if not isinstance(payload, dict):
            return [], response
        for entry in payload.get("publisherModels", []):
            model = _vertex_model(entry, publisher=publisher, separator=separator, placeholders=placeholders)
            if model is None:
                continue
            if accepted and model.launch_stage.upper() not in accepted:
                continue
            collected.append(model)
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            break
    return collected, None


def _vertex_model(
    entry: Any, *, publisher: str, separator: str, placeholders: frozenset[str] = frozenset()
) -> ModelInfo | None:
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name", ""))
    if not name:
        return None
    # "publishers/anthropic/models/claude-sonnet-4-5" -> "claude-sonnet-4-5"
    model_id = name.rsplit("/", 1)[-1]
    version = str(entry.get("versionId", ""))
    if version.strip().lower() in placeholders:
        version = ""  # a placeholder version is not part of the address
    return ModelInfo(
        id=model_id,
        version=version,
        launch_stage=str(entry.get("launchStage", "")),
        publisher=publisher,
        separator=separator,
        raw={k: entry.get(k) for k in ("name", "versionId", "launchStage") if k in entry},
    )


def _vertex_base_url(config: Settings, region: str) -> str:
    """Regional endpoints are per-region; 'global' has its own hostname."""
    region = (region or "").strip()
    global_names = {r.strip().lower() for r in config.str_list("global_regions")}
    if not region or region.lower() in global_names:
        template = config.str_("global_endpoint_template", "https://aiplatform.googleapis.com")
    else:
        template = config.str_("endpoint_template", "https://{region}-aiplatform.googleapis.com")
    return _render(template, {"region": region}).rstrip("/")


def _vertex_remediation(response: Any, project: str, region: str) -> list[str]:
    status = getattr(response, "status", 0)
    if status in (401, 403):
        return [
            "The token was rejected. Refresh it: gcloud auth application-default login",
            f"Confirm the caller has aiplatform.endpoints.predict on project {project or '<unset>'}.",
        ]
    if status == 404:
        return [
            f"No publisher catalogue at that path for region {region!r}.",
            "Check provision.vertex.region against provision.vertex.known_regions.",
        ]
    if status == 429:
        return ["Vertex AI is throttling the catalogue call; retry shortly."]
    if status == 0:
        return ["Could not reach the Vertex endpoint; check egress and proxy settings."]
    return []


# ----------------------------------------------------------------------------
# Direct Anthropic API
# ----------------------------------------------------------------------------


def discover_anthropic_models(
    ctx: RunContext,
    config: Settings,
    *,
    tiers: Sequence[str] = DEFAULT_TIERS,
) -> ModelCatalog:
    """List the models the API key is entitled to."""
    catalog = ModelCatalog(backend="anthropic-api", tiers=tuple(tiers))
    base = config.str_("base_url", "https://api.anthropic.com").rstrip("/")
    path = config.str_("models_path", "/v1/models")
    catalog.endpoint = f"{base}{path}"

    secret = ctx.state.get("anthropic_api_key")
    if secret is None:
        secret = ctx.secrets.first_available(
            ctx.settings.str_list("auth.anthropic_api.api_key_refs", ["env:ANTHROPIC_API_KEY"]),
            label="Anthropic API key",
        )
    if secret is None:
        catalog.detail = "no Anthropic API key available"
        catalog.add_remediation("Expose a key through auth.anthropic_api.api_key_refs (env:, file:, keyring:).")
        return catalog

    headers = {
        "x-api-key": secret.reveal(),
        "anthropic-version": config.str_("version_header", "2023-06-01"),
        "accept": "application/json",
    }
    limit = config.int_("models_page_size", 100)
    timeout = ctx.settings.float_("provision.model_discovery_timeout_seconds", 30)

    collected: list[ModelInfo] = []
    after_id = ""
    with ctx.http(timeout=timeout) as client:
        for _ in range(config.int_("max_pages", 10)):
            url = _with_params(catalog.endpoint, {"limit": limit, "after_id": after_id})
            response = client.get(url, headers=headers, timeout=timeout)
            if not response.ok:
                catalog.detail = _http_failure(response, catalog.endpoint)
                if response.status in (401, 403):
                    catalog.add_remediation("The API key was rejected; check that it is active.")
                return catalog
            payload = response.json()
            if not isinstance(payload, dict):
                catalog.detail = f"unexpected response body from {catalog.endpoint}"
                return catalog
            entries = payload.get("data", [])
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                collected.append(
                    ModelInfo(
                        id=str(entry["id"]),
                        display_name=str(entry.get("display_name", "")),
                        launch_stage="GA",
                        separator="",
                        raw={k: entry.get(k) for k in ("id", "display_name", "created_at") if k in entry},
                    )
                )
            if not payload.get("has_more") or not payload.get("last_id"):
                break
            after_id = str(payload["last_id"])

    collected.sort(key=ModelInfo.sort_key, reverse=True)
    catalog.models = collected
    catalog.ok = True
    catalog.detail = f"{len(collected)} model(s) available to this key"
    return catalog


# ----------------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------------

_render = fill


def _with_params(url: str, params: dict[str, Any]) -> str:
    """Append non-empty query parameters, preserving any already in ``url``."""
    pairs = [f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v not in (None, "", 0)]
    if not pairs:
        return url
    parts = urlsplit(url)
    query = "&".join([p for p in (parts.query, *pairs) if p])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _http_failure(response: Any, endpoint: str) -> str:
    status = getattr(response, "status", 0)
    if status == 0:
        return f"could not reach {endpoint}: {getattr(response, 'error', 'network error')}"
    if 200 <= status < 400:  # reached, answered, but not with a catalogue
        return f"unexpected response body from {endpoint}"
    return f"{endpoint} returned HTTP {status}"


def discover(
    ctx: RunContext,
    backend: str,
    *,
    project: str = "",
    region: str = "",
    tiers: Sequence[str] = DEFAULT_TIERS,
) -> ModelCatalog:
    """Dispatch to the right discovery routine for ``backend``."""
    if backend == "vertex":
        return discover_vertex_models(
            ctx,
            ctx.settings.section("provision.vertex"),
            project=project,
            region=region,
            tiers=tiers,
        )
    if backend == "anthropic-api":
        return discover_anthropic_models(ctx, ctx.settings.section("provision.anthropic_api"), tiers=tiers)
    catalog = ModelCatalog(backend=backend, tiers=tuple(tiers))
    catalog.detail = f"model discovery is not implemented for backend {backend!r}"
    return catalog


__all__ = [
    "ModelInfo",
    "ModelCatalog",
    "discover",
    "discover_vertex_models",
    "discover_anthropic_models",
    "DEFAULT_TIERS",
]
