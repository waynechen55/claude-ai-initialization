"""Stage 2's PyPI route -- the one source that has no search API.

The official registry, npm and GitHub all answer a *query*: ask them about
"example mcp server" and they say what they have. PyPI does not. Its JSON
endpoint answers about one exact distribution name and nothing else, so a
Python-published MCP server is found only if the right name is asked about.

That makes the name templates load-bearing in a way a search query is not: get
them wrong and the stage reports "no published MCP server matched this target"
for a server that is sitting on PyPI under a name nobody tried. These tests pin
down which names are asked, how many, what turns the route off, and that a hit
comes back launchable rather than as a lead to read later.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery.registry_client import PackageHit, RegistryClient
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

TARGET = "https://api.example.com/v1"

#: The product name ``TARGET`` reduces to, rendered through the packaged
#: templates. Spelled out rather than derived, so that a change to either the
#: templates or the token rule is a test failure and not a silent agreement.
DEFAULT_NAMES = ["mcp-server-example", "example-mcp-server", "mcp-example", "example-mcp"]


def pypi_url(name: str) -> str:
    return f"https://pypi.org/pypi/{name}/json"


def published(name: str, *, summary: str = "An MCP server for Example", version: str = "2.1.0") -> Response:
    """PyPI's answer for a distribution that exists."""
    body = {
        "info": {
            "name": name,
            "summary": summary,
            "version": version,
            "project_url": f"https://pypi.org/project/{name}/",
            "requires_python": ">=3.10",
        }
    }
    return Response(url=pypi_url(name), status=200, text=json.dumps(body))


class FakeIndex:
    """An HTTP client answering from a table, recording every URL it was asked.

    Anything not in the table is a 404, which is what PyPI says about a name
    nobody has published -- the case the whole route has to survive.
    """

    def __init__(self, answers: dict[str, Response]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __enter__(self) -> "FakeIndex":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def get(self, url: str, **_kwargs: object) -> Response:
        self.asked.append(url)
        return self.answers.get(url, Response(url=url, status=404))


def client(ctx, monkeypatch, answers: dict[str, Response], **config) -> tuple[RegistryClient, FakeIndex]:
    """A registry client wired to a fake index.

    With no ``config`` the packaged defaults apply, which is the configuration
    an operator actually runs; pass keys to test what a different one does.
    """
    index = FakeIndex(answers)
    monkeypatch.setattr(type(ctx), "http", lambda _self, **_k: index)
    section = ctx.settings.section("mcp.packages")
    if config:
        section = Settings(data={**section.data, **config}, environ=section.environ)
    return RegistryClient(ctx, section), index


# ----------------------------------------------------------------------------
# the names that get asked about
# ----------------------------------------------------------------------------


def test_the_names_asked_about_come_from_the_product_name(ctx, monkeypatch) -> None:
    registry, _ = client(ctx, monkeypatch, {})
    assert registry.pypi_names(TARGET) == DEFAULT_NAMES


def test_a_name_is_normalised_the_way_pypi_normalises_it(ctx, monkeypatch) -> None:
    """PEP 503: ``MCP_Server.Example`` and ``mcp-server-example`` are one name.

    Asking for both would be two round trips for one answer, and the second
    spelling is the only one the index actually responds to."""
    registry, _ = client(ctx, monkeypatch, {}, pypi_name_templates=["MCP_Server.{target}", "mcp--server--{target}"])
    assert registry.pypi_names(TARGET) == ["mcp-server-example"]


def test_the_lookups_one_target_costs_are_capped(ctx, monkeypatch) -> None:
    """Each name is a separate round trip; a long template list is not a licence
    to spend a dozen of them on one host."""
    registry, index = client(
        ctx,
        monkeypatch,
        {},
        pypi_name_templates=["mcp-a-{target}", "mcp-b-{target}", "mcp-c-{target}", "mcp-d-{target}"],
        pypi_max_lookups=2,
    )
    assert registry.pypi_names(TARGET) == ["mcp-a-example", "mcp-b-example"]
    registry.search_pypi(TARGET)
    assert len(index.asked) == 2


# ----------------------------------------------------------------------------
# asking
# ----------------------------------------------------------------------------


def test_a_python_published_server_is_found(ctx, monkeypatch) -> None:
    registry, index = client(ctx, monkeypatch, {pypi_url("mcp-server-example"): published("mcp-server-example")})

    hits = registry.search_pypi(TARGET)

    assert [(h.name, h.source, h.version) for h in hits] == [("mcp-server-example", "pypi", "2.1.0")]
    assert index.asked == [pypi_url(name) for name in DEFAULT_NAMES]


def test_a_name_nobody_published_is_not_a_hit(ctx, monkeypatch) -> None:
    registry, index = client(ctx, monkeypatch, {})
    assert registry.search_pypi(TARGET) == []
    assert len(index.asked) == len(DEFAULT_NAMES)


def test_without_a_pypi_url_pypi_is_never_asked(ctx, monkeypatch) -> None:
    """The route is disabled by emptying its URL -- and disabled means no
    traffic, not traffic whose answer is thrown away."""
    registry, index = client(ctx, monkeypatch, {}, pypi_package_url="")
    assert registry.search_pypi(TARGET) == []
    assert index.asked == []


def test_an_empty_template_list_also_turns_the_route_off(ctx, monkeypatch) -> None:
    registry, index = client(ctx, monkeypatch, {}, pypi_name_templates=[])
    assert registry.pypi_names(TARGET) == []
    assert registry.search_pypi(TARGET) == []
    assert index.asked == []


def test_an_answer_that_is_not_a_package_is_not_a_hit(ctx, monkeypatch) -> None:
    """A proxy or captive portal answering 200 with an HTML page must not become
    a package the run then tries to launch."""
    url = pypi_url("mcp-server-example")
    registry, _ = client(ctx, monkeypatch, {url: Response(url=url, status=200, text="<html>hello</html>")})
    assert registry.search_pypi(TARGET) == []


# ----------------------------------------------------------------------------
# what a hit becomes
# ----------------------------------------------------------------------------


def candidates_for(ctx, monkeypatch, hit: PackageHit):
    registry, _ = client(ctx, monkeypatch, {})
    return registry.to_candidates(hit)


def test_a_python_package_is_launched_with_uvx(ctx, monkeypatch) -> None:
    hit = PackageHit(name="mcp-server-example", source="pypi", description="An MCP server", version="2.1.0")
    [candidate] = candidates_for(ctx, monkeypatch, hit)
    assert candidate.command == ["uvx", "mcp-server-example"]
    assert candidate.kind == "stdio"
    assert "mcp-server-example" in candidate.source


def test_a_node_package_is_still_launched_with_npx(ctx, monkeypatch) -> None:
    hit = PackageHit(name="@vendor/mcp-server", source="npm", score=0.8)
    [candidate] = candidates_for(ctx, monkeypatch, hit)
    assert candidate.command == ["npx", "-y", "@vendor/mcp-server"]


def test_a_repository_is_a_lead_and_not_a_launch(ctx, monkeypatch) -> None:
    """GitHub has no launcher, and inventing one would mean running a command
    that was never published."""
    hit = PackageHit(name="owner/mcp-example", source="github", url="https://github.test/owner/mcp-example")
    [candidate] = candidates_for(ctx, monkeypatch, hit)
    assert candidate.command == []
    assert any("clone it" in note for note in candidate.notes)


def test_an_ecosystem_with_no_launcher_configured_is_not_guessed_at(ctx, monkeypatch) -> None:
    registry, _ = client(ctx, monkeypatch, {}, registry_launchers={"npm": "npx"})
    [candidate] = registry.to_candidates(PackageHit(name="mcp-server-example", source="pypi"))
    assert candidate.command == []


# ----------------------------------------------------------------------------
# alongside the other three sources
# ----------------------------------------------------------------------------


def npm_answer(url: str, names: list[str]) -> Response:
    objects = [{"package": {"name": name, "version": "1.0.0"}, "score": {"final": 0.5}} for name in names]
    return Response(url=url, status=200, text=json.dumps({"objects": objects}))


def npm_url(ctx, query: str) -> str:
    section = ctx.settings.section("mcp.packages")
    template = section.str_("npm_search_url")
    return template.replace("{query}", quote(query, safe="")).replace("{limit}", str(section.int_("limit", 10)))


def test_a_python_server_ranks_below_a_node_one_and_above_a_repository(ctx, monkeypatch) -> None:
    """Ordering is by source first: npm is where most servers live, PyPI is a
    real package too, a repository is only a lead."""
    answers = {
        npm_url(ctx, "example mcp server"): npm_answer(npm_url(ctx, "example mcp server"), ["node-mcp"]),
        pypi_url("mcp-server-example"): published("mcp-server-example"),
    }
    registry, _ = client(ctx, monkeypatch, answers)
    assert [hit.source for hit in registry.search_all(TARGET)] == ["npm", "pypi"]


def test_pypi_is_still_asked_when_the_searches_found_plenty(ctx, monkeypatch) -> None:
    """The query loop stops early once the searches have turned up enough. PyPI
    costs the same short list of lookups either way, and a Python-published
    server must not be invisible to a run that happened to find npm packages
    first."""
    url = npm_url(ctx, "example mcp server")
    plenty = [f"mcp-server-{n}" for n in range(60)]
    answers = {url: npm_answer(url, plenty), pypi_url("mcp-server-example"): published("mcp-server-example")}
    registry, index = client(ctx, monkeypatch, answers)

    sources = {hit.source for hit in registry.search_all(TARGET)}

    assert sources == {"npm", "pypi"}
    assert [u for u in index.asked if u.startswith("https://pypi.org")] == [pypi_url(name) for name in DEFAULT_NAMES]


def test_one_dead_source_does_not_stop_the_others(ctx, monkeypatch) -> None:
    registry, _ = client(ctx, monkeypatch, {pypi_url("mcp-server-example"): published("mcp-server-example")})
    monkeypatch.setattr(
        type(registry), "search_npm", lambda _self, _q: (_ for _ in ()).throw(RuntimeError("npm is down"))
    )
    assert [hit.source for hit in registry.search_all(TARGET)] == ["pypi"]


# ----------------------------------------------------------------------------
# the stage
# ----------------------------------------------------------------------------


@pytest.fixture
def pipeline(ctx, monkeypatch):
    def build(answers: dict[str, Response]) -> tuple[TraditionalPipeline, FakeIndex]:
        index = FakeIndex(answers)
        monkeypatch.setattr(type(ctx), "http", lambda _self, **_k: index)
        return TraditionalPipeline(ctx), index

    return build


def test_the_package_stage_offers_a_python_server_it_found(pipeline) -> None:
    searcher, _ = pipeline({pypi_url("mcp-server-example"): published("mcp-server-example")})

    stage = searcher.stage_package(TargetOutcome(target=TARGET))

    assert ["uvx", "mcp-server-example"] in [c.command for c in stage.candidates]
    assert any(hit["source"] == "pypi" for hit in stage.findings["search_hits"])


def test_the_stage_says_which_names_it_asked_pypi_about(pipeline) -> None:
    """ "No published MCP server matched this target" is only a useful answer if
    the reader can see what was asked -- a search hit carries its own query,
    but a lookup that matched nothing leaves no trace at all."""
    searcher, _ = pipeline({})

    stage = searcher.stage_package(TargetOutcome(target=TARGET))

    assert stage.findings["pypi_names"] == DEFAULT_NAMES
