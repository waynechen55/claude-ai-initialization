"""Stage 3: build a bridge when the target has no MCP server of its own.

Three escalating answers, cheapest first:

1. **An off-the-shelf adapter.**  If stage 1 found a live endpoint that the
   client cannot reach directly -- a remote HTTP server where a stdio client is
   needed, or the reverse -- ``mcp-remote`` / ``supergateway`` already solve
   that.  Writing code would be worse than using them.
2. **A generated bridge from the target's own API description.**  If the target
   publishes OpenAPI/Swagger, its operations *are* a tool list.  We emit a
   self-contained stdio MCP server plus a JSON operation manifest.
3. **Nothing** -- and stage 4 gets to reason about it.

Design rules for the generated bridge, all of which exist to keep the security
constraints intact:

* Zero third-party dependencies -- it must run wherever Python runs.
* The base URL and every credential come from **environment variables read at
  run time**.  Nothing is baked into the file; the generated code contains no
  URL-with-token, no key, no header value.
* Only the HTTP methods in ``mcp.bridge.generated.safe_methods`` become tools
  unless the operator explicitly opts into mutating ones, so a bridge generated
  by an unattended run cannot POST/DELETE against the target.
* **What is registered is what was tested.**  The interpreter goes into
  ``.mcp.json`` as an absolute path and the trust store is named explicitly --
  see :meth:`BridgeBuilder.interpreter` and :meth:`BridgeBuilder.ca_bundle`.
  A bridge is proved here and launched later by a different process, and
  anything left to be resolved at that point can resolve differently.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from .. import __version__
from ..core.context import RunContext
from ..core.fileio import write_generated
from ..core.logging_setup import get_logger
from ..core.trust_store import TrustStore
from . import bridge_auth, login_check, trust
from .types import Candidate, Stage, Transport, slugify

log = get_logger("mcp.bridge")

#: Fallbacks for ``[mcp.bridge.generated] accept``/``accept_retry_statuses``/
#: ``accept_fallbacks``, equal to the packaged values (see 40-mcp.toml).
DEFAULT_ACCEPT = "*/*"
DEFAULT_ACCEPT_RETRY = (404, 406, 415)
DEFAULT_ACCEPT_FALLBACKS = ("*/*",)

#: Environment variables the generated bridge reads. Names only; the operator
#: supplies the values, so no credential is ever written to disk by initialize-claude-ai.
ENV_BASE_URL = "MCP_BRIDGE_BASE_URL"
ENV_TIMEOUT = "MCP_BRIDGE_TIMEOUT"
ENV_MANIFEST = "MCP_BRIDGE_MANIFEST"
ENV_CA_BUNDLE = "MCP_BRIDGE_CA_BUNDLE"


@dataclass
class BridgeArtifacts:
    script: Path
    manifest: Path
    tool_count: int


class BridgeBuilder:
    """Produces bridge candidates for a target."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.config = ctx.settings.section("mcp.bridge")
        self.generated = self.config.section("generated")

    # -- adapters --------------------------------------------------------------

    def adapter_candidates(self, target_slug: str, findings: Mapping[str, Any]) -> list[Candidate]:
        """Wrap an endpoint we already found in an existing adapter."""
        out: list[Candidate] = []
        endpoint = str(findings.get("endpoint_url") or "")
        if endpoint:
            template = self.config.list_("adapters.remote_to_stdio", ["npx", "-y", "mcp-remote", "{url}"])
            if template:
                out.append(
                    Candidate(
                        kind="stdio",
                        name=f"{target_slug}-remote",
                        command=[str(p).replace("{url}", endpoint) for p in template],
                        transport=Transport.STDIO,
                        origin=Stage.BRIDGE,
                        source=f"remote-to-stdio adapter ({template[0]}) over {endpoint}",
                        confidence=0.75,
                        notes=[
                            "Wraps the remote endpoint so a stdio-only client can use it, "
                            "and handles the OAuth flow if the endpoint needs one.",
                        ],
                    )
                )

        stdio_command = findings.get("stdio_command")
        if stdio_command:
            template = self.config.list_(
                "adapters.stdio_to_http",
                [
                    "npx",
                    "-y",
                    "supergateway",
                    "--stdio",
                    "{command}",
                ],
            )
            if template:
                joined = " ".join(str(p) for p in stdio_command)
                out.append(
                    Candidate(
                        kind="stdio",
                        name=f"{target_slug}-gateway",
                        command=[str(p).replace("{command}", joined) for p in template],
                        transport=Transport.STDIO,
                        origin=Stage.BRIDGE,
                        source="supergateway adapter",
                        confidence=0.6,
                        notes=["Exposes a stdio MCP server over HTTP/SSE for remote clients."],
                    )
                )
        return out

    # -- generation ------------------------------------------------------------

    def generate_from_openapi(
        self,
        target: str,
        spec: Mapping[str, Any],
        *,
        base_url: str,
        plan: Mapping[str, Any] | None = None,
        spec_is_evidence: bool = True,
    ) -> tuple[Candidate | None, BridgeArtifacts | None, str]:
        """Emit a stdio MCP server exposing the target's documented operations.

        Returns ``(candidate, artifacts, detail)``; ``candidate`` is None when the
        spec yielded nothing safe to expose.
        """
        slug = slugify(target)
        operations = self._operations(spec, base_url)
        if not operations:
            return None, None, "the API description contained no safely exposable operations"

        out_dir = self.config.path("output_dir") or (self.ctx.work_dir / "bridges")
        filename = self.generated.str_("filename_template", "mcp_bridge_{slug}.py").replace(
            "{slug}", slug.replace("-", "_")
        )
        script_path = out_dir / filename
        manifest_path = out_dir / f"{slug}.operations.json"

        manifest = {
            "name": f"{slug}-bridge",
            # The release of this program that generated the pair.
            "version": __version__,
            # Which script generation wrote this, so the pair can be checked
            # rather than discovered to disagree halfway through a tool schema.
            # See MANIFEST_SCHEMA in the generated source.
            "schema": MANIFEST_SCHEMA,
            "script": filename,
            "base_url": operations["base_url"],
            # Sent where an operation's own description names no single media
            # type. One value from configuration, never a weighted list: OBS
            # answered a logged-in request with a multi-value Accept with its
            # HTML 404 page, and "Accept: application/json" alone once with 406.
            "accept": self.generated.str_("accept", DEFAULT_ACCEPT),
            # A read-only call answered with one of these statuses is asked once
            # more with each fallback it has not sent (Bridge.call).
            "accept_retry": {
                "statuses": [int(s) for s in self.generated.list_("accept_retry_statuses", list(DEFAULT_ACCEPT_RETRY))],
                "values": self.generated.str_list("accept_fallbacks", list(DEFAULT_ACCEPT_FALLBACKS)),
            },
            # The protocol revisions the bridge may answer in, newest first --
            # the ones this program's own prober speaks (mcp.protocol.versions).
            # A bridge serves tools only, which every revision has, so it echoes
            # the client's version when it is one of these and offers the newest
            # otherwise, as the specification's negotiation asks.
            "protocol_versions": self.ctx.settings.str_list(
                "mcp.protocol.versions", ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
            ),
            "operations": operations["tools"],
        }

        if self.ctx.dry_run:
            return (
                None,
                None,
                f"[dry run] would write {script_path} exposing {len(operations['tools'])} tool(s)",
            )

        # How the API takes a credential, from its description or its own 401,
        # so the entry names parts a person can fill -- not "a header" and "a
        # value". See bridge_auth.
        method = self.auth_method(spec, operations, plan=plan, spec_is_evidence=spec_is_evidence)
        parts = bridge_auth.parts_of(method, self.ctx.settings) if method else []
        if parts:
            manifest["auth"] = {
                **{k: v for k, v in method.items() if k in ("scheme", "in", "name", "prefix")},
                # The bridge reads each part under the very name the operator
                # exports -- BUILD_EXAMPLE_NET_USERNAME -- so the entry, the report
                # and the shell all say the same word.
                "variables": {part.lower(): bridge_auth.exported_name(slug, part, self.ctx.settings) for part in parts},
            }

        self.ctx.ensure_dir(out_dir)
        try:
            write_generated(manifest_path, json.dumps(manifest, indent=2) + "\n")
            write_generated(script_path, _BRIDGE_SOURCE, executable=True)
        except OSError as exc:
            return None, None, f"cannot write the bridge: {exc}"

        python = self.interpreter()
        # The manifest and the CA bundle are named here, explicitly, even though
        # the script can find them beside itself. That fallback is what broke
        # openqa.example.net: copied to another machine, the directory also held an
        # older manifest, the script opened that one and exited -- and nothing
        # in the entry said a manifest existed, where it was, or which variable
        # chose it. A setting the entry does not name is a setting nobody can
        # find.
        #
        # The old objection -- four absolute paths into this program's state
        # directory -- no longer holds: the writer rewrites each through a
        # configured root (`${ICA_MCP_BRIDGES:-...}/...`), so the directory still
        # moves by exporting one variable. The variables the script reads and
        # this does not set (auth, timeout) are declared by the writer from the
        # script itself; see :mod:`.env_contract`.
        env = {ENV_BASE_URL: operations["base_url"], ENV_MANIFEST: str(manifest_path)}
        notes = [
            f"Exposes {len(operations['tools'])} operation(s) from the target's API description.",
            f"This bridge is the directory {out_dir.name}/ -- the script, its manifest and any "
            f"PEM beside it. The entry names the manifest in {ENV_MANIFEST} and the bundle in "
            f"{ENV_CA_BUNDLE}; copy the directory and export the root variable to move it.",
        ]
        bundle = self.ca_bundle(target, base_url, operations["base_url"])
        if bundle:
            env[ENV_CA_BUNDLE] = bundle
            notes.append(f"TLS is verified against {bundle} ({ENV_CA_BUNDLE}).")

        candidate = Candidate(
            kind="stdio",
            name=f"{slug}-bridge",
            command=[python, str(script_path)],
            env=env,
            transport=Transport.STDIO,
            origin=Stage.BRIDGE,
            source=f"generated from {operations['spec_source']}",
            confidence=0.7,
            notes=notes,
        )
        if parts:
            candidate.login_scheme = str(method.get("scheme") or method.get("method") or "").lower()
            self._declare_credential(candidate, target, slug, method, parts, operations)
        else:
            candidate.add_note(
                "Neither the API description nor the server named an authentication method, so "
                "the bridge sends no credential; regenerate it if the API turns out to need one."
            )
        artifacts = BridgeArtifacts(script=script_path, manifest=manifest_path, tool_count=len(operations["tools"]))
        return candidate, artifacts, f"generated {script_path.name} with {artifacts.tool_count} tool(s)"

    # -- how the bridge authenticates ------------------------------------------

    def auth_method(
        self,
        spec: Mapping[str, Any],
        operations: Mapping[str, Any],
        *,
        plan: Mapping[str, Any] | None = None,
        spec_is_evidence: bool = True,
    ) -> dict[str, str]:
        """The method from the API description, the address's login plan, or the server's own 401; {} if none says.

        A description the *server* published is evidence and comes first. One
        this program assembled for an API kind (``spec_is_evidence`` false)
        carries only the kind's configured default, so the login plan -- what
        the address takes and what the operator holds -- comes before it.
        """
        auth = self.generated.section("auth")
        if auth.bool_("from_spec", True) and spec_is_evidence:
            method = bridge_auth.method_from_spec(spec)
            if method:
                return method
        if auth.bool_("from_plan", True):
            method = bridge_auth.method_from_plan(plan)
            if method:
                return method
        if auth.bool_("from_spec", True) and not spec_is_evidence:
            method = bridge_auth.method_from_spec(spec)
            if method:
                return method
        if not auth.bool_("probe_challenge", True):
            return {}
        url = self._probe_url(operations)
        if not url:
            return {}
        try:
            with self.ctx.http(timeout=auth.float_("timeout_seconds", 10)) as client:
                response = client.get(url)
        except Exception as exc:  # noqa: BLE001 - asking is optional; failing to ask is not an error
            log.debug("asking %s how it authenticates failed: %s", url, exc)
            return {}
        if response.status != 401:
            return {}
        return bridge_auth.method_from_challenge(response.headers.get("www-authenticate", ""), self.ctx.settings)

    @staticmethod
    def _probe_url(operations: Mapping[str, Any]) -> str:
        """A documented GET that needs no path parameter, to ask unauthenticated."""
        base = str(operations.get("base_url", "")).rstrip("/")
        for tool in operations.get("tools", []):
            path = str(tool.get("path", ""))
            if tool.get("method") == "GET" and "{" not in path:
                return base + path
        return base

    def _declare_credential(
        self,
        candidate: Candidate,
        target: str,
        slug: str,
        method: Mapping[str, str],
        parts: list[str],
        operations: Mapping[str, Any],
    ) -> None:
        """Name each part after the target, describe it, and check the login if it is set."""
        settings = self.ctx.settings
        host = urlsplit(target if "://" in target else f"https://{target}").hostname or target
        # How it sends the credential comes from evidence, so a refusal from it
        # is a verdict on the credential (types.unconfirmed_refusal).
        candidate.auth_method_evidence = str(method.get("evidence") or "its API description")
        exported: dict[str, str] = {}
        for part in parts:
            name = bridge_auth.exported_name(slug, part, settings)
            exported[part] = name
            candidate.secret_env[name] = f"env:{name}"
            candidate.secret_formats[name] = bridge_auth.describe_part(part, host, settings)
            candidate.secret_sources[name] = host
        wanted = ", ".join(f"{exported[p]} ({bridge_auth.describe_part(p, host, settings)})" for p in parts)
        candidate.add_note(
            f"{host} takes {bridge_auth.method_in_words(method)}, according to "
            f"{method.get('evidence', 'its API description')}: export {wanted}. The bridge builds "
            "the request header itself; nothing is stored in the file."
        )
        if self.generated.section("auth").bool_("check_login", True):
            verdict = self._check_login(method, parts, exported, operations)
            if verdict:
                candidate.add_note(verdict)

    def _check_login(
        self,
        method: Mapping[str, str],
        parts: list[str],
        exported: Mapping[str, str],
        operations: Mapping[str, Any],
    ) -> str:
        """One authenticated GET when every part is exported; what it showed, or "" if not asked.

        A login in the wrong form is otherwise found at first use. Read-only, a
        documented GET, and the values never leave this request.
        """
        values = {}
        for part in parts:
            secret = self.ctx.secrets.resolve(f"env:{exported[part]}")
            if secret is None:
                return ""
            values[part] = secret.reveal() if hasattr(secret, "reveal") else str(secret)
        headers, query = bridge_auth.request_credentials(method, values)
        url = self._probe_url(operations)
        if not url:
            return ""
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query)
        try:
            with self.ctx.http(timeout=self.generated.section("auth").float_("timeout_seconds", 10)) as client:
                response = client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return f"the login could not be checked ({type(exc).__name__})"
        names = ", ".join(exported[p] for p in parts)
        # The same verdicts, words and remedies as the login check every other
        # entry gets (login_check), so a bridge and a package whose credential
        # fails are reported alike.
        body = ""
        try:
            body = str(getattr(response, "text", "") or "")[:2000]
        except Exception:  # noqa: BLE001 - the status is the verdict; the body only refines it
            body = ""
        verdict, detail = login_check.judge_http(response.status, body, self.ctx.settings)
        verdict, detail = self._compared_with_anonymous(url, verdict, detail, response.status, body)
        line = f"{login_check.in_words(verdict)} ({detail}) with {names} as exported"
        if verdict in login_check.REFUSED:
            line += " -- " + login_check.remedy(
                verdict,
                host=urlsplit(url).hostname or "",
                variables=[exported[p] for p in parts],
                detail=detail,
                settings=self.ctx.settings,
            )
        return line

    def _compared_with_anonymous(self, url: str, verdict: str, detail: str, status: int, body: str) -> tuple[str, str]:
        """The same GET with no credential: an answer anybody gets proves nothing about the login.

        See :meth:`.CandidateVerifier._anonymous_check` -- the same rule for a
        server's tools; here for the request the generated bridge makes itself.
        """
        if not self.ctx.settings.bool_("mcp.verification.login_check.compare_with_anonymous", True):
            return verdict, detail
        if verdict not in (login_check.ACCEPTED, login_check.REJECTED):
            return verdict, detail
        try:
            with self.ctx.http(timeout=self.generated.section("auth").float_("timeout_seconds", 10)) as client:
                anonymous = client.get(url)
        except Exception:  # noqa: BLE001 - no comparison is no change
            return verdict, detail
        text = str(getattr(anonymous, "text", "") or "")[:2000]
        if verdict == login_check.ACCEPTED and 200 <= anonymous.status < 300:
            return (
                login_check.UNCHECKED,
                f"{detail}, and the same with no credential at all -- it proves nothing about the login",
            )
        if verdict == login_check.REJECTED and anonymous.status == status and login_check.same_answer(text, body):
            return (
                verdict,
                f"{detail}, exactly as with no credential -- it may never have been sent, or it may not be valid",
            )
        return verdict, detail

    # -- how the bridge will be launched ---------------------------------------

    def interpreter(self) -> str:
        """The absolute path of the interpreter the bridge is registered with.

        Not the word ``python3``. A bridge is proved here and launched later by
        Claude Code, in a different process with a different ``PATH``, and a
        bare name is resolved again at that moment: on a host carrying a
        Homebrew, pyenv or conda Python ahead of the system one, the bridge that
        handshaked during discovery starts under an interpreter nobody tested.
        The usual symptom is every call failing TLS verification, because that
        interpreter trusts a different certificate bundle -- a failure that
        looks like the target being unreachable and is not.

        So the path is resolved once, here, and written out in full. The bridge
        is standard-library only, so any CPython runs it; what has to be true is
        that the one recorded is the one that will actually start.
        ``mcp.bridge.generated.interpreter`` pins a specific one; otherwise this
        program's own interpreter is used, since it is absolute by construction
        and known to exist, and ``interpreter_names`` is the last resort for the
        case where that is unavailable.
        """
        configured = self.generated.str_("interpreter").strip()
        if configured:
            found = self.ctx.runner.which(configured)
            if found:
                return os.path.realpath(found)
            log.warning(
                "mcp.bridge.generated.interpreter names %r, which was not found; "
                "falling back to this program's own interpreter",
                configured,
            )
        if sys.executable:
            return os.path.realpath(sys.executable)
        for name in self.generated.str_list("interpreter_names", ["python3", "python"]):
            found = self.ctx.runner.which(name)
            if found:
                return os.path.realpath(found)
        # Nothing resolved: return the first configured name so the candidate
        # still describes what it wanted, and let verification fail honestly
        # rather than registering a command that was never going to start.
        return next(iter(self.generated.str_list("interpreter_names", ["python3", "python"])), "")

    def ca_bundle(self, *reaches: str) -> str:
        """The PEM bundle the bridge should verify TLS against, or ``""``.

        Empty means "whatever this interpreter already trusts", which is the
        right answer on a host whose Python was built by the distribution. It
        is the wrong answer wherever the target's certificate is signed by an
        internal authority the distribution does not ship -- the ordinary case
        for an internal GitLab or openQA -- and wherever the interpreter brings
        its own narrower store. Naming the system bundle covers both, and it
        never weakens verification: it is the set of authorities this host has
        been told to trust, which is a superset of a vendored one.

        Detection itself lives in :mod:`.trust`, shared with the writer, which
        needs the same answer for every *other* server it registers. It was
        here alone for a long time, and the consequence was that the one kind
        of entry this program writes itself could reach a host behind an
        internal CA while every published package it registered could not.
        ``mcp.bridge.generated.ca_bundle`` still pins a file for bridges only.

        ``reaches`` are the addresses the bridge will call. When one is a host
        whose certificate the host bundle could not verify, the run's own
        bundle -- the host's plus what that target needed -- is named instead
        (see :class:`~..core.trust_store.TrustStore`).
        """
        configured = self.generated.str_("ca_bundle").strip()
        if configured:
            if Path(os.path.expanduser(configured)).is_file():
                return os.path.expanduser(configured)
            log.warning("mcp.bridge.generated.ca_bundle names %r, which does not exist", configured)
            return ""
        run_bundle = TrustStore.for_run(self.ctx).bundle_for(reaches) if reaches else ""
        return run_bundle or trust.system_bundle(self.ctx.settings)

    # -- spec parsing ----------------------------------------------------------

    def _client_safe(self, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give a parameter the client cannot accept by name an ``arg`` it can.

        The tool schema must pass ``mcp.verification.client_name_pattern`` or
        the client refuses every request once the tool is loaded -- what
        osc-mcp's ``path_repository jsonschema:`` did. An API parameter named
        ``package[]`` or ``X-Foo Bar`` is exposed as ``package_`` /
        ``X-Foo_Bar`` and still sent to the API under its own name.
        """
        rule = self.ctx.settings.str_("mcp.verification.client_name_pattern", "^[a-zA-Z0-9_.-]{1,64}$")
        if not rule:
            return parameters
        taken = {p["name"] for p in parameters if re.fullmatch(rule, p["name"])}
        for parameter in parameters:
            if re.fullmatch(rule, parameter["name"]):
                continue
            base = (re.sub(r"[^a-zA-Z0-9_.-]", "_", parameter["name"]).strip("_") or "arg")[:60]
            arg, n = base, 2
            while arg in taken or not re.fullmatch(rule, arg):
                arg = f"{base[:57]}_{n}"
                n += 1
            taken.add(arg)
            parameter["arg"] = arg
        return parameters

    def _operations(self, spec: Mapping[str, Any], base_url: str) -> dict[str, Any] | None:
        """Turn an OpenAPI document into a tool manifest."""
        paths = spec.get("paths")
        if not isinstance(paths, Mapping):
            return None

        safe = {m.upper() for m in self.generated.str_list("safe_methods", ["GET", "HEAD", "OPTIONS"])}
        # Which of them change nothing -- announced to the client as readOnlyHint,
        # which is also what lets the login check call one (login_check.pick).
        read_only = set(safe)
        if self.generated.bool_("include_mutating_methods", False):
            safe |= {"POST", "PUT", "PATCH", "DELETE"}
        max_tools = self.generated.int_("max_tools", 40)

        resolved_base = _server_url(spec, base_url)
        tools: list[dict[str, Any]] = []

        for path, item in paths.items():
            if not isinstance(item, Mapping) or len(tools) >= max_tools:
                continue
            shared = _parameters(item.get("parameters"))
            for method, operation in item.items():
                if not isinstance(operation, Mapping):
                    continue
                # A POST its description marks read-only (x-read-only: a GraphQL
                # query) is a read like any GET.
                declared_read = bool(operation.get("x-read-only"))
                if method.upper() not in safe and not declared_read:
                    continue
                if len(tools) >= max_tools:
                    break
                name = _tool_name(operation, method, str(path))
                params = self._client_safe(shared + _parameters(operation.get("parameters")))
                # One documented type is asked for exactly; several (or none)
                # leave the manifest's single value -- joining them made the
                # weighted list a gateway rejected.
                accept = _media_types(operation, spec)
                tools.append(
                    {
                        **({"accept": accept[0]} if len(accept) == 1 else {}),
                        "name": name,
                        "description": str(
                            operation.get("summary") or operation.get("description") or f"{method.upper()} {path}"
                        )[:400],
                        "method": method.upper(),
                        "read_only": method.upper() in read_only or declared_read,
                        "path": str(path).split("#", 1)[0],
                        "parameters": params,
                        "inputSchema": _input_schema(params),
                        **(
                            {"body_fixed": dict(operation["x-body-fixed"])}
                            if isinstance(operation.get("x-body-fixed"), Mapping)
                            else {}
                        ),
                        **(
                            {"refuse": {str(k): str(v) for k, v in operation["x-refuse"].items()}}
                            if isinstance(operation.get("x-refuse"), Mapping)
                            else {}
                        ),
                        **(
                            {"json_params": [str(x) for x in operation["x-json-params"]]}
                            if isinstance(operation.get("x-json-params"), list)
                            else {}
                        ),
                    }
                )

        if not tools:
            return None
        return {
            "base_url": resolved_base,
            "tools": tools,
            "spec_source": str(spec.get("info", {}).get("title") or "the target's OpenAPI description"),
        }


# ----------------------------------------------------------------------------
# OpenAPI helpers
# ----------------------------------------------------------------------------


def _server_url(spec: Mapping[str, Any], fallback: str) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if isinstance(server, Mapping):
                url = str(server.get("url", "")).strip()
                if url.startswith(("http://", "https://")):
                    return url.rstrip("/")
                if url.startswith("/"):
                    return fallback.rstrip("/") + url.rstrip("/")
    # Swagger 2.0
    host = str(spec.get("host", "")).strip()
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{str(spec.get('basePath', '')).rstrip('/')}"
    return fallback.rstrip("/")


def _parameters(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for parameter in raw or []:
        if not isinstance(parameter, Mapping) or not parameter.get("name"):
            continue
        location = str(parameter.get("in", "query"))
        if location not in ("query", "path", "header", "body"):
            continue
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), Mapping) else {}
        out.append(
            {
                "name": str(parameter["name"]),
                "in": location,
                "required": bool(parameter.get("required")),
                "type": str(schema.get("type") or parameter.get("type") or "string"),
                "description": str(parameter.get("description", ""))[:200],
            }
        )
    return out


def _input_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    properties = {
        p.get("arg") or p["name"]: {
            "type": p["type"] if p["type"] in ("string", "integer", "number", "boolean") else "string",
            "description": p["description"],
        }
        for p in parameters
    }
    required = [p.get("arg") or p["name"] for p in parameters if p["required"]]
    return {"type": "object", "properties": properties, "required": required}


def _media_types(operation: Mapping[str, Any], spec: Mapping[str, Any]) -> list[str]:
    """What an operation says it returns: OpenAPI 3 ``responses.*.content``, else Swagger ``produces``.

    Only successful responses count. The bridge asks for exactly these, so a
    server is never refused a format it documented -- OBS documents XML.
    """
    found: list[str] = []
    responses = operation.get("responses")
    if isinstance(responses, Mapping):
        for code, response in responses.items():
            if not str(code).startswith("2") or not isinstance(response, Mapping):
                continue
            content = response.get("content")
            if isinstance(content, Mapping):
                found.extend(str(media) for media in content if str(media) not in found)
    if not found:
        produces = operation.get("produces") or spec.get("produces") or []
        if isinstance(produces, (list, tuple)):
            found.extend(str(media) for media in produces if str(media) not in found)
    return [media for media in found if media]


def _tool_name(operation: Mapping[str, Any], method: str, path: str) -> str:
    raw = str(operation.get("operationId") or f"{method}_{path}")
    cleaned = "".join(c if c.isalnum() else "_" for c in raw).strip("_").lower()
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned[:64] or "operation"


# ----------------------------------------------------------------------------
# The generated bridge
# ----------------------------------------------------------------------------
#
# Written verbatim to disk. It is deliberately generic: the manifest supplies the
# tools, so this source is identical for every target and can be reviewed once.

_BRIDGE_SOURCE = '''#!/usr/bin/env python3
"""MCP stdio bridge, generated by initialize-claude-ai.

Exposes the operations listed in a JSON manifest as MCP tools, forwarding each
one to the target's HTTP API. Standard library only.

Configuration comes entirely from the environment -- nothing is baked in:

  MCP_BRIDGE_MANIFEST   path to the operations manifest (required)
  MCP_BRIDGE_BASE_URL   override the base URL recorded in the manifest
  the credential        read under the names the manifest's "auth" gives, for
                        the method the API uses: a user name and a password
                        (HTTP Basic -- the header is built here), a token, or an
                        API key. Nothing about it is in this file.
  MCP_BRIDGE_TIMEOUT    per-request timeout in seconds (default 30)
  MCP_BRIDGE_ACCEPT     the Accept header, for an operation whose description
                        names no media type (default: the manifest's, else
                        anything -- a server is never refused a format it serves)
  MCP_BRIDGE_CA_BUNDLE  PEM bundle used to verify TLS, for a target whose
                        certificate is signed by an internal authority. Unset
                        means this interpreter's own default trust store.

Verification is never disabled: an unverifiable peer is a different thing from
a verified one, and the way to accept an internal CA is to name its bundle.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# Answered only when the manifest names no revisions or version of its own --
# one written before it carried them. The generator writes both: the revisions
# its prober speaks (mcp.protocol.versions) and its own release.
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "initialize-claude-ai-bridge"
SERVER_VERSION = "0.1.0"

# The manifest generation this script reads. Written into every manifest and
# checked on load: the two files are produced together and a mismatched pair
# fails deep inside tool construction, where the error is a traceback and the
# operator sees only "Connection closed". Bump when the manifest shape changes.
MANIFEST_SCHEMA = 2

# Operation fields that must be an object when present, and must not be empty.
# Named here so a mismatch is reported against the field rather than raised
# from whichever line happened to touch it first.
MANIFEST_MAPPING_FIELDS = ("path_params", "query", "headers", "inputSchema")
MANIFEST_REQUIRED_FIELDS = ("name", "method", "path")


def beside_me(name: str) -> str:
    """A companion file's path, resolved against this script's own directory.

    The manifest and the CA bundle are part of the bridge, not configuration
    the operator chose: the three files are written together and are useless
    apart. Naming their absolute paths in .mcp.json made the entry describe
    this machine's directory layout, so moving it meant editing three strings
    and knowing which three -- and the entry gave no hint that the files had
    to travel at all.

    Asking the script where it is removes all three. Copy the directory, and
    the bridge finds its own parts. The environment variables still win when
    set, so nothing that worked before stops working.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def manifest_path() -> str:
    """This bridge's manifest: named, or matching this script, or unambiguous.

    The middle rule matters and was missing. Finding "the first
    *.operations.json in the directory" is right for a bridge that owns its
    directory and wrong the moment two of them share one -- and they do, when
    an operator collects several bridges in a working directory. There, the
    opensuse bridge picked up `openqa_example_net.operations.json`: a real
    manifest, for a different service, loaded in silence. Guessing wrong is
    worse than not starting, because a bridge answering about the wrong host
    looks like it works.

    So: the manifest whose name matches this script's is preferred, and a bare
    glob is only trusted when it finds exactly one candidate.
    """
    explicit = os.environ.get("MCP_BRIDGE_MANIFEST", "").strip()
    if explicit:
        return explicit

    here = os.path.dirname(os.path.abspath(__file__)) or "."
    stem = os.path.basename(os.path.abspath(__file__))
    for prefix in ("mcp_bridge_", ""):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    # Separators differ by convention on the two sides -- the script is
    # mcp_bridge_target_example_test.py and its manifest is
    # target-example-test.operations.json -- so compare with them flattened.
    stem = os.path.splitext(stem)[0].replace("-", "_")

    found = sorted(e for e in os.listdir(here) if e.endswith(".operations.json"))
    mine = [e for e in found if e.split(".")[0].replace("-", "_") == stem]
    if len(mine) == 1:
        return beside_me(mine[0])
    if len(found) == 1:
        return beside_me(found[0])
    if found:
        raise SystemExit(
            "ambiguous operations manifest: " + ", ".join(found) + " all sit next to "
            + os.path.abspath(__file__) + " and none is named for it. Set MCP_BRIDGE_MANIFEST "
            "to the right one -- loading another service's manifest would answer confidently "
            "about the wrong host."
        )
    raise SystemExit(
        "no operations manifest: MCP_BRIDGE_MANIFEST is not set and no *.operations.json "
        "file sits next to " + os.path.abspath(__file__) + ". The bridge directory has to "
        "be copied whole."
    )


def load_manifest() -> dict:
    """Read the manifest, and refuse one this script was not written for.

    A bridge is two files that have to agree, and nothing used to check that
    they did. An openQA bridge was found beside a manifest from an earlier
    generation whose ``path_params`` was a list where the script expected a
    mapping; it crashed on the first line of ``tools/list`` with

        AttributeError: 'list' object has no attribute 'items'

    which the operator saw as ``failed - Connection closed``. The script had
    been regenerated, the manifest had not, and the pair had drifted.

    So the manifest carries a stamp saying which script generation wrote it,
    and a mismatch is refused **by name** rather than discovered halfway
    through building a tool schema. A manifest with no stamp at all predates
    this check and is accepted -- refusing it would break every bridge already
    on disk -- but it is validated for shape before anything relies on it.
    """
    path = manifest_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise SystemExit(f"cannot read the operations manifest {path}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"the operations manifest {path} is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise SystemExit(f"the operations manifest {path} is not a JSON object")

    stamped = manifest.get("schema")
    if stamped is not None and stamped != MANIFEST_SCHEMA:
        raise SystemExit(
            f"the operations manifest {path} was written for schema {stamped} and this "
            f"bridge reads schema {MANIFEST_SCHEMA}. They are generated as a pair; "
            "regenerate the bridge rather than mixing the two."
        )

    problems = manifest_problems(manifest)
    if problems:
        raise SystemExit(
            f"the operations manifest {path} does not match what this bridge reads: "
            + "; ".join(problems[:3])
            + ". They are generated as a pair; regenerate the bridge."
        )
    return manifest


def manifest_problems(manifest: dict) -> list:
    """Everything about this manifest that would fail later, found up front.

    Checked here rather than where each field is used, because a failure in the
    middle of building a tool schema arrives as a Python traceback -- and a
    traceback on stderr reaches the operator as ``Connection closed``, which
    names neither the file nor the field.
    """
    problems = []
    operations = manifest.get("operations")
    if not isinstance(operations, list):
        return [f"'operations' should be a list, not {type(operations).__name__}"]
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            problems.append(f"operation {index} is a {type(op).__name__}, not an object")
            continue
        where = op.get("name") or f"operation {index}"
        for field in MANIFEST_MAPPING_FIELDS:
            value = op.get(field)
            if value is not None and not isinstance(value, dict):
                problems.append(
                    f"{where}: '{field}' is a {type(value).__name__}, expected an object"
                )
        for field in MANIFEST_REQUIRED_FIELDS:
            if not op.get(field):
                problems.append(f"{where}: '{field}' is missing")
    return problems


class Bridge:
    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        self.base_url = (os.environ.get("MCP_BRIDGE_BASE_URL") or manifest.get("base_url", "")).rstrip("/")
        self.timeout = float(os.environ.get("MCP_BRIDGE_TIMEOUT", "30") or 30)
        self.tools = {t["name"]: t for t in manifest.get("operations", [])}
        bundle = os.environ.get("MCP_BRIDGE_CA_BUNDLE", "").strip()
        if not bundle:
            # Same reasoning as the manifest: a PEM written beside the script is
            # part of the bridge. Falling back to it means the entry does not
            # have to name it, and the system trust store still answers when
            # there is no bundle -- an internal CA is the exception, not a
            # requirement of every bridge.
            for entry in sorted(os.listdir(os.path.dirname(os.path.abspath(__file__)) or ".")):
                if entry.endswith(".pem"):
                    bundle = beside_me(entry)
                    break
        self.ssl_context = ssl.create_default_context(cafile=bundle) if bundle else None
        if self.ssl_context is not None:
            # A bundle may end a chain at a CA that is not a self-signed root --
            # the chain a server sends, kept by the run that wrote this. Python
            # 3.13 allows that by default; older interpreters are told to.
            self.ssl_context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)

    def headers(self, tool: dict | None = None) -> dict:
        # A value this session proved, then what the operation documented,
        # then the operator's choice, then the manifest's, then anything.
        # Asking for JSON alone got 406 from an API that serves only XML.
        accept = (
            getattr(self, "learned_accept", "")
            or (tool or {}).get("accept")
            or os.environ.get("MCP_BRIDGE_ACCEPT", "").strip()
            or self.manifest.get("accept")
            or "*/*"
        )
        out = {"accept": accept, "user-agent": SERVER_NAME}
        out.update(self.auth_headers())
        return out

    def credential(self, part: str) -> str:
        # The variable for one part is named by the manifest, so this source
        # names none -- and an entry carries only the parts its method needs.
        name = (self.manifest.get("auth") or {}).get("variables", {}).get(part, "")
        return os.environ.get(name, "") if name else ""

    def auth_headers(self) -> dict:
        auth = self.manifest.get("auth") or {}
        scheme = str(auth.get("scheme", "")).lower()
        if scheme == "basic":
            user, password = self.credential("username"), self.credential("password")
            if user and password:
                pair = base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")
                return {"authorization": "Basic " + pair}
        elif scheme in ("bearer", "token"):
            token = self.credential("token")
            if token:
                return {"authorization": (auth.get("prefix") or scheme.capitalize()) + " " + token}
        elif scheme == "apikey" and auth.get("in", "header") == "header" and auth.get("name"):
            key = self.credential("api_key")
            if key:
                return {auth["name"]: key}
        return {}

    def unset_hint(self, status: int) -> str:
        """For a refusal: which credential variable reached the bridge empty, or not at all, and what to do.

        A variable the entry does not pass never arrives, whatever is exported:
        the client hands a server only its entry's env.
        """
        if status not in (401, 403):
            return ""
        names = [n for n in ((self.manifest.get("auth") or {}).get("variables") or {}).values() if n]
        absent = [n for n in names if n not in os.environ]
        empty = [n for n in names if n in os.environ and not os.environ.get(n)]
        if absent:
            return (" -- the entry does not pass " + ", ".join(absent) + "; add "
                    + ", ".join('"%s": "${%s:-}"' % (n, n) for n in absent)
                    + " to its env, then export it in the shell you start Claude Code from")
        if empty:
            return " -- " + ", ".join(empty) + " not set: export it in the shell you start Claude Code from"
        return ""

    def auth_query(self) -> dict:
        auth = self.manifest.get("auth") or {}
        if str(auth.get("scheme", "")).lower() == "apikey" and auth.get("in") == "query" and auth.get("name"):
            key = self.credential("api_key")
            if key:
                return {auth["name"]: key}
        return {}

    def tool_list(self) -> list:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                # Said by the generator, which knows which methods change nothing
                # (mcp.bridge.generated.safe_methods); a client -- and the login
                # check -- may then call it without fear.
                **({"annotations": {"readOnlyHint": bool(tool["read_only"])}} if "read_only" in tool else {}),
            }
            for tool in self.tools.values()
        ]

    def call(self, name: str, arguments: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"isError": True, "content": [{"type": "text", "text": "unknown tool: " + name}]}

        path = tool["path"]
        query = {}
        body = dict(tool.get("body_fixed") or {})
        headers = self.headers(tool)
        for parameter in tool.get("parameters", []):
            # "arg" is the name the tool schema exposes, when the API's own name
            # is one the client cannot accept; the request still uses the API's.
            key = parameter["name"]
            arg = parameter.get("arg") or key
            if arg not in arguments:
                if parameter.get("required"):
                    return _error("missing required parameter: " + arg)
                continue
            value = arguments[arg]
            if parameter["in"] == "path":
                path = path.replace("{" + key + "}", urllib.parse.quote(str(value), safe=""))
            elif parameter["in"] == "query":
                query[key] = str(value)
            elif parameter["in"] == "header":
                headers[key] = str(value)
            elif parameter["in"] == "body":
                if key in tool.get("json_params", []) and isinstance(value, str):
                    if not value.strip():
                        continue
                    try:
                        value = json.loads(value)
                    except ValueError:
                        return _error("parameter " + arg + " is not JSON")
                body[key] = value
        # What the operation must never send -- a GraphQL mutation through a
        # read-only bridge.
        for key, pattern in (tool.get("refuse") or {}).items():
            if re.search(pattern, str(body.get(key, "")), re.IGNORECASE):
                return _error("refused: this bridge only reads (" + key + " matches " + pattern + ")")

        query.update(self.auth_query())
        url = self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

        data = json.dumps(body).encode("utf-8") if body else None
        # The request's form is retried, never its effect: only a read-only
        # call is asked again, with each fallback Accept not sent yet, after a
        # status saying the request was rejected (manifest "accept_retry").
        # OBS answered a logged-in request with a multi-value Accept with its
        # HTML 404 page and the same request with "Accept: */*" with the data.
        retry = self.manifest.get("accept_retry") or {}
        statuses = {int(s) for s in retry.get("statuses", [404, 406, 415])}
        accepts = [headers.get("accept", "*/*")]
        method = tool.get("method", "GET")
        if tool.get("read_only", method in ("GET", "HEAD", "OPTIONS")):
            accepts += [v for v in retry.get("values", ["*/*"]) if v and v not in accepts]
        first = None
        for index, accept in enumerate(accepts):
            request = urllib.request.Request(url, data=data, method=method)
            for key, value in headers.items():
                request.add_header(key, value)
            request.add_header("accept", accept)
            if data is not None:
                request.add_header("content-type", "application/json")
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self.ssl_context
                ) as response:
                    body = response.read(1_000_000).decode("utf-8", "replace")
                    if index:
                        # Kept for the session; said where an operator looks.
                        self.learned_accept = accept
                        sys.stderr.write(
                            "%s: HTTP %s with Accept %r; %r works -- set MCP_BRIDGE_ACCEPT to keep it%s"
                            % (SERVER_NAME, first[0], accepts[0], accept, chr(10))
                        )
                    return {"content": [{"type": "text", "text": body}]}
            except urllib.error.HTTPError as exc:
                detail = exc.read(4096).decode("utf-8", "replace")
                if first is None:
                    first = (exc.code, "HTTP " + str(exc.code) + ": " + detail + self.unset_hint(exc.code))
                if exc.code in statuses and index + 1 < len(accepts):
                    continue
                tried = "" if not index else " (also asked with Accept %r: HTTP %s)" % (accept, exc.code)
                return _error(first[1] + tried)
            except Exception as exc:  # noqa: BLE001
                return _error(str(exc))
        return _error(first[1] if first else "no request was sent")


def _error(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def negotiated_version(requested, supported):
    """The revision to answer ``initialize`` in: the client's if listed, else the newest listed."""
    listed = [v for v in (supported or []) if isinstance(v, str) and v] or [PROTOCOL_VERSION]
    return requested if requested in listed else listed[0]


def main() -> int:
    bridge = Bridge(load_manifest())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue

        method = message.get("method", "")
        request_id = message.get("id")

        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion", "")
            result = {
                "protocolVersion": negotiated_version(requested, bridge.manifest.get("protocol_versions")),
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": bridge.manifest.get("name", SERVER_NAME),
                    "version": str(bridge.manifest.get("version") or SERVER_VERSION),
                },
            }
        elif method == "tools/list":
            result = {"tools": bridge.tool_list()}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = bridge.call(params.get("name", ""), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        elif request_id is None:
            continue  # a notification we do not need to act on
        else:
            _write({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "method not found: " + method}})
            continue

        if request_id is not None:
            _write({"jsonrpc": "2.0", "id": request_id, "result": result})
    return 0


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


if __name__ == "__main__":
    # Any unhandled failure here is one the operator will be shown as
    # "Connection closed" -- the client sees a process that exited and nothing
    # else. A traceback on stderr names the line but not the cause, so anything
    # that escapes is reduced to one sentence that names both.
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the last word before the client sees nothing
        sys.stderr.write(
            "%s: %s: %s\\n" % (os.path.basename(__file__), type(exc).__name__, exc)
        )
        sys.stderr.write(
            "This bridge and its .operations.json manifest are generated as a pair; "
            "if one has been replaced without the other, regenerate the bridge.\\n"
        )
        raise SystemExit(1)
'''

#: The manifest generation the script above reads, taken from the script itself:
#: the stamp written into a manifest and the check that reads it back must be
#: one number, and a setting could only ever disagree with the script.
MANIFEST_SCHEMA = int(re.search(r"^MANIFEST_SCHEMA = (\d+)$", _BRIDGE_SOURCE, re.M).group(1))


__all__ = [
    "BridgeBuilder",
    "BridgeArtifacts",
    "ENV_BASE_URL",
    "ENV_TIMEOUT",
    "ENV_MANIFEST",
    "ENV_CA_BUNDLE",
]
