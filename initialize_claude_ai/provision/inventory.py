"""The "list out all associated information" half of phase 3.

Everything an operator needs to answer *what did this machine end up with?* --
platform, binaries and versions, authenticated identities, the active backend,
project/region, the model line-up, the settings file, registered MCP servers and
the relevant environment.

Every value passes through two filters before it is shown: the global redaction
filter (which knows the shape of tokens and keys and the exact values that have
been resolved this run), and a name-based mask for variables whose *name* says
they are sensitive even when the value looks innocuous.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core import redact
from ..core.context import RunContext
from ..core.logging_setup import Console, get_logger

log = get_logger("provision.inventory")

#: Shown instead of a value whose variable name matches a sensitive pattern.
PRESENT = "<set>"
ABSENT = "<unset>"


@dataclass
class Inventory:
    """A structured, already-redacted snapshot of the Claude environment."""

    platform: dict[str, Any] = field(default_factory=dict)
    binaries: list[dict[str, str]] = field(default_factory=list)
    authentication: list[dict[str, Any]] = field(default_factory=list)
    backend: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    mcp_servers: list[dict[str, str]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "binaries": self.binaries,
            "authentication": self.authentication,
            "backend": self.backend,
            "models": self.models,
            "settings": self.settings,
            "mcp_servers": self.mcp_servers,
            "environment": self.environment,
            "notes": self.notes,
        }


class InventoryCollector:
    """Builds an :class:`Inventory` from the run context and phase results."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.config = ctx.settings.section("provision.inventory")
        self.sensitive = self.config.str_list("sensitive_name_patterns")

    # -- collection ------------------------------------------------------------

    def collect(
        self,
        *,
        backend: Mapping[str, Any] | None = None,
        models: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Inventory:
        inv = Inventory()
        inv.platform = self.ctx.platform.to_dict()
        inv.backend = dict(backend or {})
        inv.models = dict(models or {})

        if self.config.bool_("show_binaries", True):
            inv.binaries = self._binaries()
        if self.config.bool_("show_auth", True):
            inv.authentication = self._authentication()
        if self.config.bool_("show_settings", True):
            inv.settings = dict(settings or {})
        if self.config.bool_("show_mcp_servers", True):
            inv.mcp_servers = self._mcp_servers()
        if self.config.bool_("show_env", True):
            inv.environment = self._environment()
        return inv

    def _binaries(self) -> list[dict[str, str]]:
        """Resolve each declared binary and, when asked, ask it its version."""
        want_versions = self.config.bool_("show_versions", True)
        aliases = self._binary_aliases()
        rows: list[dict[str, str]] = []
        for name, flags in self.config.dict_("binaries").items():
            path = self.ctx.tool(name, aliases=aliases.get(name, ()))
            row = {"name": name, "path": path or "", "version": ""}
            if not path:
                row["version"] = "not installed"
                rows.append(row)
                continue
            recorded = self.ctx.env.get(f"VERSION_{name.upper().replace('-', '_')}")
            if recorded:
                row["version"] = recorded
            elif want_versions and isinstance(flags, list) and flags:
                probe = self.ctx.runner.probe(
                    [path, *[str(f) for f in flags]], timeout=self.config.float_("version_timeout_seconds", 20)
                )
                row["version"] = probe.first_line()[:80] if probe.ok else "(version unavailable)"
            rows.append(row)
        return rows

    def _binary_aliases(self) -> dict[str, list[str]]:
        """Component key -> the binary names phase 1 accepts for it."""
        from ..install.components import load_components

        try:
            specs = load_components(self.ctx.settings)
        except Exception as exc:  # noqa: BLE001 - an inventory must never be fatal
            log.debug("component catalog unavailable for alias lookup: %s", exc)
            return {}
        return {spec.key: spec.binary_names() for spec in specs}

    def _authentication(self) -> list[dict[str, Any]]:
        """Report phase 2's outcomes; identities only, never credentials."""
        outcomes = self.ctx.state.get("auth_outcomes") or {}
        rows: list[dict[str, Any]] = []
        for key, outcome in outcomes.items():
            rows.append(
                {
                    "backend": key,
                    "status": getattr(outcome, "status", ""),
                    "identity": redact.redact(getattr(outcome, "identity", "") or "-"),
                    "method": getattr(outcome, "method", ""),
                    "verified": bool(getattr(outcome, "verified", False)),
                }
            )
        if not rows:
            rows.append(
                {
                    "backend": "-",
                    "status": "unknown",
                    "identity": "-",
                    "method": "phase 2 did not run in this invocation",
                    "verified": False,
                }
            )
        return rows

    def _mcp_servers(self) -> list[dict[str, str]]:
        """Ask the CLI what is registered. Phase 4 is what *adds* servers."""
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            return []
        argv = [
            str(part).replace("{claude}", claude)
            for part in self.config.list_("commands.claude_mcp_list", ["{claude}", "mcp", "list"])
        ]
        if not argv:
            return []
        probe = self.ctx.runner.probe(argv, timeout=self.config.float_("command_timeout_seconds", 45))
        if not probe.ok:
            return [{"name": "(unavailable)", "detail": f"`{' '.join(argv)}` failed"}]
        rows: list[dict[str, str]] = []
        for line in probe.stdout.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, detail = line.partition(":")
            name = name.strip()
            # Skip the CLI's own headings ("Checking MCP server health...").
            if not name or name.endswith("...") or " " in name:
                continue
            rows.append({"name": name, "detail": redact.redact(detail.strip())[:120]})
        return rows

    def _environment(self) -> dict[str, str]:
        """The Claude-relevant environment, masked by value *and* by name."""
        patterns = self.config.str_list(
            "env_report_patterns",
            [
                "ANTHROPIC_*",
                "CLAUDE_*",
                "CLOUD_ML_REGION",
                "GOOGLE_CLOUD_*",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "CLOUDSDK_*",
                "AWS_REGION",
                "MCP_*",
            ],
        )
        out: dict[str, str] = {}
        for name in sorted(os.environ):
            if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
                continue
            out[name] = self.mask(name, os.environ[name])
        return out

    def mask(self, name: str, value: str) -> str:
        """Mask by variable name first, then scrub whatever survives."""
        if not value:
            return ABSENT
        if any(fnmatch.fnmatchcase(name.upper(), p.upper()) for p in self.sensitive):
            return PRESENT
        return redact.redact(value)


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------


def render(console: Console, inv: Inventory) -> None:
    """Print the inventory as the closing act of phase 3."""
    console.write("")
    console.rule("Claude environment")

    if inv.backend:
        console.header("Backend")
        for key in ("backend", "account", "project", "region", "endpoint", "model"):
            if inv.backend.get(key):
                console.kv(key.replace("_", " ").title(), inv.backend[key])

    if inv.binaries:
        console.header("Binaries")
        console.table(
            [[b["name"], b["path"] or "-", b["version"] or "-"] for b in inv.binaries],
            headers=["component", "path", "version"],
        )

    if inv.authentication:
        console.header("Authentication")
        console.table(
            [
                [
                    a["backend"],
                    a["status"],
                    a["identity"],
                    a["method"] or "-",
                    "yes" if a["verified"] else "no",
                ]
                for a in inv.authentication
            ],
            headers=["backend", "status", "identity", "method", "verified"],
        )

    if inv.models:
        console.header("Models")
        for tier, model in (inv.models.get("tiers") or {}).items():
            console.kv(tier, model or "(unset)")
        available = inv.models.get("available") or []
        if available:
            console.kv("Available", f"{len(available)} offered by {inv.models.get('endpoint', '?')}")
            for entry in available[: inv.models.get("display_limit", 12)]:
                console.bullet(entry, indent=4)
            if len(available) > inv.models.get("display_limit", 12):
                console.bullet(f"... and {len(available) - 12} more", indent=4)
        elif inv.models.get("detail"):
            console.kv("Discovery", inv.models["detail"])

    if inv.settings:
        console.header("Settings")
        console.kv("File", inv.settings.get("path", "-"))
        if inv.settings.get("backup"):
            console.kv("Backup", inv.settings["backup"])
        changes = inv.settings.get("changes") or []
        if changes:
            for change in changes:
                console.bullet(change, indent=4)
        else:
            console.bullet("no changes required", indent=4)

    console.header("MCP servers")
    if inv.mcp_servers:
        console.table(
            [[s.get("name", "-"), s.get("detail", "")] for s in inv.mcp_servers],
            headers=["name", "endpoint"],
        )
    else:
        console.bullet("none registered - run phase 4 to discover and add one", indent=4)

    if inv.environment:
        console.header("Environment")
        console.table(
            [[k, v] for k, v in inv.environment.items()],
            headers=["variable", "value"],
        )

    for note in inv.notes:
        console.bullet(note, indent=2)


__all__ = ["Inventory", "InventoryCollector", "render", "PRESENT", "ABSENT"]
