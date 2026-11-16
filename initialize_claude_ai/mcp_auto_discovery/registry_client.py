"""Stage 2: find an MCP server someone has already written.

Four sources, cheapest and most authoritative first:

1. **The official MCP registry** (``registry.modelcontextprotocol.io``).  Its
   entries are structured well enough to build a launch command from directly --
   ``packages[]`` gives the registry type, identifier, runtime hint, transport,
   arguments and environment variables; ``remotes[]`` gives hosted endpoints.
2. **npm** search -- where most community servers are published.
3. **PyPI** lookup -- for ``mcp-server-*`` style Python packages. PyPI has no
   usable search API for this: its JSON endpoint answers about one exact
   distribution name. So the names are *produced* from ``pypi_name_templates``
   and asked about one at a time, which is why this source is a lookup where
   the other three are searches.
4. **GitHub** repository search -- the long tail, and the only source that finds
   a server that was never published anywhere.

Credential handling: registry entries mark environment variables and headers
with ``isSecret``, and those are recorded as *references* on the candidate
(``secret_env``), never as values, so a generated ``.mcp.json`` names the
variable and leaves the operator to supply it.

That flag is somebody else's judgement about this project's second rule, so it
is taken as a hint that can only make us **more** careful: ``true`` is believed,
``false`` is checked against :mod:`.secret_policy` -- the same test the agentic
pipeline applies to what a subagent says. Before that, an entry declaring
``{"name": "Authorization", "value": "Bearer sk-live-...", "isSecret": false}``
would have had its value copied verbatim into ``.mcp.json``, while the identical
header coming from an agent was dropped: one rule with two strengths, and the
weaker one on the path that runs when there is no agent to supervise it.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit

from ..core.config import Settings
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from .address import parse as parse_address
from .grounding import HOSTING_NAMESPACES, mentions_host, text_names
from .secret_policy import holds_a_credential
from .types import Candidate, Stage, Transport

log = get_logger("mcp.registry")

#: Where the sources of one :meth:`RegistryClient.search_all` call report that
#: they could not be asked. A context variable rather than an attribute because
#: one client serves every target, and targets run concurrently.
_PROBLEMS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("registry_problems", default=None)

#: Registry transport strings -> our transport constants.
_TRANSPORT_MAP = {
    "streamable-http": Transport.STREAMABLE_HTTP,
    "streamable_http": Transport.STREAMABLE_HTTP,
    "http": Transport.STREAMABLE_HTTP,
    "sse": Transport.SSE,
    "stdio": Transport.STDIO,
}


@dataclass
class PackageHit:
    """A search result, before it is turned into something launchable."""

    name: str
    source: str
    description: str = ""
    url: str = ""
    version: str = ""
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description[:200],
            "url": self.url,
            "version": self.version,
            "score": round(self.score, 3),
        }


class RegistryClient:
    """Searches the package ecosystems for an MCP server matching a target."""

    def __init__(self, ctx: RunContext, config: Settings | None = None) -> None:
        self.ctx = ctx
        self.config = config or ctx.settings.section("mcp.packages")
        self.limit = self.config.int_("limit", 10)
        self.timeout = self.config.float_("search_timeout_seconds", ctx.settings.float_("http.timeout_seconds", 15.0))
        self.registry_timeout = self.config.float_("registry_timeout_seconds", 30.0) or self.timeout

    # -- queries ---------------------------------------------------------------

    def queries_for(self, target: str) -> list[str]:
        """Render ``query_templates`` for a target, plus the bare target itself.

        For the full-text searches -- npm and GitHub. The official registry is
        asked :meth:`registry_queries_for` instead.
        """
        return self._render(
            target,
            self.config.str_list(
                "query_templates",
                [
                    "{target} mcp server",
                    "mcp-server-{target}",
                    "{target} model context protocol",
                    "@modelcontextprotocol {target}",
                ],
            ),
        )

    def registry_queries_for(self, target: str) -> list[str]:
        """What to ask the official MCP registry, which matches server *names*.

        Its ``search`` parameter is a substring match on the name, not a
        full-text search, so a phrase like "confluence mcp server" -- which no
        server is called -- returns nothing, slowly: measured at 13-15 s for 0
        results, against 7 s for the 10 that "confluence" returns. Every
        full-text template was a question this source cannot answer, and it is
        the one source that publishes launch commands and declared variables.
        """
        return self._render(target, self.config.str_list("registry_query_templates", ["{target}"]))

    def _render(self, target: str, templates: Sequence[str]) -> list[str]:
        tokens = self.product_tokens(target)
        primary = tokens[0]
        out: list[str] = []
        for template in templates:
            rendered = str(template).replace("{target}", primary).strip()
            if rendered and rendered not in out:
                out.append(rendered)
        # Then the bare tokens, most specific first: the organisation is worth
        # one question after the product, not instead of it.
        for token in tokens:
            if token not in out:
                out.append(token)
        return out

    def product_tokens(self, target: str) -> list[str]:
        """The searchable words for a target, honouring configuration."""
        return product_tokens(target, self.config.str_list("generic_host_labels", list(GENERIC_HOST_LABELS)))

    def pypi_names(self, target: str) -> list[str]:
        """The distribution names to ask PyPI about for a target.

        Rendered from ``pypi_name_templates`` and normalised the way PyPI
        normalises a name itself (PEP 503), so that ``MCP_Server.Example`` and
        ``mcp-server-example`` are one question rather than three. Capped by
        ``pypi_max_lookups``: each name is a separate round trip, and a long
        template list should not turn one target into a dozen of them.

        Every template is expected to be MCP-specific -- a bare ``{target}``
        would match an unrelated package that merely shares the product's name,
        which is why none of the packaged defaults is one.
        """
        token = self.product_tokens(target)[0]
        out: list[str] = []
        for template in self.config.str_list(
            "pypi_name_templates",
            [
                "mcp-server-{target}",
                "{target}-mcp-server",
                "mcp-{target}",
                "{target}-mcp",
            ],
        ):
            name = _pypi_normalize(str(template).replace("{target}", token))
            if name and name not in out:
                out.append(name)
        limit = self.config.int_("pypi_max_lookups", 6)
        return out[:limit] if limit > 0 else out

    # -- sources ---------------------------------------------------------------

    def search_official(self, query: str) -> list[PackageHit]:
        base = self.config.str_("registry_url", "https://registry.modelcontextprotocol.io").rstrip("/")
        path = self.config.str_("registry_search_path", "/v0/servers?search={query}&limit={limit}")
        url = base + path.replace("{query}", quote(query, safe="")).replace("{limit}", str(self.limit))

        payload = self._get_json(url, timeout=self.registry_timeout)
        if not isinstance(payload, Mapping):
            return []
        hits: list[PackageHit] = []
        for entry in payload.get("servers", []):
            server = entry.get("server") if isinstance(entry, Mapping) else None
            if not isinstance(server, Mapping) or not server.get("name"):
                continue
            hits.append(
                PackageHit(
                    name=str(server["name"]),
                    source="mcp-registry",
                    description=str(server.get("description", "")),
                    url=str((server.get("repository") or {}).get("url", "")),
                    version=str(server.get("version", "")),
                    # Registry entries are curated: they outrank raw search hits.
                    score=1.0,
                    raw=dict(server),
                )
            )
        return hits

    def search_npm(self, query: str) -> list[PackageHit]:
        template = self.config.str_(
            "npm_search_url", "https://registry.npmjs.org/-/v1/search?text={query}&size={limit}"
        )
        if not template:
            return []
        url = template.replace("{query}", quote(query, safe="")).replace("{limit}", str(self.limit))
        payload = self._get_json(url)
        if not isinstance(payload, Mapping):
            return []
        hits: list[PackageHit] = []
        for obj in payload.get("objects", []):
            package = obj.get("package") if isinstance(obj, Mapping) else None
            if not isinstance(package, Mapping) or not package.get("name"):
                continue
            hits.append(
                PackageHit(
                    name=str(package["name"]),
                    source="npm",
                    description=str(package.get("description", "")),
                    url=str((package.get("links") or {}).get("npm", "")),
                    version=str(package.get("version", "")),
                    score=float((obj.get("score") or {}).get("final", 0.0) or 0.0),
                    raw=dict(package),
                )
            )
        return hits

    def lookup_pypi(self, name: str) -> PackageHit | None:
        template = self.config.str_("pypi_package_url", "https://pypi.org/pypi/{name}/json")
        if not template:
            return None
        payload = self._get_json(template.replace("{name}", quote(name, safe="")))
        if not isinstance(payload, Mapping):
            return None
        info = payload.get("info")
        if not isinstance(info, Mapping):
            return None
        return PackageHit(
            name=str(info.get("name", name)),
            source="pypi",
            description=str(info.get("summary", "")),
            url=str(info.get("project_url") or info.get("package_url") or ""),
            version=str(info.get("version", "")),
            score=0.7,
            raw={"requires_python": info.get("requires_python", "")},
        )

    def search_pypi(self, target: str) -> list[PackageHit]:
        """Every name from :meth:`pypi_names` that PyPI actually publishes.

        Takes a target rather than a query, unlike its three siblings: there is
        nothing to search, only names to try. A name nobody has published
        answers 404, which :meth:`_get_json` reports as nothing.
        """
        hits: list[PackageHit] = []
        for name in self.pypi_names(target):
            hit = self.lookup_pypi(name)
            if hit is not None:
                hits.append(hit)
        return hits

    def search_github(self, query: str) -> list[PackageHit]:
        template = self.config.str_(
            "github_search_url", "https://api.github.com/search/repositories?q={query}&per_page={limit}"
        )
        if not template:
            return []
        url = template.replace("{query}", quote(query, safe="")).replace("{limit}", str(self.limit))
        headers = {"accept": "application/vnd.github+json"}
        token = self.ctx.secrets.first_available(
            [r for r in [self.config.str_("github_token_ref", "env:GITHUB_TOKEN")] if r], label="GitHub token"
        )
        if token:
            # Only raises the rate limit; the search itself is public data.
            headers["authorization"] = f"Bearer {token.reveal()}"

        payload = self._get_json(url, headers=headers)
        if not isinstance(payload, Mapping):
            return []
        hits: list[PackageHit] = []
        for repo in payload.get("items", []):
            if not isinstance(repo, Mapping) or not repo.get("full_name"):
                continue
            hits.append(
                PackageHit(
                    name=str(repo["full_name"]),
                    source="github",
                    description=str(repo.get("description") or ""),
                    url=str(repo.get("html_url", "")),
                    score=min(1.0, float(repo.get("stargazers_count") or 0) / 1000.0),
                    raw={"stars": repo.get("stargazers_count"), "language": repo.get("language")},
                )
            )
        return hits

    def search_all(self, target: str, problems: list[str] | None = None) -> list[PackageHit]:
        """Every source, deduplicated by package name, best first.

        ``problems``, when given, receives one line per source that could not be
        asked -- a timeout, a refused connection, a 5xx. Those used to reach the
        debug log only, so "no published MCP server matched" read the same as
        "the registry never answered", and only one of them is a finding.
        """
        hits: dict[str, PackageHit] = {}
        token = _PROBLEMS.set(problems if problems is not None else [])

        def collect(finder: Any, argument: str) -> None:
            try:
                found = finder(argument)
            except Exception as exc:  # noqa: BLE001 - one dead source must not stop the stage
                log.debug("%s failed for %r: %s", finder.__name__, argument, exc)
                _note_problem(f"{finder.__name__} for {argument!r}: {exc}")
                return
            for hit in found:
                key = f"{hit.source}:{hit.name.lower()}"
                if key not in hits or hit.score > hits[key].score:
                    hits[key] = hit

        try:
            # The registry first and with its own questions (see
            # registry_queries_for), then the full-text sources.
            for query in self.registry_queries_for(target):
                collect(self.search_official, query)
            for query in self.queries_for(target):
                for finder in (self.search_npm, self.search_github):
                    collect(finder, query)
                if len(hits) >= self.limit * 3:
                    break
            # After the searches, and not subject to their early exit: PyPI is
            # asked about a short fixed list of names rather than a query, so it
            # costs the same however much the other sources turned up, and a
            # Python-published server would otherwise be invisible to a run that
            # found enough npm packages to stop looking.
            collect(self.search_pypi, target)
        finally:
            _PROBLEMS.reset(token)

        ranked = sorted(
            hits.values(),
            key=lambda h: (_source_rank(h.source), -h.score, h.name),
        )
        return ranked

    # -- conversion ------------------------------------------------------------

    def to_candidates(self, hit: PackageHit, target: str = "") -> list[Candidate]:
        """Turn a search hit into launchable candidates.

        Official-registry entries carry enough structure to build an exact
        command; hits from npm/PyPI/GitHub get a best-effort launcher and a note
        saying so, because guessing is what stage 4 exists to clean up after.

        ``target`` is the address being searched for. With it, the variables a
        registry entry declares for an address are filled in with the target's
        -- see :meth:`_fill_addresses`.
        """
        if hit.source == "mcp-registry":
            return self._from_registry_entry(hit, target)
        return self._from_plain_package(hit)

    def _from_registry_entry(self, hit: PackageHit, target: str = "") -> list[Candidate]:
        server = hit.raw
        out: list[Candidate] = []

        for remote in server.get("remotes", []) or []:
            if not isinstance(remote, Mapping) or not remote.get("url"):
                continue
            candidate = Candidate(
                kind="url",
                name=_server_slug(hit.name),
                url=str(remote["url"]),
                transport=_TRANSPORT_MAP.get(str(remote.get("type", "")).lower(), Transport.STREAMABLE_HTTP),
                origin=Stage.PACKAGE,
                source=f"MCP registry remote ({hit.name})",
                confidence=0.9,
            )
            self._absorb_headers(candidate, remote.get("headers"))
            out.append(candidate)

        launchers = self.config.dict_("registry_launchers")
        for package in server.get("packages", []) or []:
            if not isinstance(package, Mapping):
                continue
            candidate = self._from_registry_package(hit, package, launchers, target)
            if candidate:
                out.append(candidate)
        return out

    def _from_registry_package(
        self,
        hit: PackageHit,
        package: Mapping[str, Any],
        launchers: Mapping[str, Any],
        target: str = "",
    ) -> Candidate | None:
        identifier = str(package.get("identifier", "")).strip()
        if not identifier:
            return None
        registry_type = str(package.get("registryType", "")).lower()
        launcher_name = str(package.get("runtimeHint") or launchers.get(registry_type) or "")
        template = self.config.list_(f"launchers.{launcher_name}")
        if not template:
            log.debug("no launcher configured for %s/%s", registry_type, launcher_name)
            return None

        spec = identifier
        version = str(package.get("version", "")).strip()
        if version and registry_type == "npm":
            spec = f"{identifier}@{version}"
        elif version and registry_type == "oci" and ":" not in identifier:
            spec = f"{identifier}:{version}"

        command = [str(part).replace("{package}", spec) for part in template]
        command.extend(_registry_arguments(package.get("runtimeArguments")))
        command.extend(_registry_arguments(package.get("packageArguments")))

        candidate = Candidate(
            kind="stdio",
            name=_server_slug(hit.name),
            command=command,
            transport=_TRANSPORT_MAP.get(
                str((package.get("transport") or {}).get("type", "stdio")).lower(), Transport.STDIO
            ),
            origin=Stage.PACKAGE,
            source=f"MCP registry package ({registry_type}:{identifier})",
            confidence=0.85,
        )
        self._absorb_env(candidate, package.get("environmentVariables"))
        self._fill_addresses(candidate, package.get("environmentVariables"), target)
        if hit.description:
            candidate.add_note(hit.description[:200])
        return candidate

    def _from_plain_package(self, hit: PackageHit) -> list[Candidate]:
        # ``registry_launchers`` is keyed by ecosystem -- npm, pypi, oci -- which
        # is the same vocabulary a search hit's source uses, so the two need not
        # be configured twice. A source with no launcher (github) is a lead.
        launcher_name = str(self.config.dict_("registry_launchers").get(hit.source, "") or "")
        if not launcher_name:
            # A GitHub repository is a lead, not a launchable thing.
            candidate = Candidate(
                kind="stdio",
                name=_server_slug(hit.name),
                origin=Stage.PACKAGE,
                source=f"{hit.source} search hit",
                confidence=0.2,
            )
            candidate.add_note(
                f"Repository {hit.url or hit.name} looks relevant but publishes no package; "
                "clone it and follow its README, or let stage 4 propose a wrapper."
            )
            return [candidate]

        template = self.config.list_(f"launchers.{launcher_name}")
        if not template:
            return []
        return [
            Candidate(
                kind="stdio",
                name=_server_slug(hit.name),
                command=[str(p).replace("{package}", hit.name) for p in template],
                transport=Transport.STDIO,
                origin=Stage.PACKAGE,
                source=f"{hit.source} search hit ({hit.name})",
                confidence=0.4 + min(0.3, hit.score / 4),
                notes=[hit.description[:200]] if hit.description else [],
            )
        ]

    @staticmethod
    def _absorb_env(candidate: Candidate, variables: Any) -> None:
        """Record required variables; secrets become references, never values.

        ``isSecret`` is a registry's opinion about our guarantee, so it is taken
        as a hint that can only make us *more* careful: true is believed, false
        is checked against :func:`~.secret_policy.holds_a_credential`. A registry
        that forgets the flag on ``GITHUB_TOKEN`` would otherwise have it written
        as an ordinary variable -- and the identical declaration coming from an
        agent was already being caught, which is one rule with two strengths.
        """
        for variable in variables or []:
            if not isinstance(variable, Mapping):
                continue
            name = str(variable.get("name", "")).strip()
            if not name:
                continue
            declared = str(variable.get("value", "") or "")
            described = str(variable.get("description", ""))
            if variable.get("isSecret") or holds_a_credential(name, declared or name):
                # The operator supplies this out of band; we only name it.
                candidate.secret_env[name] = f"env:{name}"
                if not variable.get("isSecret"):
                    candidate.add_note(
                        f"{name} is named as an ordinary variable by the registry, and is "
                        "treated as a credential here: it will be read from the environment"
                    )
                if variable.get("isRequired"):
                    candidate.add_note(f"requires secret {name} ({described})".strip())
            elif variable.get("isRequired"):
                candidate.env[name] = ""
                candidate.add_note(f"requires {name} ({described})".strip())

    def _fill_addresses(self, candidate: Candidate, variables: Any, target: str) -> None:
        """Put the target's address in the variables an entry declares for one.

        A registry entry says *which* variable holds the address --
        ``CONFLUENCE_HOST``, ``GITLAB_URL`` -- but only this run knows *what*
        it is. Left empty, the candidate is launched pointing at nothing, and
        one that starts anyway is refused at registration for naming no
        address of the target. confluence.example.com had two such entries in the
        registry, ``@atlassian-dc-mcp/confluence`` and
        ``confluence-datacenter-mcp``, and neither could have been registered.

        Decided by configuration (``mcp.packages.address_fill``), not by a list
        of products: the variable's *name* must be address-shaped and not some
        other kind of address, and it must plausibly be about this product --
        its name contains the product's word, or it is the only address-shaped
        variable declared. Secrets are never considered: :meth:`_absorb_env`
        has already moved them to ``secret_env``, and this reads only ``env``
        candidates.
        """
        rule = self.config.section("address_fill")
        if not target or not rule.bool_("enabled", True):
            return
        address = parse_address(target)
        host, base = address.host, address.url
        if not host or not base:
            return

        def compiled(key: str) -> list[re.Pattern[str]]:
            out = []
            for raw in rule.str_list(key):
                try:
                    out.append(re.compile(str(raw), re.IGNORECASE))
                except re.error:
                    log.warning("mcp.packages.address_fill.%s: %r is not a valid pattern", key, raw)
            return out

        shaped_like, excluded, host_shaped = (
            compiled("name_patterns"),
            compiled("exclude_patterns"),
            compiled("host_patterns"),
        )
        shaped: list[str] = []
        described: dict[str, str] = {}
        for variable in variables or []:
            if not isinstance(variable, Mapping):
                continue
            name = str(variable.get("name", "")).strip()
            if not name or name in candidate.secret_env or name in shaped:
                continue
            if any(p.search(name) for p in shaped_like) and not any(p.search(name) for p in excluded):
                shaped.append(name)
                described[name] = " ".join(str(variable.get(k) or "") for k in ("description", "format", "default"))
        says_host, says_url = compiled("description_host_patterns"), compiled("description_url_patterns")

        tokens = [t.upper() for t in self.product_tokens(target) if t]
        require = rule.bool_("require_product_or_sole", True)
        sole_grounds = rule.bool_("sole_fill_grounds", False)
        for name in shaped:
            names_product = any(t in name.upper() for t in tokens)
            if require and len(shaped) > 1 and not names_product:
                candidate.add_note(
                    f"{name} looks like an address, but it names neither this product nor is it "
                    "the only address the entry declares, so it is left for the operator"
                )
                continue
            # The registry's own words first: "hostname" or "URL" in the
            # variable's description, format or default. Its name is only a
            # fallback -- it says little, and the result is proved by a call.
            words = described.get(name, "")
            if any(p.search(words) for p in says_host):
                value = host
            elif any(p.search(words) for p in says_url):
                value = base
            else:
                value = host if any(p.search(name) for p in host_shaped) else base
            candidate.env[name] = value
            if names_product or sole_grounds:
                candidate.add_note(
                    f"{name} set to {value}: the entry declares it as the address of the service, "
                    "and this run is for that address"
                )
                continue
            # The address is ours, so it cannot also be the evidence that the
            # package is for this target: a keyword hit declaring nothing but
            # a generic BASE_URL would otherwise be "tied" to any service we
            # searched for, by a value we wrote into it ourselves.
            candidate.unvouched_env.add(name)
            candidate.add_note(
                f"{name} set to {value} so the server can start: it is the only address the entry "
                "declares, but its name does not say which service it is for, so it does not by "
                "itself tie this package to the target"
            )

    @staticmethod
    def _absorb_headers(candidate: Candidate, headers: Any) -> None:
        """Copy declared headers, minus anything that carries a credential.

        This is the sharpest case of trusting somebody else's ``isSecret``. The
        value here goes verbatim into ``.mcp.json``, so a registry entry reading
        ``{"name": "Authorization", "value": "Bearer sk-live-...",
        "isSecret": false}`` would have written a live token into a file people
        commit and copy. The agentic pipeline dropped exactly that header; the
        deterministic one, which runs when there is no agent to supervise it,
        did not.
        """
        for header in headers or []:
            if not isinstance(header, Mapping):
                continue
            name = str(header.get("name", "")).strip()
            if not name:
                continue
            value = str(header.get("value", ""))
            if header.get("isSecret") or holds_a_credential(name, value):
                candidate.add_note(f"sends header {name}: supply it via mcp.discovery.auth.bearer_token_ref")
                continue
            candidate.headers[name] = value

    # -- transport -------------------------------------------------------------

    def _get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        limit = timeout or self.timeout
        with self.ctx.http(timeout=limit) as client:
            response = client.get(url, headers=dict(headers or {}), timeout=limit)
        if not response.ok:
            log.debug("%s did not answer usably: %s", url, response.problem)
            # A 404 is an answer -- PyPI saying there is no such distribution
            # is the lookup working. Nothing coming back, a server error or a
            # rate limit is a source that could not be asked, and is reported.
            if response.status == 0 or response.status >= 500 or response.status == 429:
                _note_problem(f"{urlsplit(url).netloc or url}: {response.problem}")
            return None
        return response.json()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _note_problem(line: str) -> None:
    """Record that a source could not be asked, for the current search_all."""
    problems = _PROBLEMS.get()
    if problems is not None and line not in problems:
        problems.append(line)


def _registry_arguments(arguments: Any) -> list[str]:
    """Flatten the registry's argument objects into argv fragments."""
    out: list[str] = []
    for argument in arguments or []:
        if not isinstance(argument, Mapping):
            continue
        kind = str(argument.get("type", "positional"))
        value = argument.get("value")
        if value in (None, ""):
            # A variable the operator must fill in; skipping keeps the trial run
            # honest -- it will fail loudly rather than silently misbehave.
            continue
        if kind == "named":
            name = str(argument.get("name", "")).strip()
            if name:
                out.append(name)
        out.append(str(value))
    return out


#: Labels that name no product: infrastructure prefixes and the public suffix.
#: Overridden by ``mcp.packages.generic_host_labels``; defaults only, because a
#: complete public-suffix list is not something to vendor for this.
GENERIC_HOST_LABELS = (
    "www",
    "api",
    "app",
    "mcp",
    "svc",
    "service",
    "web",
    "portal",
    "com",
    "org",
    "net",
    "io",
    "ai",
    "dev",
    "de",
    "co",
    "uk",
    "eu",
    "cn",
    "jp",
    "gov",
    "edu",
    "ac",
    "int",
    "info",
    "cloud",
    "local",
    "internal",
)


def product_tokens(target: str, generic: Sequence[str] = GENERIC_HOST_LABELS) -> list[str]:
    """The words worth searching a package registry for, most specific first.

    ``confluence.example.com`` -> ``["confluence", "example"]``. The product is the
    *leftmost* meaningful label, not the rightmost: on an internal host the
    shape is ``<product>.<organisation>.<tld>``, and taking the last label asks
    about the organisation instead of the thing.

    This used to return exactly one token, chosen as ``parts[-1]``, and the
    consequences were not subtle. ``confluence.example.com`` and
    ``jira.example.com`` were both searched for as **"example"**, so
    ``confluence-datacenter-mcp`` and ``jira-datacenter-mcp`` were never
    findable; ``smelt.example.net`` was searched for as **"de"**, which is a
    Spanish word, and returned a Colombian regulatory-compliance server that
    was then registered as SMELT's MCP endpoint.

    The organisation is kept as a second token rather than discarded: a vendor
    that publishes one server for its whole estate is a real case, and asking
    about ``example`` after ``confluence`` costs one query.
    """
    text = str(target).strip().lower()
    text = text.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].strip("[]")
    drop = {str(g).strip().lower() for g in generic if str(g).strip()}
    parts = [p for p in text.split(".") if p and p not in drop]
    return parts or [text] if parts or text else [str(target)]


def relevance_of(
    candidate: Candidate,
    tokens: Sequence[str],
    host: str = "",
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
) -> str:
    """Why this candidate has anything to do with this target, or ``""``.

    There are exactly two honest answers, and a gate that knows only the first
    is worse than no gate at all:

    1. **It names the product.** ``confluence-datacenter-mcp`` for
       ``confluence.example.com`` -- the package is *about* the thing.
    2. **It is a generic server aimed at the target.** ``mcp-graphql`` says
       nothing about SMELT anywhere in its name or description, and it is the
       correct answer for ``smelt.example.net`` because it is configured with
       ``ENDPOINT=https://smelt.example.net/graphql/``. Relevance lives in the
       configuration, not the name.

    A filter that tested only (1) would reject the working SMELT entry, which
    is why this returns the *reason* rather than a boolean: a caller can report
    what it matched on, and reviewing that is how the rule gets corrected.

    ``@kubb/mcp`` satisfies neither -- it does not mention Confluence, and it
    is pointed at nothing of yours -- which is exactly the finding that made
    this function necessary.
    """
    # Words, host labels and registry owners -- never substrings. As a
    # substring test this found "example" in "examplep_administradores" and "github"
    # in the registry namespace "io.github.mcp-dir", and registered a Brazilian
    # regulator's tool on api.mcp.ai for six Example hosts and github.com. The
    # hosting namespace is whoever published the entry; the slug stays, because
    # a server called "gitlab-mcp" is about GitLab whoever published it.
    haystack = " ".join(
        [candidate.name, candidate.source, " ".join(candidate.full_command()), " ".join(candidate.notes), candidate.url]
    )
    for token in tokens:
        token = str(token).strip().lower()
        if len(token) >= 3 and text_names(token, haystack, hosting_namespaces):
            return f"names the product ({token})"

    # Configured at the target: the URL may live in an argument or in the
    # environment, so both are searched -- values only, never secret ones. By
    # label, so `jira.example.com.evil.net` is not jira.example.com.
    if host:
        configured = " ".join([*candidate.args, *candidate.env.values(), candidate.url])
        if mentions_host(configured, host):
            return f"a generic server configured for {host}"
    return ""


def _pypi_normalize(name: str) -> str:
    """The name PyPI indexes: PEP 503 -- lowercase, runs of ``-_.`` become ``-``."""
    return re.sub(r"[-_.]+", "-", str(name).strip()).strip("-").lower()


def _server_slug(name: str) -> str:
    """'io.github.owner/mcp-filesystem' -> 'mcp-filesystem'."""
    return str(name).rsplit("/", 1)[-1].rsplit(".", 1)[-1] or str(name)


def _source_rank(source: str) -> int:
    return {"mcp-registry": 0, "npm": 1, "pypi": 2, "github": 3}.get(source, 9)


def dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Keep the highest-confidence candidate per launch identity."""
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.identity()
        if key not in best or candidate.confidence > best[key].confidence:
            best[key] = candidate
    return sorted(best.values(), key=lambda c: -c.confidence)


__all__ = ["RegistryClient", "PackageHit", "dedupe"]
