"""A target is searched for by the kind of API it serves, not only by its name.

smelt.example.net serves GraphQL and nothing else: no MCP endpoint, no OpenAPI
document, no server named after it. Discovery reported "the target publishes no
machine-readable API description" and searched the registries for "smelt",
while ``mcp-graphql`` -- a generic server for any GraphQL API -- works once its
ENDPOINT points at the target. The operator had registered it by hand.
"""

from __future__ import annotations

import json

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery import api_kinds
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
)

SCHEMA = json.dumps({"data": {"__schema": {"queryType": {"name": "Query"}}}})
FOUND = {"graphql": {"label": "GraphQL", "url": "https://smelt.example.test/graphql/"}}


def reply(url: str, status: int = 200, text: str = "", content_type: str = "application/json") -> Response:
    return Response(url=url, status=status, headers={"content-type": content_type}, text=text)


class Server:
    """Answers the recognising request at ``answers`` and 404s everywhere else."""

    def __init__(self, answers: dict[str, str], pages: dict[str, str] | None = None, status: int = 404) -> None:
        self.answers, self.pages, self.status, self.sent = answers, pages or {}, status, []

    def send(self, method, url, content_type, body):
        self.sent.append((method, url, content_type, body))
        if url in self.answers:
            return reply(url, text=self.answers[url])
        return reply(url, status=self.status, text="")

    def fetch(self, url):
        if url in self.pages:
            return reply(url, text=self.pages[url], content_type="text/html")
        return reply(url, status=404)


def graphql(ctx) -> api_kinds.ApiKind:
    return next(k for k in api_kinds.kinds(ctx.settings) if k.name == "graphql")


# -- configuration -----------------------------------------------------------------


def test_kinds_come_from_configuration(ctx) -> None:
    kinds = {k.name: k for k in api_kinds.kinds(ctx.settings)}
    assert kinds["graphql"].probed and kinds["graphql"].adapters[0].package == "mcp-graphql"
    assert not kinds["openapi"].probed and kinds["openapi"].finding == "openapi_url"


def test_a_new_kind_is_one_table(ctx) -> None:
    ctx.settings.set(
        "mcp.discovery.api_kinds.odata",
        {"label": "OData", "paths": ["/odata/$metadata"], "method": "GET", "success_markers": ["edmx:Edmx"]},
    )
    odata = next(k for k in api_kinds.kinds(ctx.settings) if k.name == "odata")
    server = Server({"https://x.example.test/odata/$metadata": "<edmx:Edmx/>"})
    assert api_kinds.detect(odata, "https://x.example.test", send=server.send, fetch=server.fetch)
    assert server.sent[0][0] == "GET"


# -- recognising one ---------------------------------------------------------------


def test_introspection_is_what_counts(ctx) -> None:
    server = Server({"https://smelt.example.test/graphql/": SCHEMA})
    url = api_kinds.detect(graphql(ctx), "https://smelt.example.test", send=server.send, fetch=server.fetch)
    assert url == "https://smelt.example.test/graphql/"
    method, _, content_type, body = server.sent[-1]
    assert method == "POST" and content_type == "application/json" and "__schema" in body


def test_a_page_that_merely_exists_is_not_graphql(ctx) -> None:
    server = Server({}, status=200)
    assert api_kinds.detect(graphql(ctx), "https://x.example.test", send=server.send, fetch=server.fetch) == ""


def test_an_unreachable_host_costs_one_request(ctx) -> None:
    server = Server({}, status=0)
    assert api_kinds.detect(graphql(ctx), "https://x.example.test", send=server.send, fetch=server.fetch) == ""
    assert len(server.sent) == 1


def test_an_ide_page_names_the_endpoint(ctx) -> None:
    page = '<script>GraphiQL.createFetcher({ url: "/internal/graphql" })</script>'
    server = Server(
        {"https://x.example.test/internal/graphql": SCHEMA},
        pages={"https://x.example.test/graphiql": page},
    )
    url = api_kinds.detect(graphql(ctx), "https://x.example.test", send=server.send, fetch=server.fetch)
    assert url == "https://x.example.test/internal/graphql"


def test_an_ide_page_pointing_elsewhere_is_ignored(ctx) -> None:
    page = '<script>fetcher({ url: "https://demo.example.org/graphql" })</script>'
    server = Server(
        {"https://demo.example.org/graphql": SCHEMA},
        pages={"https://x.example.test/graphiql": page},
    )
    assert api_kinds.detect(graphql(ctx), "https://x.example.test", send=server.send, fetch=server.fetch) == ""


def test_described_reads_both_shapes_and_the_openapi_finding(ctx) -> None:
    findings = {
        "api_kinds": {"graphql": "https://x.example.test/graphql"},
        "openapi_url": "https://x.example.test/openapi.json",
    }
    found = api_kinds.described(ctx.settings, findings, host="x.example.test")
    assert found["graphql"]["url"] == "https://x.example.test/graphql"
    assert found["openapi"] == {"label": "OpenAPI", "url": "https://x.example.test/openapi.json"}


def test_described_drops_another_hosts_api(ctx) -> None:
    findings = {"api_kinds": {"graphql": {"url": "https://elsewhere.example.org/graphql"}}}
    assert api_kinds.described(ctx.settings, findings, host="x.example.test") == {}


# -- the generic servers -------------------------------------------------------------


class Registry:
    def __init__(self, version: str = "2.0.4") -> None:
        self.version, self.asked = version, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **_kwargs):
        self.asked.append(url)
        return reply(url, text=json.dumps({"version": self.version}))


def test_an_adapter_is_pinned_and_pointed_at_the_api(ctx, monkeypatch) -> None:
    registry = Registry()
    monkeypatch.setattr(ctx, "http", lambda **_k: registry)
    [candidate] = api_kinds.adapter_candidates(ctx, "smelt.example.test", FOUND)
    assert candidate.full_command() == ["npx", "-y", "mcp-graphql@2.0.4"]
    assert candidate.env == {"ENDPOINT": "https://smelt.example.test/graphql/"}
    assert candidate.origin == Stage.PACKAGE
    assert registry.asked == ["https://registry.npmjs.org/mcp-graphql/latest"]
    api_kinds.adapter_candidates(ctx, "smelt.example.test", FOUND)
    assert len(registry.asked) == 1, "looked up once per run"


def test_pinning_can_be_turned_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.packages.pin_versions", False)
    monkeypatch.setattr(ctx, "http", lambda **_k: Registry())
    [candidate] = api_kinds.adapter_candidates(ctx, "smelt.example.test", FOUND)
    assert candidate.full_command() == ["npx", "-y", "mcp-graphql"]


def test_a_strange_version_is_not_pinned_to(ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx, "http", lambda **_k: Registry("1.0; rm -rf /"))
    [candidate] = api_kinds.adapter_candidates(ctx, "smelt.example.test", FOUND)
    assert candidate.full_command() == ["npx", "-y", "mcp-graphql"]


def test_an_adapter_on_an_unknown_registry_is_skipped(ctx) -> None:
    ctx.settings.set("mcp.discovery.api_kinds.graphql.adapters", [{"package": "x", "registry": "nowhere"}])
    assert api_kinds.adapter_candidates(ctx, "smelt.example.test", FOUND) == []


# -- the pipelines -----------------------------------------------------------------


def proven(candidates, **_kwargs):
    for candidate in candidates:
        candidate.handshake = Handshake(ok=True, round_trip=True, tools=["query-graphql"])
    return list(candidates)


def pipeline(ctx, monkeypatch, found=FOUND) -> TraditionalPipeline:
    ctx.settings.set("mcp.packages.pin_versions", False)
    made = TraditionalPipeline(ctx)
    monkeypatch.setattr(made, "api_kinds_for", lambda target: dict(found))
    monkeypatch.setattr(made.registry, "search_all", lambda target, problems=None: [])
    monkeypatch.setattr(made.registry, "pypi_names", lambda target: [])
    return made


def test_the_package_stage_offers_the_generic_server_with_no_search_hits(ctx, monkeypatch) -> None:
    made = pipeline(ctx, monkeypatch)
    monkeypatch.setattr(made.verifier, "verify_all", proven)
    monkeypatch.setattr(made, "registrable", lambda c, o: True)

    stage = made.stage_package(TargetOutcome(target="smelt.example.test"))

    assert stage.status == Status.OK
    assert stage.verified[0].env["ENDPOINT"] == "https://smelt.example.test/graphql/"
    assert "generic GraphQL server mcp-graphql" in stage.detail


def test_with_no_api_kind_the_package_stage_says_what_it_always_said(ctx, monkeypatch) -> None:
    made = pipeline(ctx, monkeypatch, found={})
    stage = made.stage_package(TargetOutcome(target="x.example.test"))
    assert stage.detail == "no published MCP server matched this target"


def test_the_bridge_stage_names_the_api_instead_of_denying_one(ctx, monkeypatch) -> None:
    made = pipeline(ctx, monkeypatch)
    stage = made.stage_bridge(TargetOutcome(target="smelt.example.test"))
    assert (
        stage.findings["bridge_detail"] == "the target publishes a GraphQL API at https://smelt.example.test/graphql/"
    )
    assert any(
        "GraphQL API at https://smelt.example.test/graphql/ is the thing to bridge" in r for r in stage.remediation
    )
    assert stage.candidates == [], "tier 2 tried the generic servers; they are not trialled twice"


def test_the_bridge_stage_offers_them_when_tier_2_is_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.packages.enabled", False)
    made = pipeline(ctx, monkeypatch)
    monkeypatch.setattr(made.verifier, "verify_all", lambda c, **k: [])
    stage = made.stage_bridge(TargetOutcome(target="smelt.example.test"))
    assert [c.origin for c in stage.candidates] == [Stage.BRIDGE]


def test_the_playbook_does_not_say_there_is_no_api(ctx, monkeypatch) -> None:
    made = pipeline(ctx, monkeypatch)
    said = " ".join(made._proposals(TargetOutcome(target="smelt.example.test"), {}))
    assert "No API description was found" not in said
    assert "serves a GraphQL API at https://smelt.example.test/graphql/" in said


def test_without_one_the_playbook_is_unchanged(ctx, monkeypatch) -> None:
    made = pipeline(ctx, monkeypatch, found={})
    said = " ".join(made._proposals(TargetOutcome(target="x.example.test"), {}))
    assert "No API description was found on" in said


def test_an_agent_run_looks_once_per_address(ctx, monkeypatch) -> None:
    made = TraditionalPipeline(ctx)
    calls = []
    monkeypatch.setattr(made, "_find_api_kinds", lambda target, *a: calls.append(target.target) or dict(FOUND))
    target = TargetOutcome(target="smelt.example.test")
    assert made.api_kinds_for(target)["graphql"]["url"] == FOUND["graphql"]["url"]
    made.api_kinds_for(target)
    assert calls == ["smelt.example.test"]


def test_an_endpoint_stage_that_looked_is_not_repeated(ctx, monkeypatch) -> None:
    made = TraditionalPipeline(ctx)
    monkeypatch.setattr(made, "_find_api_kinds", lambda *a: (_ for _ in ()).throw(AssertionError("asked again")))
    target = TargetOutcome(target="x.example.test")
    target.stages.append(StageOutcome(stage=Stage.ENDPOINT, mode=Mode.TRADITIONAL, findings={"origin_status": 200}))
    assert made.api_kinds_for(target) == {}


def test_the_agent_is_told_the_kind_and_the_launch(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.packages.pin_versions", False)
    made = AgenticPipeline(ctx)
    monkeypatch.setattr(made.fallback, "api_kinds_for", lambda target: dict(FOUND))
    target = TargetOutcome(target="smelt.example.test")

    brief = made._api_kind_brief(Stage.PACKAGE, target)

    assert "a GraphQL API at https://smelt.example.test/graphql/" in brief
    assert "(graphql)" in brief
    assert "npx -y mcp-graphql with ENDPOINT=https://smelt.example.test/graphql/" in brief
    assert "{" not in brief.replace('{"', "")
    assert made._api_kind_brief(Stage.ENDPOINT, target) == ""


def test_the_agent_is_told_nothing_when_there_is_nothing(ctx, monkeypatch) -> None:
    made = AgenticPipeline(ctx)
    monkeypatch.setattr(made.fallback, "api_kinds_for", lambda target: {})
    assert made._api_kind_brief(Stage.PACKAGE, TargetOutcome(target="x.example.test")) == ""
