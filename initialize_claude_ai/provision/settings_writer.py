"""Safe, auditable edits to Claude Code's ``settings.json``.

The operator's file is treated as theirs: by default initialize-claude-ai *merges* into
it, keeps a timestamped backup of whatever was there before, and reports the
exact keys it added or changed rather than silently rewriting the document.

The write is atomic (temp file + ``replace``) so an interrupted run can never
leave a half-written settings file behind, and the result is chmod 600 because
``settings.json`` may carry an ``apiKeyHelper`` command line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core import redact
from ..core.context import RunContext
from ..core.fileio import write_private
from ..core.logging_setup import get_logger

log = get_logger("provision.settings")


@dataclass
class SettingsChange:
    """One dotted key that the write would alter."""

    key: str
    old: Any
    new: Any

    @property
    def is_new(self) -> bool:
        return self.old is _MISSING

    @property
    def is_removal(self) -> bool:
        return self.new is _MISSING

    def describe(self) -> str:
        if self.is_removal:
            return f"{self.key}: {redact.redact(str(self.old))} -> (removed)"
        shown_new = redact.redact(str(self.new))
        if self.is_new:
            return f"{self.key} = {shown_new}"
        return f"{self.key}: {redact.redact(str(self.old))} -> {shown_new}"


@dataclass
class SettingsWriteResult:
    path: Path
    existed: bool = False
    written: bool = False
    dry_run: bool = False
    backup: Path | None = None
    changes: list[SettingsChange] = field(default_factory=list)
    error: str = ""
    merged: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "existed": self.existed,
            "written": self.written,
            "dry_run": self.dry_run,
            "backup": str(self.backup) if self.backup else "",
            "changes": [c.describe() for c in self.changes],
            "error": self.error,
        }


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_MISSING = _Missing()


class SettingsWriter:
    """Reads, diffs and writes a Claude Code settings document."""

    def __init__(self, ctx: RunContext, path: Path) -> None:
        self.ctx = ctx
        self.path = path
        cfg = ctx.settings.section("provision")
        self.merge = cfg.bool_("merge_settings", True)
        self.backup = cfg.bool_("backup_settings", True)
        self.backup_suffix = cfg.str_("backup_suffix", "%Y%m%d-%H%M%S")

    # -- reading ---------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """Load the current document; an unreadable file is reported, not lost."""
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except OSError as exc:
            raise SettingsReadError(f"cannot read {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SettingsReadError(f"{self.path} is not valid JSON: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    # -- writing ---------------------------------------------------------------

    def apply(self, updates: Mapping[str, Any], *, remove: Sequence[str] = ()) -> SettingsWriteResult:
        """Merge ``updates`` into the document and persist it if anything moved.

        ``remove`` names dotted keys to take *out*, which merging alone cannot
        do. It is for keys this program wrote itself and no longer stands
        behind -- a previous backend's environment variables, say. The caller
        is responsible for only ever naming keys it owns; nothing here guesses
        that a key is stale.
        """
        result = SettingsWriteResult(path=self.path, dry_run=self.ctx.dry_run)
        result.existed = self.path.is_file()

        try:
            current = self.read()
        except SettingsReadError as exc:
            result.error = str(exc)
            return result

        base = dict(current) if self.merge else {}
        merged = deep_merge(base, _prune(updates))
        for dotted in remove:
            drop(merged, dotted)
        result.merged = merged
        result.changes = diff(current, merged)

        if not result.changes:
            return result
        if self.ctx.dry_run:
            log.info("[dry-run] would update %d key(s) in %s", len(result.changes), self.path)
            return result

        if result.existed and self.backup:
            result.backup = self._backup()

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_private(self.path, json.dumps(merged, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            result.error = f"cannot write {self.path}: {exc}"
            return result

        result.written = True
        log.info("updated %d key(s) in %s", len(result.changes), self.path)
        return result

    def _backup(self) -> Path | None:
        stamp = datetime.now().strftime(self.backup_suffix)
        target = self.path.with_name(f"{self.path.name}.{stamp}.bak")
        try:
            write_private(target, self.path.read_bytes())
        except OSError as exc:
            log.warning("could not back up %s: %s", self.path, exc)
            return None
        return target


class SettingsReadError(Exception):
    """The existing settings file could not be parsed."""


# ----------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into a copy of ``base``; overlay wins."""
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


def flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted keys so a diff reads like a changelog."""
    if not isinstance(node, dict):
        return {prefix: node} if prefix else {}
    out: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def drop(node: dict[str, Any], dotted: str) -> bool:
    """Delete one dotted key from a nested document. True if it was there.

    Containers emptied by the deletion are left in place: an ``env: {}`` that
    the operator will see and understand beats silently reshaping parts of
    their document that were not the point of the edit.
    """
    head, _, tail = dotted.partition(".")
    if not tail:
        return node.pop(head, _MISSING) is not _MISSING
    child = node.get(head)
    return drop(child, tail) if isinstance(child, dict) else False


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[SettingsChange]:
    """Dotted-key changes from ``before`` to ``after``, removals included.

    A merge never deletes on its own, so a key missing from ``after`` got there
    through ``SettingsWriter.apply(remove=...)`` -- a deliberate retraction,
    and the single most important line in the report when it happens, because
    it is the one that says an old backend's routing is finally gone.
    """
    old = flatten(dict(before))
    new = flatten(dict(after))
    changes: list[SettingsChange] = []
    for key in sorted(new):
        previous = old.get(key, _MISSING)
        if previous is _MISSING or previous != new[key]:
            changes.append(SettingsChange(key=key, old=previous, new=new[key]))
    changes += [SettingsChange(key=key, old=old[key], new=_MISSING) for key in sorted(old) if key not in new]
    return changes


def _prune(node: Any) -> Any:
    """Drop empty values so an unset option never overwrites a real one."""
    if isinstance(node, Mapping):
        cleaned = {k: _prune(v) for k, v in node.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", {}, [])}
    return node


def settings_path(ctx: RunContext) -> Path:
    """Resolve the settings file for the configured scope."""
    cfg = ctx.settings.section("provision")
    filename = cfg.str_("settings_filename", "settings.json")
    scope = cfg.str_("scope", "user").strip().lower()
    if scope == "project":
        return Path.cwd() / cfg.str_("project_settings_dir", ".claude") / filename
    return ctx.claude_home / filename


def secret_settings_path(ctx: RunContext) -> Path:
    """The settings file a *credential* goes to: one Claude Code reads and nobody shares.

    The user's ``settings.json`` for the ``user`` scope. For the ``project``
    scope :func:`settings_path` is ``.claude/settings.json`` -- the file Claude
    Code means to be committed beside the code -- so a credential goes to the
    project's local file instead (``provision.project_local_settings_filename``),
    which Claude Code reads the same way and keeps out of version control.
    """
    cfg = ctx.settings.section("provision")
    scope = cfg.str_("scope", "user").strip().lower()
    if scope == "project":
        return (
            Path.cwd()
            / cfg.str_("project_settings_dir", ".claude")
            / cfg.str_("project_local_settings_filename", "settings.local.json")
        )
    return settings_path(ctx)


def env_settings_paths(ctx: RunContext) -> list[Path]:
    """Every settings file whose ``env`` this program writes -- shared first, private last (it wins)."""
    paths = [settings_path(ctx)]
    private = secret_settings_path(ctx)
    if private not in paths:
        paths.append(private)
    return paths


__all__ = [
    "SettingsWriter",
    "SettingsWriteResult",
    "SettingsChange",
    "SettingsReadError",
    "deep_merge",
    "flatten",
    "diff",
    "drop",
    "env_settings_paths",
    "secret_settings_path",
    "settings_path",
]
