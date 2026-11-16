"""A bridge that lets the service's own client do the login.

Some services cannot be logged in to with anything a variable can carry:
api.example.net asks for ``Signature`` -- every request signed with the operator's
SSH key, which ``osc`` does -- and Kerberos, NTLM and single-sign-on portals are
the same. Every server the program could write sent a fixed header, so no value
of any variable could make build-example-net work.

The service's official client knows how to perform that login. So this
generates a small stdio MCP server whose tools are the product's read
operations, each run as the client's own read command
(``osc -A {api_url} api {path}``): argv only, never a shell; only the operations
listed in ``[mcp.discovery.clients.<name>]``, so nothing but reads can be asked.

It never depends on how the client is set up on this machine. At start-up the
bridge writes its *own* client configuration -- from the TOML ``config``
template and the entry's variables -- into a private temporary directory,
points the client at it (``config_variable``, ``OSC_CONFIG``) and points the
client's home and XDG directories there too (``isolate``), so the operator's
``~/.config/osc/oscrc`` is never read, whether it exists or not. The login
comes from the entry like every other server's: named variables, chosen by the
login the service asks for (``logins``: a user name and the path of the SSH
public key for ``Signature``, a user name and a password for Basic), described for what to
supply, empty until exported -- the same "waiting for a credential" as any
entry. The same entry works on any machine with the same variables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.config import fill
from ..core.context import RunContext
from ..core.fileio import write_generated
from ..core.logging_setup import get_logger
from .types import Candidate, Stage, Transport, slugify

log = get_logger("mcp.client_bridge")

ENV_MANIFEST = "MCP_CLIENT_MANIFEST"
#: The service the bridge is for -- named in the entry, so the entry says which
#: service it talks to, and the bridge says it in its instructions.
ENV_SERVICE = "MCP_CLIENT_SERVICE"


@dataclass
class ClientArtifacts:
    script: Path
    manifest: Path
    tool_count: int


def install_steps(client: Mapping[str, Any]) -> list[list[str]]:
    """The ways to install a client (``install``), each an argument vector, in the order they are tried."""
    return [[str(a) for a in step] for step in client.get("install") or [] if isinstance(step, (list, tuple)) and step]


def install_with_ways(
    ctx: RunContext, program: str, ways: Sequence[Sequence[str]], *, timeout: float, label: str = "mcp prerequisite"
) -> list[str]:
    """Run ``ways`` (a client's ``install`` steps) in order until ``program`` is on the PATH; the ones tried.

    Only steps whose own installer is present are run. Shared by phase 4's
    trial (:meth:`.verifier.CandidateVerifier._ensure_required`) and phase 1,
    which installs what a registered entry calls before Claude Code starts it.
    """
    from .locks import INSTALL_STEPS

    tried: list[str] = []
    for way in ways:
        way = [str(a) for a in way]
        if not way or not ctx.runner.which(way[0]):
            continue
        tried.append(" ".join(way))
        with INSTALL_STEPS(tuple(way)):
            ctx.runner.run(way, timeout=timeout, label=label)
        if ctx.runner.which(program):
            log.info("installed %s with %s", program, " ".join(way))
            break
    return tried


def requirements(candidate: Candidate, settings: Any) -> dict[str, list[list[str]]]:
    """The programs ``candidate`` drives at call time, with how to install each.

    Its own record first; else, for an entry written earlier (reused, or copied
    from another machine), what its manifest names: the client and the binary
    its read command runs. build.opensuse.org's client bridge was judged on a
    machine without ``osc`` as "the call failed for another reason", replaced,
    and the same bridge for build.example.net was written there anyway.
    """
    if candidate.requires:
        return dict(candidate.requires)
    from .portability import expand

    path = expand(str(candidate.env.get(ENV_MANIFEST) or ""))
    if not path:
        return {}
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    read = manifest.get("read") or []
    binary = str(read[0]) if read else ""
    if not binary:
        return {}
    client = clients(settings).get(str(manifest.get("client") or ""), {})
    return {binary: install_steps(client)}


def clients(settings: Any) -> dict[str, Mapping[str, Any]]:
    return {str(k): v for k, v in settings.dict_("mcp.discovery.clients", {}).items() if isinstance(v, Mapping)}


def client_for(settings: Any, plan: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """The catalogue client the login plan chose (or offers), and its table; ("", {}) if none."""
    logins = [plan.get("chosen") or {}, *(plan.get("alternatives") or [])]
    table = clients(settings)
    for login in logins:
        name = str(login.get("client") or "")
        if login.get("method") == "client" and name in table:
            return name, table[name]
    return "", {}


def routes(
    client: Mapping[str, Any], plan: Mapping[str, Any], host: str, settings: Any
) -> list[tuple[str, list[str], str]]:
    """Every ``(scheme, parts, api host)`` the client can perform and the service offered, in ``method_order``.

    One bridge per route, each proved on its own: build.example.net offers Basic on
    the address and Signature on api.example.net, and which one this account can
    use is a question for a call, not for the order of a list.
    """
    from .login_methods import DEFAULT_METHOD_ORDER, api_hosts, classify

    logins = {
        str(k).lower(): [str(p) for p in v] for k, v in (client.get("logins") or {}).items() if isinstance(v, list)
    }
    order = settings.str_list("mcp.discovery.auth.method_order", list(DEFAULT_METHOD_ORDER))
    preferred = api_hosts(host, settings)
    offered = sorted(plan.get("offered") or [], key=lambda o: (str(o.get("host")) not in preferred,))
    found: list[tuple[str, list[str], str]] = []
    equal = settings.bool_("mcp.discovery.auth.methods_equal", True)
    if equal:
        # Every login the client can perform and the service offered, in the
        # order found -- no kind of login preferred (an SSH-key signature
        # counts as much as a password); evidence orders them below.
        for o in offered:
            scheme = str(o.get("scheme") or "").lower()
            if scheme in logins and all(r[0] != scheme for r in found):
                found.append((scheme, logins[scheme], str(o.get("host") or host)))
    else:
        for method in order:
            for o in offered:
                scheme = str(o.get("scheme") or "").lower()
                if (
                    scheme in logins
                    and classify(scheme, settings).get("method") == method
                    and all(r[0] != scheme for r in found)
                ):
                    found.append((scheme, logins[scheme], str(o.get("host") or host)))
    if not found:
        first = next(iter(logins), "")
        found.append((first, logins.get(first, []), host))
    # Evidence, before anything is proved by a call: what this host accepted
    # before first and what it refused (never accepted) last -- build.example.net
    # refused the password login every run and every run tried it first
    # (login_history; plan["history"]); then a login its API host itself
    # offers; then one this machine holds what it needs for.
    history = {str(k).lower(): str(v) for k, v in (plan.get("history") or {}).items()}
    order_of = {"accepted": 0, "": 1, "refused": 2}
    index = {r[0]: i for i, r in enumerate(found)}
    found.sort(
        key=lambda r: (
            order_of.get(history.get(r[0], ""), 1),
            0 if r[2] in preferred else 1,
            0 if holds_for(client, r[0], r[1], plan, settings) else 1,
            index[r[0]],
        )
    )
    return found


def holds_for(
    client: Mapping[str, Any], scheme: str, parts: Sequence[str], plan: Mapping[str, Any], settings: Any
) -> bool:
    """Does this machine hold what the ``scheme`` login needs -- evidence, never a preference for a kind?

    A key-file login: one of the client's ``key_files`` exists (or its key
    variable is set). Any other: the plan found that method held -- exported,
    or sent by an entry here now (``plan["held"]``, ``plan["in_use"]``).
    """
    from .login_methods import classify

    key_part = str(client.get("key_part") or "")
    if key_part and key_part in parts:
        files = [Path(str(f)).expanduser() for f in client.get("key_files") or []]
        return any(f.is_file() for f in files)
    method = classify(scheme, settings).get("method") or scheme
    return method in (plan.get("held") or {}) or method in (plan.get("in_use") or {})


def route(client: Mapping[str, Any], plan: Mapping[str, Any], host: str, settings: Any) -> tuple[str, list[str], str]:
    """``(scheme, parts, api host)``: how the client logs in, and where.

    The first of :func:`routes` -- evidence first, no kind of login preferred
    (``mcp.discovery.auth.methods_equal``); with that off, the first in
    ``method_order``. The host is the one that offered *that* scheme,
    an API host (``api_host_patterns``) before the address itself:
    build.opensuse.org's own "API login" does not know an openSUSE account,
    api.opensuse.org does.
    """
    from .login_methods import DEFAULT_METHOD_ORDER, api_hosts, classify

    if settings.bool_("mcp.discovery.auth.methods_equal", True):
        return routes(client, plan, host, settings)[0]
    logins = {
        str(k).lower(): [str(p) for p in v] for k, v in (client.get("logins") or {}).items() if isinstance(v, list)
    }
    order = settings.str_list("mcp.discovery.auth.method_order", list(DEFAULT_METHOD_ORDER))
    preferred = api_hosts(host, settings)
    offered = sorted(plan.get("offered") or [], key=lambda o: (str(o.get("host")) not in preferred,))
    for method in order:
        for o in offered:
            scheme = str(o.get("scheme") or "").lower()
            if scheme in logins and classify(scheme, settings).get("method") == method:
                return scheme, logins[scheme], str(o.get("host") or host)
    first = next(iter(logins), "")
    return first, logins.get(first, []), host


def generate(
    ctx: RunContext, target: str, host: str, plan: Mapping[str, Any]
) -> tuple[Candidate | None, ClientArtifacts | None, str]:
    """The client bridge for ``target``'s first route (see :func:`generate_all`); why not otherwise."""
    made = generate_all(ctx, target, host, plan)
    return made[0] if made else (None, None, "the login plan names no client this program knows how to drive")


def generate_all(
    ctx: RunContext, target: str, host: str, plan: Mapping[str, Any]
) -> list[tuple[Candidate | None, ClientArtifacts | None, str]]:
    """One client bridge per route the client can perform (:func:`routes`); [] when the plan names no client."""
    name, client = client_for(ctx.settings, plan)
    if not name:
        return []
    return [_generate(ctx, target, host, name, client, route_) for route_ in routes(client, plan, host, ctx.settings)]


def _generate(
    ctx: RunContext, target: str, host: str, name: str, client: Mapping[str, Any], route_: tuple[str, list[str], str]
) -> tuple[Candidate | None, ClientArtifacts | None, str]:
    """The client bridge for one route."""
    import hashlib

    from . import bridge_auth
    from .bridge import BridgeBuilder

    operations = [dict(op) for op in client.get("operations") or [] if isinstance(op, Mapping) and op.get("name")]
    if not operations:
        return None, None, f"[mcp.discovery.clients.{name}] lists no operations"
    scheme, parts, api_host = route_
    tokens = {
        "binary": str(client.get("binary") or name),
        "api_url": f"https://{api_host}",
        "host": api_host,
        "origin": f"https://{host}",
    }
    slug_upper = slugify(host)
    variables = {part: bridge_auth.exported_name(slug_upper, part, ctx.settings) for part in parts}
    line_templates = {str(k): str(v) for k, v in (client.get("lines") or {}).items()}
    manifest = {
        "server": f"{slugify(host)}-client-bridge",
        "client": name,
        "instructions": fill(str(client.get("instructions") or f"Read-only access to {host} through {name}."), tokens),
        "read": [fill(str(part), tokens) for part in client.get("read") or []],
        # The client's own configuration, written by the bridge at start-up from
        # these variables -- never the operator's.
        "config": fill(str(client.get("config") or ""), tokens),
        "config_variable": str(client.get("config_variable") or ""),
        "isolate": [str(v) for v in client.get("isolate") or []],
        "parts": {variables[part]: line_templates.get(part, "") for part in parts},
        "extra_lines": [str(x) for x in (client.get("extra_lines") or {}).get(scheme, [])],
        # Parts whose value must be the path of an existing file (an SSH public
        # key): checked before the client runs, so a token or a name exported
        # there is named plainly instead of failing inside the client.
        "file_parts": [variables[part] for part in parts if part in [str(x) for x in client.get("file_parts") or []]],
        # Parts the bridge can do without -- an SSH key file, when the standard
        # ones (key_files) are tried in its place: never waited for.
        "optional_parts": [
            variables[part] for part in parts if part in [str(x) for x in client.get("optional_parts") or []]
        ],
        "key_part": variables.get(str(client.get("key_part") or ""), ""),
        "key_files": [str(k) for k in client.get("key_files") or []]
        if str(client.get("key_part") or "") in parts
        else [],
        "key_refused_markers": [str(m) for m in client.get("key_refused_markers") or []],
        "key_register_hint": fill(str(client.get("key_register_hint") or ""), tokens),
        "timeout": float(client.get("timeout_seconds") or 60),
        "tools": [
            {
                "name": str(op["name"]),
                "summary": str(op.get("summary") or op["name"]),
                "command": [fill(str(part), tokens) for part in op.get("command") or []],
                "path": str(op.get("path") or ""),
                "params": {str(k): str(v) for k, v in (op.get("params") or {}).items()},
            }
            for op in operations
        ],
    }
    if not manifest["read"]:
        return None, None, f"[mcp.discovery.clients.{name}] has no read command"
    builder = BridgeBuilder(ctx)
    out_dir = builder.config.path("output_dir") or (ctx.work_dir / "bridges")
    slug = slugify(host).replace("-", "_")
    script = Path(out_dir) / f"mcp_client_bridge_{slug}.py"
    # Named by route and content: a later run generating another route must
    # not change what an existing entry runs. build-example-net's entry declared
    # SSH-key slots while its manifest -- one file per address, rewritten --
    # had become the password route, and asked for a password nobody passed.
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:8]
    manifest_file = Path(out_dir) / f"{slugify(host)}.{scheme or 'client'}.{digest}.client.json"
    ctx.ensure_dir(Path(out_dir))
    try:
        write_generated(manifest_file, json.dumps(manifest, indent=2) + "\n")
        write_generated(script, _SOURCE, executable=True)
    except OSError as exc:
        return None, None, f"cannot write the client bridge: {exc}"
    env = {ENV_MANIFEST: str(manifest_file), ENV_SERVICE: f"https://{host}"}
    for variable in client.get("env_passthrough") or []:
        env[str(variable)] = f"${{{variable}:-}}"
    candidate = Candidate(
        kind="stdio",
        name=f"{slugify(host)}-client-bridge" + (f"-{scheme}" if scheme else ""),
        command=[builder.interpreter(), str(script)],
        env=env,
        transport=Transport.STDIO,
        origin=Stage.BRIDGE,
        source=f"generated to drive {name}, which performs {host}'s login itself",
        confidence=0.75,
        auth_method_evidence=f"the client {name}, which performs the {scheme or 'service'} login {api_host} asks for",
        login_scheme=scheme,
    )
    optional = set(manifest["optional_parts"])
    for part, variable in variables.items():
        if variable in optional:
            # A location, not a credential, and not needed: written so it can
            # be set; launched without it, the bridge tries key_files.
            candidate.env[variable] = f"${{{variable}:-}}"
            continue
        candidate.secret_env[variable] = f"env:{variable}"
        candidate.secret_formats[variable] = bridge_auth.describe_part(part, api_host, ctx.settings)
        candidate.secret_sources[variable] = api_host
    candidate.add_note(
        f"Drives {name} for {len(operations)} read operation(s) at {tokens['api_url']}, with a configuration of "
        f"its own written at start-up from {', '.join(variables.values()) or 'no variable'} -- never this "
        f"machine's {name} setup, whether there is one or not."
    )
    candidate.requires[tokens["binary"]] = install_steps(client)
    if not ctx.runner.which(tokens["binary"]):
        candidate.add_note(
            f"{tokens['binary']} is not installed on this machine; the trial installs it first "
            f"([mcp.discovery.clients.{name}] install)"
        )
    return (
        candidate,
        ClientArtifacts(script, manifest_file, len(operations)),
        (f"generated {script.name}, driving {name} with {len(operations)} tool(s)"),
    )


_SOURCE = r'''#!/usr/bin/env python3
"""MCP stdio bridge that drives a service's own client, generated by initialize-claude-ai.

Each tool runs the client's read command from the manifest ($MCP_CLIENT_MANIFEST):
argv only, never a shell; the manifest lists only read operations; the login is
the client's own (its configuration, its agent). Standard library only.
"""
import atexit, json, os, shutil, signal, subprocess, sys, tempfile, urllib.parse

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
CLIENT_ENV = None
BASE_LINES = []
HOME = ""
KEY = None
MISSING = []
ABSENT = []
WRONG = []


def prepare(manifest):
    """The client's own configuration and environment -- never the operator's setup."""
    global CLIENT_ENV, MISSING, WRONG, ABSENT
    env = dict(os.environ)
    home = tempfile.mkdtemp(prefix="mcp-client-")
    # Marked as initialize-claude-ai's, so its cleanup may remove one a killed
    # bridge left behind -- and nothing else named like it.
    try:
        open(os.path.join(home, ".initialize-claude-ai-owned"), "w").close()
    except OSError:
        pass
    # Removed when the bridge ends -- also when the client stops it: one was
    # left behind per start, 683 on one machine.
    atexit.register(shutil.rmtree, home, True)
    for name in manifest.get("isolate", []):
        env[name] = home
    lines = []
    MISSING = []
    WRONG = []
    ABSENT = []
    for variable, template in manifest.get("parts", {}).items():
        if variable == manifest.get("key_part"):
            continue  # chosen per call: see keys()
        value = os.environ.get(variable, "")
        if not value:
            if variable not in manifest.get("optional_parts", []):
                MISSING.append(variable)
                if variable not in os.environ:
                    # Not passed at all: the client hands a server only its
                    # entry's env, so exporting it cannot help -- the entry must name it.
                    ABSENT.append(variable)
        elif variable in manifest.get("file_parts", []) and not os.path.isfile(value):
            WRONG.append(variable)
        elif template:
            lines.append(template.replace("{value}", value))
    lines += manifest.get("extra_lines", [])
    global BASE_LINES, HOME
    BASE_LINES, HOME = lines, home
    CLIENT_ENV = env
    configure(manifest, None)


def configure(manifest, key):
    """Write the client's own configuration, with ``key`` (a key file) when the route signs."""
    lines = list(BASE_LINES)
    template = manifest.get("parts", {}).get(manifest.get("key_part", ""), "")
    if key and template:
        lines.append(template.replace("{value}", key))
    if manifest.get("config") and manifest.get("config_variable"):
        path = os.path.join(HOME, "client.conf")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(manifest["config"].replace("{lines}", "\n".join(lines)) + "\n")
        CLIENT_ENV[manifest["config_variable"]] = path


def keys(manifest):
    """The key files to sign with: the one the entry names, else the standard ones that exist.

    A .pub signs only through an ssh-agent; with none running, its private
    sibling is used -- the way ssh itself picks a key.
    """
    named = os.environ.get(manifest.get("key_part", ""), "") if manifest.get("key_part") else ""
    found = [named] if named else [os.path.expanduser(k) for k in manifest.get("key_files", [])]
    out = []
    for key in found:
        if key.endswith(".pub") and not os.environ.get("SSH_AUTH_SOCK") and os.path.isfile(key[:-4]):
            key = key[:-4]
        if os.path.isfile(key) and key not in out:
            out.append(key)
    return out


def load():
    with open(os.environ["MCP_CLIENT_MANIFEST"], encoding="utf-8") as handle:
        return json.load(handle)


def schema(tool):
    props = {k: {"type": "string", "description": v} for k, v in tool.get("params", {}).items()}
    return {"type": "object", "properties": props, "required": sorted(props)}


def argv_for(manifest, tool, args):
    values = {k: urllib.parse.quote(str(args.get(k, "")).replace("'", ""), safe="") for k in tool.get("params", {})}
    if tool.get("command"):
        return [part.format(**values) for part in tool["command"]]
    path = tool["path"].format(**values)
    return [part.replace("{path}", path) for part in manifest["read"]]


def call(manifest, name, args):
    tool = next((t for t in manifest["tools"] if t["name"] == name), None)
    if tool is None:
        return {"isError": True, "content": [{"type": "text", "text": "unknown tool: " + name}]}
    missing = [k for k in tool.get("params", {}) if not str(args.get(k, "")).strip()]
    if missing:
        return {"isError": True, "content": [{"type": "text", "text": "missing: " + ", ".join(missing)}]}
    if ABSENT:
        return {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: the entry does not pass "
                                              + ", ".join(ABSENT) + " -- add " + ", ".join(
                                                  '"%s": "${%s:-}"' % (v, v) for v in ABSENT)
                                              + " to its env (the client passes a server only its entry's env), "
                                              "then export it in the shell you start Claude Code from"}]}
    if MISSING:
        return {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: " + ", ".join(MISSING)
                                              + " not set -- export it in the shell you start Claude Code from"}]}
    if WRONG:
        return {"isError": True, "content": [{"type": "text", "text": "misconfigured: " + ", ".join(WRONG)
                                              + " must be the path of an existing file (the key's .pub file), and it is not"}]}
    global KEY
    signing = bool(manifest.get("key_part"))
    named = os.environ.get(manifest.get("key_part", ""), "") if signing else ""
    if named and not os.path.isfile(os.path.expanduser(named)):
        return {"isError": True, "content": [{"type": "text", "text": "misconfigured: " + manifest["key_part"]
                                              + " must be the path of an existing key file, and it is not"}]}
    tried = []
    for key in ([KEY] if KEY else keys(manifest)) if signing else [None]:
        if signing:
            configure(manifest, key)
        try:
            run = subprocess.run(argv_for(manifest, tool, args), stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=manifest.get("timeout", 60), env=CLIENT_ENV)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"{manifest['client']} did not run: {exc}"}]}
        text = (run.stderr or run.stdout or "").strip()
        if run.returncode == 0:
            KEY = key
            return {"content": [{"type": "text", "text": run.stdout[:1000000]}]}
        if not signing:
            return {"isError": True, "content": [{"type": "text", "text": f"{manifest['client']} exited {run.returncode}: {text[-2000:]}"}]}
        refused = any(m and m.lower() in text.lower() for m in manifest.get("key_refused_markers", []))
        tried.append(f"{key}: " + ("the service refused its signature (the key is not registered with your account)"
                                   if refused else "could not sign with it (" + (text.splitlines() or ["?"])[-1][:160] + ")"))
    if signing and not tried:
        return {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: no SSH key to sign with -- set "
                                              + manifest.get("key_part", "") + " to your key file. " + manifest.get("key_register_hint", "")}]}
    return {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: every SSH key was tried -- "
                                          + "; ".join(tried) + ". " + manifest.get("key_register_hint", "")}]}


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so atexit runs when the client stops us
    manifest = load()
    prepare(manifest)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if rid is None:
            continue
        if method == "initialize":
            asked = params.get("protocolVersion")
            service = os.environ.get("MCP_CLIENT_SERVICE", "")
            result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": manifest["server"], "version": "1.0.0"},
                      "instructions": manifest.get("instructions", "") + (f" Service: {service}." if service else "")}
        elif method == "tools/list":
            result = {"tools": [{"name": t["name"], "description": t["summary"], "inputSchema": schema(t),
                                 "annotations": {"readOnlyHint": True}} for t in manifest["tools"]]}
        elif method == "tools/call":
            result = call(manifest, params.get("name", ""), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''

__all__ = ["ClientArtifacts", "client_for", "clients", "generate", "generate_all", "route", "routes"]
