"""Secret redaction.

Every string that can reach a log file, a console, a JSON report or a subagent
prompt is funnelled through :func:`redact`.  Two mechanisms are combined:

* **Value registration** -- once a concrete secret value has been resolved, it is
  registered here and scrubbed verbatim wherever it appears.
* **Pattern matching** -- regexes catch secret-shaped text that was never
  resolved through :mod:`initialize_claude_ai.core.secrets` (for example a token echoed
  by a third-party CLI).

The pattern list is configuration-driven; :func:`configure` is called once the
settings are loaded so that operators can add site-specific patterns without
touching code.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Iterable

MASK = "***REDACTED***"

# Minimum length before a registered value is worth scrubbing.  Short values
# (e.g. a project id of "us") would otherwise shred unrelated output.
_MIN_SCRUB_LEN = 8

_lock = threading.Lock()
_registered: set[str] = set()
_patterns: list[re.Pattern[str]] = []

#: Used until :func:`configure` installs the configured set, and so the same
#: set as ``[redaction].patterns`` (a test holds them equal): a secret logged
#: before the settings load deserves the same protection as one logged after.
#: Kept deliberately conservative -- false positives destroy debuggability.
_DEFAULT_PATTERNS: tuple[str, ...] = (
    r"sk-ant-[A-Za-z0-9_\-]{16,}",
    r"sk-[A-Za-z0-9]{32,}",
    r"ya29\.[A-Za-z0-9._\-]{20,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"gho_[A-Za-z0-9]{20,}",
    r"AIza[A-Za-z0-9_\-]{30,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
    r"""(?i)\b(api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)\b\s*[=:]\s*["']?([^\s"',;}]{6,})""",
    # A credential written into a URL -- http://user:pw@host, which is how an
    # appliance behind basic auth is routinely passed around, and which arrives
    # here through --mcp-server, a registry hit or a link mined out of a page.
    # Only the password is masked, so the line still reads as the address it is.
    r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]*:(?P<secret>[^/\s@]+)@",
)

_KEYISH = re.compile(r"(?i)(key|token|secret|password|passwd|credential|authorization|cookie|private)")


def configure(patterns: Iterable[str] | None = None) -> None:
    """Install the redaction pattern set (called after settings load)."""
    source = tuple(patterns) if patterns else _DEFAULT_PATTERNS
    compiled: list[re.Pattern[str]] = []
    for raw in source:
        try:
            compiled.append(re.compile(raw, re.DOTALL))
        except re.error:  # a bad operator-supplied pattern must not kill the run
            continue
    with _lock:
        _patterns[:] = compiled


def register(value: str | None) -> None:
    """Register a concrete secret value so it is scrubbed verbatim."""
    if not value or len(value) < _MIN_SCRUB_LEN:
        return
    with _lock:
        _registered.add(value)


def registered_count() -> int:
    with _lock:
        return len(_registered)


def redact(text: Any) -> str:
    """Return ``text`` with every known or secret-shaped value masked."""
    if text is None:
        return ""
    out = text if isinstance(text, str) else str(text)
    with _lock:
        values = sorted(_registered, key=len, reverse=True)
        patterns = list(_patterns) or [re.compile(p, re.DOTALL) for p in _DEFAULT_PATTERNS]
    for value in values:
        if value in out:
            out = out.replace(value, MASK)
    for pattern in patterns:
        out = pattern.sub(_mask_match, out)
    return out


def _mask_match(match: re.Match[str]) -> str:
    # A pattern that names the secret part keeps everything around it, so a URL
    # stays a URL: "https://user:***REDACTED***@host/path".
    if "secret" in match.re.groupindex and match.group("secret") is not None:
        whole, offset = match.group(0), match.start()
        start, end = match.span("secret")
        return f"{whole[: start - offset]}{MASK}{whole[end - offset :]}"
    # Keep the label ("api_key=") so logs stay readable, mask only the value.
    if match.re.groups >= 2:
        return f"{match.group(1)}={MASK}"
    return MASK


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact a mapping by key name *and* by value content."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if _KEYISH.search(key):
            out[key] = MASK if value not in (None, "") else value
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [redact_mapping(v) if isinstance(v, dict) else redact(v) for v in value]
        elif isinstance(value, str):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


def redact_argv(argv: Iterable[str]) -> list[str]:
    """Redact a command line, masking the value after a secret-looking flag."""
    out: list[str] = []
    mask_next = False
    for token in argv:
        if mask_next:
            out.append(MASK)
            mask_next = False
            continue
        # '--token=value' carries the value with it, so it must be masked here
        # rather than by arming mask_next for the (innocent) next argument.
        if "=" in token and _KEYISH.search(token.split("=", 1)[0]):
            out.append(f"{token.split('=', 1)[0]}={MASK}")
            continue
        if token.startswith("-") and _KEYISH.search(token):
            out.append(token)
            mask_next = True
            continue
        out.append(redact(token))
    return out
