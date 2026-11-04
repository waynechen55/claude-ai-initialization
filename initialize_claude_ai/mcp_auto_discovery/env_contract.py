"""Every variable a server reads belongs in its ``env`` block.

An entry in ``.mcp.json`` is a contract with whoever copies it: *this is
everything the server needs*. The writer used to honour only half of it. It
wrote the variables a candidate **declared**, and a candidate declares what its
author remembered to mention -- which for a script is rarely everything the
script **reads**.

openqa.example.net is the case that forced this. The agent's bridge read six
variables -- ``MCP_BRIDGE_BASE_URL``, ``_MANIFEST``, ``_CA_BUNDLE``,
``_API_KEY``, ``_API_SECRET``, ``_TIMEOUT`` -- and declared one. The manifest
and the CA bundle were found "beside the script" by a fallback buried in the
source. Copied to another machine, the directory held a second, older manifest
under a near-identical name; the bridge loaded that one, rejected its schema
and exited, and the operator was shown ``Connection closed``. Nothing in the
entry said a manifest existed, where it was, or which variable chose it. The
generated bridge in :mod:`.bridge` made the same choice on purpose.

So the variables are now read from the source rather than from the author's
memory. For each file the entry launches that is text on this disk, the reads
are found by ``mcp.env_contract.read_patterns`` -- one regex per idiom, with a
``name`` group and an optional ``default`` group -- and each variable is given
the value **the script would have used anyway**:

* a literal default in the read itself (``os.environ.get("X", "30")``);
* otherwise a file the next few lines fall back to, when one exists beside the
  script -- ``os.environ.get("X") or os.path.join(HERE, "ops.json")`` is a read
  of ``X`` defaulting to that file;
* otherwise nothing, which is written ``${NAME:-}``.

That last form is deliberate and was checked against Claude Code rather than
assumed. ``${NAME:-}`` expands to an empty string when ``NAME`` is unset. A bare
``${NAME}`` that is unset is passed to the server **literally**, as the
characters ``${NAME}``, with a warning -- so a script testing "is it set?" sees
a value that is not one. Nested defaults (``${A:-${B:-x}}``) are not expanded
at all, which is why a path default is written through the ordinary path rules
in :meth:`~.writer.McpWriter.portable` instead of being wrapped twice.

Because every default is the script's own, the entry behaves as it did on the
machine that wrote it, and says out loud on every other machine what it needs.
The start check then launches the entry as written, so the claim is proved
rather than trusted, and a variable that turns out to change behaviour is
withdrawn by the ``withdraw_optional_reads`` repair in :mod:`.healing`.

What this cannot see is a variable read by code it does not scan: a package
installed by ``npx`` or ``uvx``, or a module the launched script imports. Those
are the author's to declare, which the skills require.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..core.config import Settings
from ..core.logging_setup import get_logger

log = get_logger("mcp.env_contract")

#: Defaults only; ``mcp.env_contract.read_patterns`` overrides them. Each needs
#: a ``name`` group and may have a ``default`` group. One per idiom rather than
#: one clever pattern, so an installation can add a language by adding a line.
READ_PATTERNS = (
    # Python: os.environ.get / os.getenv / environ.get, with an optional literal default.
    r"""(?:os\.)?(?:environ\.get|getenv)\(\s*["'](?P<name>[A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""(?:\s*,\s*["'](?P<default>[^"'\n]*)["'])?""",
    # Python: os.environ["X"]
    r"""(?:os\.)?environ\[\s*["'](?P<name>[A-Za-z_][A-Za-z0-9_]*)["']\s*\]""",
    # JavaScript / TypeScript: process.env.X or process.env["X"], with || or ?? "default".
    r"""process\.env(?:\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)|\[\s*["'](?P<name2>[A-Za-z_][A-Za-z0-9_]*)["']\s*\])"""
    r"""(?:\s*(?:\|\||\?\?)\s*["'](?P<default>[^"'\n]*)["'])?""",
    # Ruby: ENV["X"] / ENV.fetch("X", "default")
    r"""ENV(?:\.fetch\(|\[)\s*["'](?P<name>[A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""(?:\s*,\s*["'](?P<default>[^"'\n]*)["'])?""",
    # Go and Rust
    r'os\.(?:Getenv|LookupEnv)\(\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"',
    r'env::var(?:_os)?\(\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"',
    # POSIX shell: ${X} and ${X:-default}. Bare $X is left alone -- it is far
    # too often a positional or a loop variable to be read as configuration.
    r"""\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}\n]*))?\}""",
)

#: Variables that describe the host rather than configure the server. Defaults
#: only; ``mcp.env_contract.ignore`` overrides them. Writing ``HOME`` into an
#: entry would pin one machine's home directory into every copy of the file --
#: the opposite of the point.
IGNORE = (
    r"^(PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|TERM|LANG|LANGUAGE|TZ|HOSTNAME|DISPLAY)$",
    r"^(TMPDIR|TEMP|TMP)$",
    r"^(XDG|LC|PYTHON|VIRTUAL_ENV|CONDA)_?",
    r"^NODE_(ENV|OPTIONS|PATH|DEBUG|NO_WARNINGS)$",
    r"^(SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|NODE_EXTRA_CA_CERTS)$",
    r"^(HTTPS?_PROXY|NO_PROXY|ALL_PROXY|https?_proxy|no_proxy|all_proxy)$",
)

#: Defaults only; ``mcp.env_contract.session_variables`` overrides them. A
#: variable the operator's login session sets -- an SSH agent's socket, a
#: Kerberos cache -- is the session's, not a server's: written under its own
#: name as ``${NAME:-}``, never renamed per server, never a credential, never
#: something the operator is asked to export. build-example-net and
#: build-opensuse-org each read ``SSH_AUTH_SOCK`` and were written to expand
#: ``${BUILD_EXAMPLE_NET_SSH_AUTH_SOCK:-}`` and ``${BUILD_OPENSUSE_ORG_SSH_AUTH_SOCK:-}``
#: -- names nobody has, so the agent the session already runs was never reached.
SESSION_VARIABLES = (
    r"^SSH_AUTH_SOCK$",
    r"^SSH_AGENT_PID$",
    r"^GPG_AGENT_INFO$",
    r"^KRB5CCNAME$",
    r"^DBUS_SESSION_BUS_ADDRESS$",
    r"^XDG_RUNTIME_DIR$",
    r"^(WAYLAND_)?DISPLAY$",
)

#: Defaults only; ``mcp.env_contract.credential_in_default`` overrides it. A
#: default carrying a password in a URL (``https://user:pw@host``) is a secret
#: whatever the variable is called.
CREDENTIAL_IN_DEFAULT = r"://[^/@\s:]+:[^/@\s]+@"

#: A quoted string that could be a filename -- no whitespace, no quotes.
_QUOTED = re.compile(r"""["']([^"'\s]{1,255})["']""")

#: Defaults only; ``mcp.env_contract.fallback_literal_patterns`` overrides them.
#: A read with no default of its own whose *next* tokens fall back to a literal
#: -- ``os.environ.get("X") or "30"``, ``process.env.X || "30"``, also when the
#: fallback sits on the following line. Matched against what follows the read
#: (``default_window_lines``), each with a ``default`` group. The generated
#: gitlab.example.net bridge read ``MCP_BRIDGE_USER_AGENT`` this way; with its
#: default unseen, a value equal to it was mistaken for a credential.
#: Fallbacks for ``mcp.env_contract.constant_patterns`` / ``indirect_read_patterns``: a variable read
#: through a constant -- ``TOKEN_VAR = "GITLAB_EXAMPLE_NET_TOKEN"`` then ``os.environ[TOKEN_VAR]``.
#: Fallback for ``[mcp.env_contract] launcher_names``: what runs a server, never
#: the server -- matched whole against a launched file's name (and its target's,
#: through a symlink).
LAUNCHER_NAMES = (
    "npx",
    "npm",
    "pnpx",
    "pnpm",
    "yarn",
    "bunx",
    "bun",
    "deno",
    "node",
    "nodejs",
    "uvx",
    "uv",
    "pipx",
    "python[0-9.]*",
    "pypy[0-9.]*",
    "bash",
    "sh",
    "dash",
    "zsh",
    "env",
    "docker",
    "podman",
    "npx-cli\\.js",
    "npm-cli\\.js",
)
CONSTANT_PATTERNS = (
    r"""^\s*(?:(?:const|let|var|final|static|export)\s+)*(?P<const>[A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*\w+\s*)?=\s*["'](?P<value>[A-Za-z_][A-Za-z0-9_]*)["']\s*;?\s*(?:#.*|//.*)?$""",
)
INDIRECT_READ_PATTERNS = (
    r"""(?:os\.)?(?:environ\.get|getenv)\(\s*(?P<ref>[A-Za-z_][A-Za-z0-9_]*)\s*[,)]""",
    r"""environ\[\s*(?P<ref>[A-Za-z_][A-Za-z0-9_]*)\s*\]""",
    r"""(?P<ref>[A-Za-z_][A-Za-z0-9_]*)\s+(?:not\s+)?in\s+os\.environ""",
    r"""process\.env\[\s*(?P<ref>[A-Za-z_][A-Za-z0-9_]*)\s*\]""",
)

FALLBACK_LITERAL_PATTERNS = (r"""^\s*\)?\s*(?:or|\|\||\?\?)\s*["'](?P<default>[^"'\n]*)["']""",)


@dataclass(frozen=True)
class Read:
    """One variable one file reads, and what it falls back to when unset."""

    name: str
    default: str | None
    source: str
    line: int

    @property
    def is_path(self) -> bool:
        return bool(self.default) and str(self.default).startswith("/")


#: Where an installed package's own source lies, per launcher family; equal to
#: ``[mcp.env_contract.package_sources]``. ``launchers`` are command names,
#: ``skip`` words between the launcher and the package (``uv tool run``),
#: ``globs`` are searched under ``~`` with ``{module}`` the package's import or
#: directory name (``{package}`` as written, ``{module_name}`` with ``-`` as
#: ``_``), ``extensions`` the files read.
#: The patterns a *package's* source is read with: every language idiom but the
#: shell one, whose ``${X}`` is a template literal in JavaScript. Equal to
#: ``mcp.env_contract.package_read_patterns``.
PACKAGE_READ_PATTERNS = tuple(p for p in READ_PATTERNS if not p.startswith(r"\$\{"))

DEFAULT_PACKAGE_SOURCES = {
    "python": {
        "launchers": ["uvx", "pipx", "uv"],
        "skip": ["tool", "run", "--from"],
        "globs": [
            ".cache/uv/archive-v0/*/{module_name}",
            ".local/share/uv/tools/*/lib/python*/site-packages/{module_name}",
            ".local/pipx/venvs/*/lib/python*/site-packages/{module_name}",
        ],
        "extensions": [".py"],
    },
    "node": {
        "launchers": ["npx", "bunx", "pnpx"],
        "skip": ["-y", "--yes"],
        "globs": [".npm/_npx/*/node_modules/{package}"],
        "extensions": [".js", ".mjs", ".cjs"],
    },
}


class EnvContract:
    """Finds what a launched script reads, so the entry can declare all of it."""

    def __init__(self, settings: Settings) -> None:
        self.config = settings.section("mcp.env_contract")
        self.passthrough = set(settings.str_list("mcp.verification.client_env.passthrough", []))

    @property
    def enabled(self) -> bool:
        return self.config.bool_("enabled", True)

    def patterns(self, key: str = "read_patterns", default: Sequence[str] = READ_PATTERNS) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for raw in self.config.str_list(key, list(default)):
            try:
                compiled.append(re.compile(raw))
            except re.error as exc:
                log.warning("ignoring mcp.env_contract.%s entry %r: %s", key, raw, exc)
        return compiled

    @staticmethod
    def mentions(name: str, files: Sequence[Path]) -> bool:
        """Does ``name`` appear anywhere in ``files`` -- read, listed, or built from parts we cannot see?"""
        for path in files:
            try:
                if name in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
        return False

    def ignored(self, name: str) -> bool:
        if name in self.passthrough:
            # The client passes it through already; declaring it would pin this
            # host's value into the file.
            return True
        for raw in self.config.str_list("ignore", list(IGNORE)):
            try:
                if re.search(raw, name):
                    return True
            except re.error:
                continue
        return False

    def session(self, name: str) -> bool:
        """Is ``name`` set by the operator's login session (see :data:`SESSION_VARIABLES`)?"""
        for raw in self.config.str_list("session_variables", list(SESSION_VARIABLES)):
            try:
                if re.search(raw, str(name)):
                    return True
            except re.error:
                continue
        return False

    def embeds_a_credential(self, value: str) -> bool:
        """Does a default found in the source carry a credential inline?"""
        raw = self.config.str_("credential_in_default", CREDENTIAL_IN_DEFAULT)
        try:
            return bool(raw) and bool(re.search(raw, str(value)))
        except re.error:
            return True  # a broken pattern must not let a secret through

    # -- which files -----------------------------------------------------------

    def scripts_in(self, argv: Iterable[str]) -> list[Path]:
        """The launched files that are readable text on this disk.

        A package name is not a path and falls out on ``is_file``. An
        interpreter or launcher is never the server, binary or not
        (``launcher_names``): Homebrew's ``npx`` is a JavaScript file, its own
        ``process.env[key]`` was read as the server's, and gitlab-example-net's
        HEADERS slot was "renamed" to ``key``.
        """
        limit = self.config.int_("max_bytes", 2_000_000)
        launchers = [
            re.compile(f"(?:{p})\\Z") for p in self.config.str_list("launcher_names", list(LAUNCHER_NAMES)) if p
        ]
        found: list[Path] = []
        for token in argv:
            text = str(token)
            if not text or text.startswith("-") or "${" in text:
                continue
            path = Path(text)
            names = {path.name}
            try:
                names.add(path.resolve().name)
            except OSError:
                pass
            if any(p.match(n) for p in launchers for n in names):
                continue
            try:
                if not path.is_file() or path.stat().st_size > limit:
                    continue
                head = path.read_bytes()[:4096]
            except OSError:
                continue
            if b"\0" in head:
                continue
            if path not in found:
                found.append(path)
        return found

    def all_reads(self, argv: Sequence[str]) -> tuple[str, set[str], list[Path]]:
        """``(what, every variable it reads, the files read)`` for a launched script or an installed package.

        ``what`` names the reader (the script's file name or the package); an
        empty set means nothing readable was found.
        """
        scripts = self.scripts_in(argv)
        if scripts:
            return scripts[0].name, {read.name for script in scripts for read in self._scan(script)}, scripts
        package, reads, files = self._package(argv)
        return (package, reads, files) if reads else ("", set(), [])

    def package_reads(self, argv: Sequence[str]) -> tuple[str, set[str]]:
        """``(package, every variable its installed source reads)``; see :meth:`_package`."""
        package, reads, _ = self._package(argv)
        return package, reads

    def _package(self, argv: Sequence[str]) -> tuple[str, set[str], list[Path]]:
        """``(package, every variable its installed source reads)``; ``("", set())`` when unknown.

        A package's variables are what its own code reads, not what its
        README or an agent says: ``mcp-openapi-proxy`` reads its credential
        from ``API_KEY`` (or headers in ``EXTRA_HEADERS``), and build.example.net's
        entry put it under ``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD``, a name
        nothing reads. Only an installed copy found under the configured
        ``package_sources`` is read; nothing is downloaded for this.
        """
        if not self.enabled or not self.config.bool_("read_packages", True):
            return "", set(), []
        tokens = [str(t) for t in argv]
        sources = self.config.dict_("package_sources", DEFAULT_PACKAGE_SOURCES)
        for source in sources.values():
            if not isinstance(source, dict) or not tokens:
                continue
            if Path(tokens[0]).name not in [str(x) for x in source.get("launchers") or []]:
                continue
            skip = {str(x) for x in source.get("skip") or []}
            package = next((t for t in tokens[1:] if t not in skip and not t.startswith("-")), "")
            if not package:
                return "", set(), []
            name = self._package_name(package)
            files = self._package_files(name, source)
            patterns = self.patterns("package_read_patterns", PACKAGE_READ_PATTERNS)
            reads = {read.name for path in files for read in self._scan(path, patterns)}
            return name, reads, files
        return "", set(), []

    def package_of(self, argv: Sequence[str]) -> str:
        """The package a launcher line runs (``uvx mcp-bugzilla@1 ...`` -> ``mcp-bugzilla``); "" for anything else.

        Read with the same launcher families as the package's own source, and
        without reading any file: it names a server, it does not inspect one.
        """
        tokens = [str(t) for t in argv]
        if not tokens:
            return ""
        for source in self.config.dict_("package_sources", DEFAULT_PACKAGE_SOURCES).values():
            if not isinstance(source, dict) or Path(tokens[0]).name not in [
                str(x) for x in source.get("launchers") or []
            ]:
                continue
            skip = {str(x) for x in source.get("skip") or []}
            package = next((t for t in tokens[1:] if t not in skip and not t.startswith("-")), "")
            return self._package_name(package) if package else ""
        return ""

    @staticmethod
    def _package_name(spec: str) -> str:
        """``mcp-graphql@2.0.4`` -> ``mcp-graphql``; ``@scope/x@1`` -> ``@scope/x``; ``pkg==1`` -> ``pkg``."""
        text = re.split(r"[=<>~!\[]", spec, maxsplit=1)[0]
        at = text.rfind("@")
        return text[:at] if at > 0 else text

    def _package_files(self, package: str, source: dict) -> list[Path]:
        home = Path.home()
        limit = max(1, self.config.int_("max_package_files", 400))
        extensions = tuple(str(e) for e in source.get("extensions") or [])
        tokens = {"package": package, "module_name": package.split("/")[-1].replace("-", "_").replace(".", "_")}
        candidates: list[Path] = []
        for pattern in source.get("globs") or []:
            filled = str(pattern)
            for key, value in tokens.items():
                filled = filled.replace("{" + key + "}", value)
            candidates += [p for p in home.glob(filled) if p.is_dir()]
        if not candidates:
            return []
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        files = [
            p
            for p in newest.rglob("*")
            if p.suffix in extensions and p.is_file() and "node_modules" not in p.relative_to(newest).parts
        ]
        return sorted(files)[:limit]

    # -- what they read --------------------------------------------------------

    def reads_of(self, argv: Sequence[str]) -> list[Read]:
        """Every variable the launched scripts read, first occurrence wins."""
        if not self.enabled:
            return []
        reads: dict[str, Read] = {}
        for script in self.scripts_in(argv):
            for read in self._scan(script):
                if read.name not in reads and not self.ignored(read.name):
                    reads[read.name] = read
                elif read.name in reads and reads[read.name].default is None and read.default:
                    # A later read of the same variable that states a default
                    # is better evidence than an earlier one that did not.
                    reads[read.name] = read
        return list(reads.values())

    def _scan(self, script: Path, patterns: Sequence[re.Pattern[str]] | None = None) -> list[Read]:
        try:
            lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        window = max(0, self.config.int_("default_window_lines", 6))
        patterns = list(patterns) if patterns is not None else self.patterns()
        out: list[Read] = []
        for number, line in enumerate(lines):
            for pattern in patterns:
                for match in pattern.finditer(line):
                    groups = match.groupdict()
                    name = groups.get("name") or groups.get("name2") or ""
                    if not name:
                        continue
                    default = groups.get("default")
                    tail = "\n".join(lines[number : number + 1 + window])[match.end() :]
                    if not default:
                        # `get("X")` or `get("X", "")` -- empty when unset -- and a
                        # literal after it is what the script really uses.
                        # An empty literal (`or ""`) is still "unset": the file
                        # the following lines fall back to is looked for as before.
                        literal = self._fallback_literal(tail)
                        if literal:
                            default = literal
                    if default is not None:
                        default = self._as_file(default, script) or default
                    else:
                        default = self._fallback_file(tail, script, name)
                    out.append(Read(name, default, str(script), number + 1))
        if patterns is None or patterns == self.patterns():
            out += self._indirect_reads(lines, script, {r.name for r in out})
        return out

    def _indirect_reads(self, lines: Sequence[str], script: Path, known: set[str]) -> list[Read]:
        """Variables read through a constant naming them (``TOKEN_VAR = "X"; os.environ[TOKEN_VAR]``).

        gitlab-example-net's bridge read its token so; the scan saw no literal name
        and the file check called the token "never read". Only a name bound to
        a string literal in the same file, and then used in a read, counts.
        (``mcp.env_contract.constant_patterns``, ``indirect_read_patterns``.)
        """
        constants: dict[str, str] = {}
        for line in lines:
            for pattern in self.patterns("constant_patterns", CONSTANT_PATTERNS):
                found = pattern.match(line)
                if found:
                    constants.setdefault(found.group("const"), found.group("value"))
        if not constants:
            return []
        out: list[Read] = []
        for number, line in enumerate(lines):
            for pattern in self.patterns("indirect_read_patterns", INDIRECT_READ_PATTERNS):
                for match in pattern.finditer(line):
                    name = constants.get(match.group("ref") or "")
                    if name and name not in known:
                        known.add(name)
                        out.append(Read(name, None, str(script), number + 1))
        return out

    def _fallback_literal(self, tail: str) -> str | None:
        """The literal a read falls back to right after it (``… or "30"``), or ``None``."""
        for pattern in self.patterns("fallback_literal_patterns", FALLBACK_LITERAL_PATTERNS):
            found = pattern.match(tail)
            if found and "default" in found.groupdict():
                return found.group("default")
        return None

    @staticmethod
    def _as_file(text: str, script: Path) -> str:
        """``text`` as an absolute path, when it names a file beside the script."""
        if not text or "${" in text:
            return ""
        candidate = Path(text) if text.startswith("/") else script.parent / text
        try:
            return str(candidate.resolve()) if candidate.is_file() else ""
        except OSError:
            return ""

    def _fallback_file(self, tail: str, script: Path, name: str) -> str | None:
        """The file the lines after a read fall back to, if they name one here.

        Only a file that *exists* counts. A quoted string that happens to look
        like a filename is not evidence of anything; one that resolves beside
        the script is what the script would open.
        """
        for match in _QUOTED.finditer(tail):
            text = match.group(1)
            if text == name:
                continue
            found = self._as_file(text, script)
            if found and Path(found) != script.resolve():
                return found
        return None


__all__ = ["CREDENTIAL_IN_DEFAULT", "FALLBACK_LITERAL_PATTERNS", "IGNORE", "READ_PATTERNS", "EnvContract", "Read"]
