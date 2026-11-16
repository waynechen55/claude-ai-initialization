"""Layered, fully overridable configuration.

Nothing in this program hardcodes a package name, a URL, a path, a model id, a
region or a timeout.  Every such value lives in a TOML document and can be
overridden, in increasing order of precedence, by:

0. the ``env`` block of the Claude settings file this program itself rewrites,
   which a parent Claude Code exports into our environment -- our own previous
   answer, and therefore the weakest one there is (see :func:`demote_echo`)
1. the packaged defaults in ``initialize_claude_ai/data/*.toml``
2. a system config file  (``$INITIALIZE_CLAUDE_AI_SYSTEM_CONFIG`` or /etc/initialize-claude-ai/config.toml)
3. a user config file    (``$XDG_CONFIG_HOME/initialize-claude-ai/config.toml``)
4. an explicit ``--config FILE`` (repeatable)
5. a ``.env``-style file (``--env-file`` / ``$INITIALIZE_CLAUDE_AI_ENV_FILE``)
6. process environment variables ``INITIALIZE_CLAUDE_AI_<PATH>`` (``__`` separates keys)
7. ``--set dotted.key=value`` command line overrides

String values support lazy interpolation of ``${ENV_VAR}``, ``${ENV_VAR:-default}``
and ``${cfg:dotted.key}``, which is what lets the shipped defaults reference the
host environment instead of baking values in.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

from .errors import ConfigError

#: Prefix for environment-variable overrides, itself overridable so the tool can
#: be vendored under another name.
ENV_PREFIX = os.environ.get("INITIALIZE_CLAUDE_AI_ENV_PREFIX", "INITIALIZE_CLAUDE_AI_")
ENV_SEPARATOR = "__"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Matches only the *innermost* reference: a default may itself contain
# ``${...}``, so the substitution below runs repeatedly until it settles.
_INTERPOLATION = re.compile(r"\$\{(?P<kind>cfg:)?(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)(?::-(?P<default>[^{}]*))?\}")

#: Every ``${NAME`` that is not ``${cfg:``, including one nested inside another
#: reference's default -- which :data:`_INTERPOLATION` deliberately does not match
#: in a single pass, and which is still a variable this value listens to.
_ENV_REFERENCE = re.compile(r"\$\{(?!cfg:)([A-Za-z_][A-Za-z0-9_]*)")

#: How many nested interpolation passes to make over one string before giving up.
_INTERPOLATION_PASSES = 8

#: How deep into the document to descend while interpolating. A separate limit
#: from the one above, which counts passes over a single string: this one is the
#: guard against a structure that refers to itself.
_MAX_NESTING = 8

_UNSET = object()


def _deep_merge(base: MutableMapping[str, Any], overlay: Mapping[str, Any]) -> None:
    """Recursively merge ``overlay`` into ``base`` in place."""
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), MutableMapping):
            _deep_merge(base[key], value)
        else:
            base[key] = _copy(value)


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover
        raise ConfigError(
            "TOML support is unavailable",
            remediation="Use Python 3.11+ or `pip install tomli`.",
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a mapping without executing it."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'" and value[-1:] == value[0]:
            value = value[1:-1]
        elif value:
            # Strip an unquoted trailing comment, honouring shell quoting rules.
            try:
                parts = shlex.split(value, comments=True)
                value = parts[0] if parts else ""
            except ValueError:
                pass
        if key:
            values[key] = value
    return values


#: The spellings of "yes" accepted wherever a string stands for a switch.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def truthy(value: Any) -> bool:
    """Whether a string (an environment variable, an override) means on."""
    return str(value or "").strip().lower() in TRUTHY


def fill(template: Any, tokens: Mapping[str, Any]) -> str:
    """``template`` with each ``{name}`` in ``tokens`` replaced by its value.

    The one placeholder syntax of every command and URL template in the
    configuration. Plain replacement, not ``str.format``: a template may hold
    braces that are not placeholders (JSON, a shell ``${VAR}``), and a name
    with no token is left as written for the caller to notice.
    """
    text = str(template)
    for name, value in tokens.items():
        text = text.replace("{" + str(name) + "}", str(value))
    return text


def coerce(raw: str, like: Any = _UNSET) -> Any:
    """Coerce a string override to the type of the value it replaces."""
    text = raw.strip()
    if like is not _UNSET:
        if isinstance(like, bool):
            return truthy(text)
        if isinstance(like, int) and not isinstance(like, bool):
            try:
                return int(text)
            except ValueError:
                return text
        if isinstance(like, float):
            try:
                return float(text)
            except ValueError:
                return text
        if isinstance(like, list):
            if text.startswith("["):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(like, dict):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text
    # No prior value to imitate: infer conservatively.
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


@dataclass
class Settings:
    """A merged configuration document with dotted-key access."""

    data: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    #: Extra environment used for ``${VAR}`` interpolation (defaults to os.environ).
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    #: Root document used to resolve ``${cfg:dotted.key}``. A view created by
    #: :meth:`section` keeps pointing at the full document, so a nested value can
    #: still reference ``${cfg:paths.state_dir}``.
    root: "Settings | None" = field(default=None, repr=False, compare=False)
    #: Variables taken out of :attr:`environ` because they were only there as an
    #: echo of the settings file this run rewrites -- see :func:`demote_echo`.
    #: Kept, not discarded: they are the last source anything may fall back to.
    demoted: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    #: Which layer last set each dotted key: ``packaged:40-mcp.toml``,
    #: ``file:/etc/...``, ``envfile:...``, ``env:NAME`` or ``cli``. :attr:`sources`
    #: says which layers took part; this says what each of them actually decided,
    #: which is the only way to answer "why is this value what it is" without
    #: re-running the merge by hand.
    origins: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    #: Memoised ``run.timeout_scale``. ``None`` until first asked; read through
    #: :meth:`timeout_scale` rather than directly, and never on a section view --
    #: the scale is a property of the whole run, so a view defers to its root.
    _scale: float | None = field(default=None, repr=False, compare=False)

    # -- timeouts --------------------------------------------------------------

    def timeout_scale(self) -> float:
        """How much longer every operation is allowed to take (1.0 = as written).

        One number multiplying every per-operation timeout, which is the only
        honest form of a "global timeout": the 21 timeouts in this program span
        5 seconds (a TCP connect) to 1800 (waiting for a person at a browser),
        so a single value *substituted* everywhere would either kill agent turns
        at 30 seconds or leave dead connections hanging for half an hour.
        Multiplying keeps the relationships and answers the real question --
        "this machine, or this link, is slower than you assumed".
        """
        root = self.root or self
        if root._scale is None:
            try:
                root._scale = max(0.0, float(root.get("run.timeout_scale", 1.0) or 1.0))
            except (TypeError, ValueError):
                root._scale = 1.0
        return root._scale

    def _scaled(self, dotted: str, value: float) -> float:
        """Apply :meth:`timeout_scale` to a value, if this key is a timeout.

        Matched on the key's last segment rather than its full path, because
        most timeouts are read through a section view (``config.float_(
        "timeout_seconds")``) and have no full path to match against.

        The suffix rule is what keeps the five *non*-timeouts out of it:
        ``progress_interval_seconds`` is a heartbeat, ``latency_target_seconds``
        and ``latency_ceiling_seconds`` are thresholds for *scoring* a server
        when ranking -- stretching those would change which server wins, which
        is not what anybody means by "allow more time" -- ``stop_grace_seconds``
        is the gap between SIGTERM and SIGKILL, and ``timeout_turn_scale`` is
        itself a multiplier. None of them ends in ``timeout_seconds``, and
        neither does any table holding them.
        """
        scale = self.timeout_scale()
        if scale == 1.0:
            # The whole point of the default: no arithmetic at all, so an
            # untouched run cannot differ from one before this existed --
            # not by rounding, not by int becoming float.
            return value
        root = self.root or self
        suffixes = root.get("run.timeout_scale_suffixes", None) or ["timeout_seconds"]
        # The leaf, or the table holding it: ``command_timeout_seconds.print_token``
        # is one command's timeout in a table of them, keyed by command name.
        tail = dotted.split(".")[-2:]
        if not any(part.endswith(str(s)) for part in tail for s in suffixes):
            return value
        return value * scale

    # -- access ---------------------------------------------------------------

    def get(self, dotted: str, default: Any = None, *, interpolate: bool = True) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return default
        return self._interpolate(node) if interpolate else node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _UNSET)
        if value is _UNSET or value is None:
            raise ConfigError(
                f"required configuration key '{dotted}' is not set",
                remediation=f"Set it in a config file or via {self.env_var_for(dotted)}.",
            )
        return value

    def str_(self, dotted: str, default: str = "") -> str:
        value = self.get(dotted, default)
        return "" if value is None else str(value)

    def int_(self, dotted: str, default: int = 0) -> int:
        value = self.get(dotted, default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        scaled = self._scaled(dotted, number)
        return number if scaled == number else int(scaled)

    def float_(self, dotted: str, default: float = 0.0) -> float:
        value = self.get(dotted, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return self._scaled(dotted, number)

    def bool_(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return truthy(value)

    def list_(self, dotted: str, default: Sequence[Any] | None = None) -> list[Any]:
        value = self.get(dotted, None)
        if value is None:
            return list(default or [])
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def str_list(self, dotted: str, default: Sequence[str] | None = None) -> list[str]:
        return [str(v) for v in self.list_(dotted, default)]

    def dict_(self, dotted: str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = self.get(dotted, None)
        if isinstance(value, Mapping):
            return dict(value)
        return dict(default or {})

    def path(self, dotted: str, default: str | os.PathLike[str] | None = None) -> Path | None:
        value = self.get(dotted, None)
        if value in (None, ""):
            return Path(default).expanduser() if default is not None else None
        return Path(str(value)).expanduser()

    def section(self, dotted: str) -> "Settings":
        """Return a view rooted at ``dotted`` (shares interpolation context)."""
        node = self.get(dotted, {}, interpolate=False)
        if not isinstance(node, Mapping):
            node = {}
        return Settings(
            data=dict(node),
            sources=self.sources,
            environ=self.environ,
            root=self.root or self,
            demoted=self.demoted,
            origins=self.origins,
        )

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, MutableMapping):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def as_dict(self, *, interpolate: bool = True) -> dict[str, Any]:
        return self._interpolate(self.data) if interpolate else _copy(self.data)

    # -- interpolation ---------------------------------------------------------

    def _interpolate(self, node: Any, _depth: int = 0) -> Any:
        if _depth > _MAX_NESTING:  # cycle guard
            return node
        if isinstance(node, str):
            return self._interpolate_str(node, _depth)
        if isinstance(node, Mapping):
            return {k: self._interpolate(v, _depth + 1) for k, v in node.items()}
        if isinstance(node, list):
            return [self._interpolate(v, _depth + 1) for v in node]
        return node

    def _interpolate_str(self, text: str, depth: int) -> str:
        if "${" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            fallback = match.group("default")
            if match.group("kind"):  # ${cfg:dotted.key} -- always against the root
                source = self.root or self
                value = source.get(name, None, interpolate=False)
                if value is None:
                    return fallback if fallback is not None else ""
                return str(source._interpolate(value, depth + 1))
            value = self.environ.get(name)
            if value in (None, ""):
                return fallback if fallback is not None else ""
            return str(value)

        # ``${A:-${B}/x}`` needs two passes: the inner reference resolves first,
        # which is what makes the outer one matchable.
        for _ in range(_INTERPOLATION_PASSES):
            rendered = _INTERPOLATION.sub(replace, text)
            if rendered == text or "${" not in rendered:
                return rendered
            text = rendered
        return text

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def env_var_for(dotted: str) -> str:
        return ENV_PREFIX + dotted.replace(".", ENV_SEPARATOR).upper()

    def flatten(self, *, interpolate: bool = True) -> dict[str, Any]:
        """Flatten to ``dotted.key -> value`` (used by ``config show``).

        ``interpolate=False`` returns the values as written, ``${VAR}`` and all,
        which is how :meth:`overrides` can tell which environment variables a
        packaged default is listening to.
        """
        out: dict[str, Any] = {}

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, Mapping):
                for key, value in node.items():
                    walk(value, f"{prefix}.{key}" if prefix else str(key))
            else:
                out[prefix] = self._interpolate(node) if interpolate else node

        walk(self.data, "")
        return out

    def overrides(self, *, ignore_env: Sequence[str] = ()) -> list[tuple[str, Any, str]]:
        """``(key, value, origin)`` for every setting this host, not the package, decides.

        Two ways a setting stops being the packaged default, and the block shown
        at the start of a run is wrong without both:

        * a later layer overwrote it -- a config file, an env file, an
          environment variable, the command line. :attr:`origins` says so.
        * the packaged default is itself a reference, ``"${CLOUD_ML_REGION:-us-east5}"``,
          and the variable it names is set. The origin is still ``packaged:``
          because no layer replaced the line; the value all the same comes from
          the environment, and that is what the operator needs told.

        ``ignore_env`` are :mod:`fnmatch` patterns for variables that say nothing
        about how this program was configured -- ``HOME`` is read by a dozen path
        defaults and listing it a dozen times buries the one line that matters.
        """
        values = self.flatten()
        raw = self.flatten(interpolate=False)
        found: dict[str, str] = {}
        for key, origin in self.origins.items():
            if key not in values:
                continue
            if not origin.startswith("packaged:"):
                found[key] = origin
                continue
            names = [
                name
                for name in _env_references(raw.get(key))
                if self.environ.get(name) and not any(fnmatch.fnmatchcase(name, p) for p in ignore_env)
            ]
            if names:
                found[key] = "env:" + ",".join(dict.fromkeys(names))
        return [(key, values[key], found[key]) for key in sorted(found)]


def _env_references(value: Any) -> list[str]:
    """The environment variables a raw value interpolates, in order of appearance.

    ``${cfg:...}`` references are another setting, not the environment, and are
    left out.
    """
    if isinstance(value, (list, tuple)):
        return [name for item in value for name in _env_references(item)]
    if not isinstance(value, str) or "${" not in value:
        return []
    return [m.group(1) for m in _ENV_REFERENCE.finditer(value)]


def _flatten_keys(node: Any, prefix: str = "") -> list[str]:
    """Dotted key for every leaf in a plain document, for origin tracking."""
    if isinstance(node, Mapping):
        out: list[str] = []
        for key, value in node.items():
            out.extend(_flatten_keys(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return [prefix] if prefix else []


def _default_config_paths(environ: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    system = environ.get(f"{ENV_PREFIX}SYSTEM_CONFIG")
    paths.append(Path(system) if system else Path("/etc/initialize-claude-ai/config.toml"))
    xdg = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg) / "initialize-claude-ai" / "config.toml")
    # A project-local file makes the tool usable straight from a checkout.
    paths.append(Path.cwd() / "initialize-claude-ai.toml")
    return paths


def _apply_env_overrides(
    settings: Settings, environ: Mapping[str, str], prefix: str = ""
) -> list[tuple[str, str, bool]]:
    """Apply ``INITIALIZE_CLAUDE_AI_A__B=value`` overrides.

    Returns ``(dotted key, variable name, the key already existed)``. The last of
    those matters only to the report: ``__`` is the separator, so a variable meant
    for ``${INITIALIZE_CLAUDE_AI_MCP_AGENTIC}`` interpolation *also* lands here as
    a brand-new top-level ``mcp_agentic`` that nothing will ever read. Applying it
    is harmless and keeps this mechanism able to reach a key no packaged file
    declares; listing it as a setting of this program would be a lie.

    ``prefix`` narrows the pass to one subtree, for the settings that have to be
    read before their own turn comes round.
    """
    applied: list[tuple[str, str, bool]] = []
    reserved = {
        f"{ENV_PREFIX}SYSTEM_CONFIG",
        f"{ENV_PREFIX}CONFIG",
        f"{ENV_PREFIX}ENV_FILE",
        f"{ENV_PREFIX}ENV_PREFIX",
    }
    for name, raw in sorted(environ.items()):
        if not name.startswith(ENV_PREFIX) or name in reserved:
            continue
        suffix = name[len(ENV_PREFIX) :]
        if not suffix:
            continue
        dotted = suffix.replace(ENV_SEPARATOR, ".").lower()
        if not dotted.startswith(prefix):
            continue
        current = settings.get(dotted, _UNSET, interpolate=False)
        settings.set(dotted, coerce(raw, current))
        applied.append((dotted, name, current is not _UNSET))
    return applied


def _apply_cli_overrides(settings: Settings, overrides: Iterable[str], prefix: str = "") -> list[str]:
    """Apply ``--set dotted.key=value`` items. Returns the dotted keys applied."""
    applied: list[str] = []
    for item in overrides:
        if "=" not in item:
            raise ConfigError(f"invalid override {item!r}", remediation="Use --set dotted.key=value")
        dotted, _, raw = item.partition("=")
        dotted = dotted.strip()
        if not dotted.startswith(prefix):
            continue
        current = settings.get(dotted, _UNSET, interpolate=False)
        settings.set(dotted, coerce(raw, current))
        applied.append(dotted)
    return applied


def demote_echo(settings: Settings, env: MutableMapping[str, str]) -> dict[str, str]:
    """Take out of the environment what is only in it because we put it there.

    Claude Code exports the ``env`` block of its settings files into every
    process it launches, so a run started from inside Claude Code inherits the
    answers its own last run wrote -- the region, the project, the model pins.
    Read back as "the environment", they outrank an env file and a configured
    default, and the result is a loop: the first value ever written is the one
    that keeps being written, and correcting it from anywhere but the command
    line has no effect.

    Removing them here is what puts the existing settings file *last*, below
    the command line, the env files and the configuration -- and last is still
    a source, so they are returned rather than dropped, for a setting that has
    no other answer to fall back on (:attr:`Settings.demoted`).

    Only an exact match is taken out. A variable whose value differs from the
    file cannot be that file's echo: something else set it, and something else
    is an answer.  Only the names ``environment.echo_names`` covers, too, minus
    ``environment.echo_keep`` -- a credential that lives in the settings file is
    the only copy of it there is.
    """
    if not settings.bool_("environment.demote_echo", True):
        return {}
    demoted: dict[str, str] = {}
    key = settings.str_("environment.echo_key", "env")
    eligible = settings.str_list(
        "environment.echo_names",
        [
            "ANTHROPIC_*",
            "CLAUDE_CODE_*",
            "CLOUD_ML_REGION",
            "GOOGLE_CLOUD_*",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "INITIALIZE_CLAUDE_AI_*",
        ],
    )
    keep = settings.str_list(
        "environment.echo_keep",
        [
            "*API_KEY*",
            "*TOKEN*",
            "*SECRET*",
            "*PASSWORD*",
            "*CREDENTIALS*",
        ],
    )
    for raw in settings.str_list("environment.echo_files"):
        if not raw.strip():
            continue
        path = Path(raw).expanduser()
        try:
            document = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            continue  # unreadable or not JSON: nothing to demote, and not our error
        block = document.get(key) if isinstance(document, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        for name, value in block.items():
            name = str(name)
            if name not in env or env[name] != str(value):
                continue
            if not any(fnmatch.fnmatchcase(name, p) for p in eligible):
                continue
            if any(fnmatch.fnmatchcase(name, p) for p in keep):
                continue
            demoted[name] = env.pop(name)
    return demoted


def load_settings(
    *,
    config_files: Iterable[str | os.PathLike[str]] = (),
    env_files: Iterable[str | os.PathLike[str]] = (),
    overrides: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
    use_default_paths: bool = True,
    data_dir: Path | None = None,
) -> Settings:
    """Build the merged :class:`Settings` document."""
    env: MutableMapping[str, str] = dict(environ if environ is not None else os.environ)
    merged: dict[str, Any] = {}
    sources: list[str] = []
    # Which layer had the last word on each key. Recorded as the merge happens,
    # because afterwards there is nothing in the document that says where a value
    # came from -- and "where did this come from" is the first question anybody
    # asks of a run that behaved unexpectedly.
    origins: dict[str, str] = {}

    # 1. packaged defaults -- every *.toml in the data dir, in filename order.
    directory = data_dir or _DATA_DIR
    for path in sorted(directory.glob("*.toml")):
        loaded = _load_toml(path)
        _deep_merge(merged, loaded)
        sources.append(f"packaged:{path.name}")
        origins.update(dict.fromkeys(_flatten_keys(loaded), f"packaged:{path.name}"))

    # 2-4. file layers
    candidates: list[Path] = []
    if use_default_paths:
        candidates.extend(_default_config_paths(env))
    explicit_env = env.get(f"{ENV_PREFIX}CONFIG")
    if explicit_env:
        candidates.extend(Path(p) for p in explicit_env.split(os.pathsep) if p)
    candidates.extend(Path(p) for p in config_files)
    for path in candidates:
        path = path.expanduser()
        if path.is_file():
            loaded = _load_toml(path)
            _deep_merge(merged, loaded)
            sources.append(f"file:{path}")
            origins.update(dict.fromkeys(_flatten_keys(loaded), f"file:{path}"))

    settings = Settings(data=merged, sources=sources, environ=env, origins=origins)

    # 4b. the settings file this run rewrites, handed back to us by the parent
    #     process. Before the env files, so that a name they supply is actually
    #     used rather than shadowed by the echo of our own last answer.
    #     Whether that happens at all is itself a setting, so the two layers that
    #     outrank a file get their say on it first -- out of turn, but only for
    #     this one subtree, and the same values are applied again below.
    _apply_env_overrides(settings, env, prefix="environment.")
    _apply_cli_overrides(settings, overrides, prefix="environment.")
    settings.demoted = demote_echo(settings, env)
    for name in sorted(settings.demoted):
        sources.append(f"echo:{name}")

    # 5. .env files -- these populate the *environment*, so they can drive both
    #    ${VAR} interpolation and INITIALIZE_CLAUDE_AI_* overrides.
    env_file_candidates = list(env_files)
    from_env = env.get(f"{ENV_PREFIX}ENV_FILE")
    if from_env:
        env_file_candidates.extend(p for p in from_env.split(os.pathsep) if p)
    for raw_path in env_file_candidates:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"env file not found: {path}")
        for key, value in parse_env_file(path).items():
            env.setdefault(key, value)  # real environment wins over the file
        sources.append(f"envfile:{path}")

    # 6. environment overrides
    for dotted, name, existed in _apply_env_overrides(settings, env):
        sources.append(f"env:{name}->{dotted}")
        # A key nobody declared and nobody spelled out with the ``__`` separator
        # is the accident described in _apply_env_overrides: recorded as a source,
        # because it did happen, but not as a setting of this program.
        if existed or "." in dotted:
            origins[dotted] = f"env:{name}"

    # 7. explicit --set overrides
    for dotted in _apply_cli_overrides(settings, overrides):
        sources.append(f"cli:{dotted}")
        origins[dotted] = "cli"

    settings.environ = env
    return settings
