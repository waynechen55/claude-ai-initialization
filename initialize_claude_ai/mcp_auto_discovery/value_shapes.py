"""Does a value fit the slot it is in? Checked, not just described.

build.example.net's entry passed its login token to ``@ivotoby/openapi-mcp-server``
as ``API_HEADERS``. That server reads ``API_HEADERS`` as ``Name:value`` pairs,
so a bare token is no header at all: build.example.net never received a login,
whatever the token was. The program already *described* the shape --
``[mcp.discovery.auth.name_shapes]`` says a ``_HEADERS`` value is
"``Name:value`` pairs … not a bare token" -- and printed that sentence for the
operator, but never held a value up against it.

``[mcp.discovery.auth.value_shapes]`` makes the description checkable: per
name pattern, how a value splits into items (``item_separator``), what each
item must look like (``item_pattern``), the words for it (``expected``) and,
per authentication method, the exact form to write (``examples``), and words
that never fit however well they match (``forbidden`` -- a scheme word where a
header *name* belongs), and ``path`` -- the value must name an existing file. The method
comes from evidence -- the API description's security scheme, else the
server's 401 challenge (:mod:`.bridge_auth`) -- so the remedy says
``Authorization:Basic <base64 of user:password>`` for a server that asks for
HTTP Basic, not a guess. Values are never printed: only their names and the
shape they lack.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

from .bridge_auth import method_in_words


def rules(settings: Any) -> dict[str, Mapping[str, Any]]:
    """``{name pattern: rule}`` from ``[mcp.discovery.auth.value_shapes]``."""
    table = settings.dict_("mcp.discovery.auth.value_shapes", {})
    return {str(k): v for k, v in table.items() if isinstance(v, Mapping)}


def mismatch(name: str, value: str, settings: Any) -> dict[str, Any] | None:
    """The rule ``value`` breaks for ``name``, as ``{name, expected, examples}``; None if it fits or none applies.

    A value that is still a ``${…}`` reference, or empty, is not judged -- there
    is nothing to judge yet.
    """
    text = str(value or "")
    if not text.strip() or "${" in text:
        return None
    for pattern, rule in rules(settings).items():
        try:
            if not re.search(pattern, str(name), re.IGNORECASE):
                continue
            item = re.compile(str(rule.get("item_pattern") or ""))
        except re.error:
            continue
        if not item.pattern:
            continue
        separator = str(rule.get("item_separator") or "")
        items = [part for part in (text.split(separator) if separator else [text]) if part.strip()]
        forbidden = {str(word).strip().lower() for word in rule.get("forbidden") or [] if str(word).strip()}
        exists = (lambda part: os.path.isfile(part.strip())) if rule.get("path") else (lambda part: True)
        if items and all(
            item.search(part) and part.strip().lower() not in forbidden and exists(part) for part in items
        ):
            return None
        examples = rule.get("examples") if isinstance(rule.get("examples"), Mapping) else {}
        return {
            "name": str(name),
            "expected": str(rule.get("expected") or "a different shape"),
            "examples": dict(examples),
        }
    return None


def remedy(problem: Mapping[str, Any], method: Mapping[str, str] | None, host: str = "", variable: str = "") -> str:
    """What to write instead, in words -- with the exact form when the authentication method is known."""
    name = str(problem.get("name") or "the variable")
    exported = variable or name
    text = f"{name} takes {problem.get('expected')}, and the value given does not have that shape"
    scheme = str((method or {}).get("scheme") or "")
    example = str((problem.get("examples") or {}).get(scheme) or "")
    if example:
        example = example.replace("{prefix}", str((method or {}).get("prefix") or scheme.capitalize()))
        example = example.replace("{name}", str((method or {}).get("name") or "X-Api-Key"))
        asks = (
            f"{host} asks for {method_in_words(method or {})}"
            if host
            else f"it asks for {method_in_words(method or {})}"
        )
        text += f" -- {asks}, so export {exported}='{example}'"
    return text


__all__ = ["mismatch", "remedy", "rules"]
