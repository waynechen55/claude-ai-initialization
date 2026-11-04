"""The component catalog: parsing and selection.

The catalog lives in ``initialize_claude_ai/data/10-components.toml``.  Operators can
extend or replace any part of it from their own config file:

``[[components]]``
    appended to (or, when ``key`` matches, merged over) the shipped catalog

``[install.overrides.<key>]``
    ``strategies``  replaces the component's install strategy list outright
    ``provides``, ``on_demand``, ``prefetch``, ``bin_dir_queries``
                    set on the component itself
    ``<field>``     patched into every strategy that already has that field
                    (e.g. ``url``, ``refs``, ``packages``)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..core.config import Settings
from ..core.errors import ConfigError
from ..core.logging_setup import get_logger

log = get_logger("components")


@dataclass
class ComponentSpec:
    key: str
    title: str = ""
    group: str = "other"
    required: bool = False
    platforms: list[str] = field(default_factory=list)
    verify: list[dict[str, Any]] = field(default_factory=list)
    install: list[dict[str, Any]] = field(default_factory=list)
    #: Token this component publishes for later templates, e.g. python -> {python}.
    provides_token: str = ""
    #: Commands that arrive with this component without a check of their own
    #: (``npx`` with npm, ``bunx`` with bun). See :meth:`commands`.
    provides: list[str] = field(default_factory=list)
    #: May phase 4 install this on demand? ``None`` defers to
    #: ``install.on_demand_default``.
    on_demand: bool | None = None
    #: Does ``--install-prefetch launchers`` take it? ``all`` always does.
    prefetch: bool = True
    #: ``{args, suffix}`` questions asking the tool where it puts what it
    #: installs -- ``go env GOPATH`` + ``bin``. The first non-empty answer wins.
    bin_dir_queries: list[dict[str, Any]] = field(default_factory=list)
    #: Names a package manager knows the component's commands by, beyond the
    #: binaries it verifies: ``nodejs`` for node. Asked of the system to find
    #: the package that provides it (:mod:`.ownership`).
    capabilities: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ComponentSpec":
        key = str(raw.get("key", "")).strip()
        if not key:
            raise ConfigError("a [[components]] entry is missing its 'key'")
        return cls(
            key=key,
            title=str(raw.get("title", key)),
            group=str(raw.get("group", "other")),
            required=bool(raw.get("required", False)),
            platforms=[str(p) for p in raw.get("platforms", [])],
            verify=[dict(v) for v in raw.get("verify", []) if isinstance(v, Mapping)],
            install=[dict(i) for i in raw.get("install", []) if isinstance(i, Mapping)],
            provides_token=str(raw.get("provides_token", "")) or _default_token(key),
            provides=[str(p).strip() for p in raw.get("provides", []) if str(p).strip()],
            on_demand=None if raw.get("on_demand") is None else bool(raw.get("on_demand")),
            prefetch=bool(raw.get("prefetch", True)),
            bin_dir_queries=[dict(q) for q in raw.get("bin_dir_queries", []) if isinstance(q, Mapping)],
            capabilities=[str(c).strip() for c in raw.get("capabilities", []) if str(c).strip()],
        )

    def applies_to(self, system: str, traits: Iterable[str] = ()) -> bool:
        """Is this component meant for the machine in front of us?

        ``platforms`` normally names operating systems (``Linux``, ``Darwin``),
        but a handful of components answer to something narrower than an OS --
        ``wsl`` is a Linux that happens to have a browser on the other side of
        it. Traits let the catalog say so without a second field that would
        mean almost the same thing, and without a branch in the phase code.
        """
        if not self.platforms:
            return True
        wanted = {p.lower() for p in self.platforms}
        return system.lower() in wanted or any(str(t).lower() in wanted for t in traits)

    def binary_names(self) -> list[str]:
        names: list[str] = []
        for check in self.verify:
            if str(check.get("kind", "binary")) == "binary":
                names.extend(str(n) for n in check.get("names", []))
        return names

    def commands(self) -> list[str]:
        """Every command this component puts on the machine: checked, then provided."""
        names: list[str] = []
        for name in (*self.binary_names(), *self.provides):
            if name and name not in names:
                names.append(name)
        return names

    def env_key(self) -> str:
        for check in self.verify:
            value = str(check.get("env_key", "")).strip()
            if value:
                return value
        return ""


#: Components whose resolved path becomes a template token for later components
#: (``{python}``, ``{claude}``, ...).  Keyed by component key.
_TOKEN_MAP = {
    "python": "python",
    "pip": "pip",
    "node": "node",
    "npm": "npm",
    "uv": "uv",
    "claude-cli": "claude",
    "gcloud": "gcloud",
    "git": "git",
    "curl": "curl",
}


#: ``[install.overrides.<key>]`` fields that set the component itself rather
#: than being patched into its strategies.
_COMPONENT_FIELDS = frozenset({"provides", "on_demand", "prefetch", "bin_dir_queries"})


def _default_token(key: str) -> str:
    return _TOKEN_MAP.get(key, "")


def _merge_component(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a user-supplied component over a shipped one."""
    merged = dict(base)
    for field_name, value in overlay.items():
        if field_name in ("verify", "install") and isinstance(value, list) and value:
            merged[field_name] = [dict(v) for v in value if isinstance(v, Mapping)]
        else:
            merged[field_name] = value
    return merged


def _apply_overrides(spec: ComponentSpec, overrides: Mapping[str, Any]) -> ComponentSpec:
    patch = overrides.get(spec.key)
    if not isinstance(patch, Mapping):
        return spec
    for field_name in _COMPONENT_FIELDS.intersection(patch):
        # About the component, not about how it installs: parsed the way the
        # catalog entry itself would have been, and applied whatever else the
        # override replaces.
        parsed = ComponentSpec.from_mapping({"key": spec.key, field_name: patch[field_name]})
        setattr(spec, field_name, getattr(parsed, field_name))
        log.debug("component %s: override set %s", spec.key, field_name)
    strategies = patch.get("strategies")
    if isinstance(strategies, list) and strategies:
        spec.install = [dict(s) for s in strategies if isinstance(s, Mapping)]
        log.debug("component %s: install strategies replaced by override", spec.key)
        return spec
    for field_name, value in patch.items():
        if field_name == "strategies" or field_name in _COMPONENT_FIELDS:
            continue
        touched = 0
        for strategy in spec.install:
            if field_name in strategy:
                strategy[field_name] = value
                touched += 1
        if touched:
            log.debug("component %s: override set %s on %d strategies", spec.key, field_name, touched)
        else:
            log.warning("component %s: override field %r matches no install strategy", spec.key, field_name)
    return spec


def load_components(settings: Settings) -> list[ComponentSpec]:
    """Parse the merged catalog into specs, applying overrides."""
    raw_entries = settings.get("components", [])
    if not isinstance(raw_entries, list):
        raise ConfigError("'components' must be an array of tables")

    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key", "")).strip()
        if not key:
            raise ConfigError("a [[components]] entry is missing its 'key'")
        if key in by_key:
            by_key[key] = _merge_component(by_key[key], entry)
        else:
            by_key[key] = dict(entry)
            order.append(key)

    overrides = settings.dict_("install.overrides")
    specs: list[ComponentSpec] = []
    for key in order:
        spec = ComponentSpec.from_mapping(by_key[key])
        specs.append(_apply_overrides(spec, overrides))
    return specs


def select_components(
    components: Iterable[ComponentSpec],
    *,
    groups: Iterable[str] | None = None,
    only: Iterable[str] = (),
    skip: Iterable[str] = (),
    system: str = "Linux",
    traits: Iterable[str] = (),
) -> tuple[list[ComponentSpec], list[str]]:
    """Filter the catalog. Returns ``(selected, notes)``.

    ``only`` accepts component keys *or* group names and, when given, wins over
    ``groups``.  Dependencies implied by template tokens are pulled in
    automatically so ``--install-only claude-cli`` still gets a working Node/Python.
    """
    all_components = list(components)
    by_key = {c.key: c for c in all_components}
    notes: list[str] = []

    only_set = {str(o).strip() for o in only if str(o).strip()}
    skip_set = {str(s).strip() for s in skip if str(s).strip()}
    group_set = {str(g).strip() for g in (groups or []) if str(g).strip()}

    if only_set:
        selected = [c for c in all_components if c.key in only_set or c.group in only_set]
        unknown = only_set - {c.key for c in all_components} - {c.group for c in all_components}
        if unknown:
            notes.append(f"ignored unknown --install-only value(s): {', '.join(sorted(unknown))}")
    elif group_set:
        selected = [c for c in all_components if c.group in group_set]
    else:
        selected = list(all_components)

    # Pull in token providers the selection depends on.
    needed_tokens: set[str] = set()
    for component in selected:
        for block in (*component.verify, *component.install):
            for value in block.values():
                for token in _TOKEN_MAP.values():
                    if token and "{" + token + "}" in str(value):
                        needed_tokens.add(token)
    for key, token in _TOKEN_MAP.items():
        if token in needed_tokens and key in by_key and by_key[key] not in selected:
            selected.append(by_key[key])
            notes.append(f"added '{key}' because a selected component needs {{{token}}}")

    selected = [c for c in selected if c.key not in skip_set and c.group not in skip_set]
    traits = tuple(traits)
    dropped = [c.key for c in selected if not c.applies_to(system, traits)]
    if dropped:
        where = "/".join((system, *traits))
        notes.append(f"not applicable on {where}: {', '.join(dropped)}")
    selected = [c for c in selected if c.applies_to(system, traits)]

    # Preserve catalog order so prerequisites install before their dependants.
    catalog_order = {c.key: i for i, c in enumerate(all_components)}
    selected.sort(key=lambda c: catalog_order.get(c.key, 999))
    return selected, notes
