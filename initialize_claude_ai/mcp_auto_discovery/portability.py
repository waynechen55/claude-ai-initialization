"""Will this entry still work tomorrow, or on another machine?

``.mcp.json`` exists to be copied. That is not an incidental property -- it is
the reason the briefing was taken *out* of the file, so that the whole thing can
be selected and pasted into another project, a commit, or a machine that has not
been set up yet. See :mod:`.writer`.

Discovery can undermine that in one move, by writing an absolute path into the
entry. Three grades, and only the worst is refused:

``PORTABLE``
    Nothing in the command is specific to this host. A URL, or a launcher that
    fetches what it needs when it starts -- ``npx -y``, ``uvx``,
    ``uv tool run``, ``docker run``. Copy it anywhere and it works.

``HOST_BOUND``
    It names a real file on this machine: a generated bridge, a system
    interpreter. It works *here*, it is registered, and the briefing says
    plainly that copying the file will not carry it.

``DISPOSABLE``
    It names something inside this program's own scratch space that was never
    meant to outlive the run -- a virtualenv built to trial a package, anything
    under the trial sandbox. Refused, because it is not durable even on the
    machine that made it.

The distinction between the last two is deliberately concrete rather than a
judgement: a virtualenv is identifiable (an ancestor directory holding
``pyvenv.cfg``), and the sandbox is a configured path. A ``.py`` file an agent
wrote into its artifacts directory is a real artifact and is kept; the venv it
pip-installed a package into is not.

*Being* a virtualenv is not the same as being a trial one, though, and treating
the two alike lost a server. On a host whose Python is externally managed
(PEP 668), phase 1 installs into its own venv (``install.venv_path``) and the
program runs from it -- so the bridge it generated for openqa.example.net named that
venv's interpreter, which is exactly as durable as the program itself. It was
refused as "built to trial a package", and the address ended with nothing. So a
venv at or under one of ``durable_dirs`` (``mcp.registration.durable_venv_roots``
plus the venv this program runs from) is graded like any other file on this
host: HOST_BOUND. Scratch space is still checked first and still wins.

This is the seventh instance of one bug -- the recorded claim outrunning the
evidence. Verification proved the command worked *at that instant*; nothing
asked whether it would still work afterwards, or anywhere else. An entry was
registered pointing at
``…/work/agent-workspace/artifacts/openqa-mcp-bridge/venv/bin/python3``, and on
any other machine that is ``ENOENT``.

This module also owns the other half of that question: **what an entry means at
the moment it is launched**. :func:`expand`, :func:`unresolved` and
:func:`launch_of` resolve ``${NAME}`` and ``${NAME:-default}`` the way the
client resolves them, which is what lets :meth:`.CandidateVerifier.verify_as_written`
start the finished entry rather than the candidate it was derived from. The two
belong together: grading a path and resolving a path are the same question asked
about a different moment, and an entry is portable only if it also *starts*.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .types import Candidate

#: ``${NAME}`` or ``${NAME:-default}`` -- the two forms Claude Code expands in
#: ``command``, ``args`` and ``env`` values, verified empirically against the
#: installed CLI. Mirrored here so this program can resolve an entry the same
#: way the client will, which is the only way to test what actually ships.
ENTRY_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

PORTABLE = "portable"
HOST_BOUND = "host-bound"
DISPOSABLE = "disposable"

#: Marks a directory as a Python virtual environment. Present in every venv
#: since PEP 405, which makes "is this inside a venv" a fact rather than a guess.
VENV_MARKER = "pyvenv.cfg"


@dataclass
class Portability:
    """How well an entry survives being copied, and what ties it down."""

    kind: str = PORTABLE
    reason: str = ""
    #: The absolute paths that tie this entry to this machine, if any.
    paths: list[str] = field(default_factory=list)

    @property
    def registrable(self) -> bool:
        return self.kind != DISPOSABLE

    def to_dict(self) -> dict[str, object]:
        return {"portability": self.kind, "reason": self.reason, "host_paths": self.paths}


def search_path(text: str) -> list[str] | None:
    """The directories of a search-path value (``/usr/bin:/bin``), or ``None`` if it is not one.

    A value is a search path when it holds the platform's list separator
    (:data:`os.pathsep`) and every part is an absolute path. It is a list of
    places to *look*, not a file the entry depends on -- a directory in it that
    does not exist is skipped by whoever searches it.
    """
    value = str(text or "")
    if os.pathsep not in value:
        return None
    parts = [part for part in value.split(os.pathsep) if part]
    if len(parts) < 2 or not all(part.startswith("/") for part in parts):
        return None
    return parts


def absolute_paths(candidate: Candidate) -> list[str]:
    """Every absolute path the entry would depend on when it starts.

    Arguments and environment values both count: a manifest named in ``env`` is
    as load-bearing as the interpreter named in ``command``, and an entry is
    only as portable as the least portable thing it needs. A search path is
    returned as its directories (:func:`search_path`) -- read whole, an agent's
    ``PATH=/usr/bin:/bin`` was "a file that is not there", and openSUSE's own
    ``osc-mcp`` was refused for build.opensuse.org before it was ever tried.
    """
    out: list[str] = []
    for token in (*candidate.full_command(), *candidate.env.values()):
        text = str(token)
        parts = search_path(text)
        if parts is not None:
            out.extend(parts)
        elif text.startswith("/"):
            out.append(text)
    return out


def credential_files(candidate: Candidate, is_credential_slot: Any) -> list[str]:
    """The absolute paths ``candidate``'s credential slots name -- a key file, a client certificate.

    build-example-net's SSH-signing bridge named ``/root/.ssh/id_ed25519`` in
    ``OBS_SSH_KEY``; the machine that ran the program had no such key, and
    the only bridge that could log in was refused as naming "a file that is
    not there" -- a password bridge the API host does not accept was written
    instead. A key file is the operator's credential, supplied per machine
    like a token. ``is_credential_slot(name)`` says which env keys hold one.
    """
    return [str(v) for k, v in candidate.env.items() if str(v).startswith(("/", "~")) and is_credential_slot(str(k))]


def search_path_directories(candidate: Candidate) -> set[str]:
    """Every directory that appears only as part of a search path in the entry."""
    found: set[str] = set()
    alone: set[str] = set()
    for token in (*candidate.full_command(), *candidate.env.values()):
        parts = search_path(str(token))
        if parts is None:
            alone.add(str(token))
        else:
            found.update(parts)
    return found - alone


def _inside_a_venv(path: Path) -> Path | None:
    """The virtualenv root containing ``path``, if it is inside one."""
    for parent in [path, *path.parents]:
        try:
            if (parent / VENV_MARKER).is_file():
                return parent
        except OSError:  # pragma: no cover - unreadable parent
            continue
    return None


def _roots(dirs: Sequence[str]) -> list[Path]:
    """Configured directories, resolved; unset (empty) entries dropped."""
    return [Path(os.path.expanduser(str(d))).resolve() for d in dirs if str(d).strip()]


def _under(path: Path, roots: Sequence[Path]) -> Path | None:
    """The first of ``roots`` that ``path`` is at or under, if any."""
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return None
    for root in roots:
        if resolved == root or root in resolved.parents:
            return root
    return None


def assess(
    candidate: Candidate,
    *,
    sandbox_dirs: Sequence[str] = (),
    durable_dirs: Sequence[str] = (),
    must_exist: bool = True,
    credential_files: Sequence[str] = (),
) -> Portability:
    """Grade one candidate.

    ``sandbox_dirs`` are scratch roots to refuse. ``durable_dirs`` are roots
    under which a virtualenv is kept on purpose -- this program's own, pipx's,
    ``uv tool``'s -- so a path inside one is host-bound, not disposable.
    ``credential_files`` are paths a credential slot names (an SSH key, a
    client certificate): absent on this machine, each is a credential still to
    supply, never a file the entry lost (:func:`credential_files`).
    """
    if candidate.kind != "stdio":
        # A URL plus ``${VAR}`` placeholders carries nothing machine-specific.
        return Portability(PORTABLE, "a URL and environment variables only")

    paths = absolute_paths(candidate)
    if not paths:
        return Portability(
            PORTABLE,
            "the launcher fetches what it needs at start-up; nothing here is a path on this host",
        )

    scratch = _roots(sandbox_dirs)
    durable = _roots(durable_dirs)
    # A directory that is only ever part of a search path may be absent: a
    # searcher skips it. It is still checked for scratch space and trial
    # virtualenvs, which nothing outside the run may depend on.
    searched = search_path_directories(candidate)
    missing: list[str] = []
    for text in paths:
        path = Path(text)

        root = _under(path, scratch)
        if root is not None:
            return Portability(
                DISPOSABLE,
                f"{text} is inside {root}, this program's scratch space, which nothing outside the run may depend on",
                [text],
            )
        venv = _inside_a_venv(path)
        if venv is not None and _under(venv, durable) is None:
            return Portability(
                DISPOSABLE,
                f"{text} is inside the virtualenv {venv}, which is not one of the durable "
                "virtualenv roots (mcp.registration.durable_venv_roots) and so is taken to "
                "have been built to trial a package; it is not durable even on this machine",
                [text],
            )
        if must_exist and text not in searched and text not in credential_files and not path.exists():
            missing.append(text)

    if missing:
        return Portability(
            DISPOSABLE,
            "the entry names "
            + ", ".join(missing)
            + ", which is not there -- verification proved the command worked at the time, "
            "not that it still does",
            missing,
        )
    return Portability(
        HOST_BOUND,
        "it names files on this machine (" + ", ".join(paths[:3]) + "), so copying "
        ".mcp.json elsewhere will not carry it",
        paths,
    )


def expand(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Resolve ``${NAME}`` and ``${NAME:-default}`` as the client will.

    An unset variable with no default is left **as written** rather than
    replaced with an empty string, because that is the case that matters and
    guessing either way hides it: an entry containing a literal ``${NAME}`` at
    launch is an entry whose author expected a value that is not there.
    """
    values = os.environ if environ is None else environ

    def one(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in values:
            return str(values[name])
        return match.group(0) if default is None else default

    return ENTRY_VARIABLE.sub(one, str(text))


def unresolved(text: str, environ: Mapping[str, str] | None = None) -> list[str]:
    """Variables in ``text`` that have neither a value nor a default."""
    values = os.environ if environ is None else environ
    return [m.group(1) for m in ENTRY_VARIABLE.finditer(str(text)) if m.group(2) is None and m.group(1) not in values]


def launch_of(
    entry: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> tuple[list[str], dict[str, str], list[str]]:
    """``(argv, env, unresolved)`` for a written stdio entry, as it will run.

    The point of resolving an entry rather than re-using the candidate it came
    from is that they are not the same thing, and the gap between them is where
    two servers broke. The candidate was trialled without ``API_HEADERS``; the
    entry shipped with ``"API_HEADERS": "${API_HEADERS}"``. Only the entry can
    answer "does the file we are about to write actually start?".
    """
    argv = [expand(str(entry.get("command", "")), environ)]
    argv += [expand(str(a), environ) for a in entry.get("args") or []]
    env = {str(k): expand(str(v), environ) for k, v in (entry.get("env") or {}).items()}
    gaps: list[str] = []
    for text in [*argv, *env.values()]:
        for name in unresolved(text, environ):
            if name not in gaps:
                gaps.append(name)
    return [a for a in argv if a], env, gaps


__all__ = [
    "DISPOSABLE",
    "ENTRY_VARIABLE",
    "HOST_BOUND",
    "PORTABLE",
    "Portability",
    "absolute_paths",
    "assess",
    "expand",
    "launch_of",
    "search_path",
    "unresolved",
]
