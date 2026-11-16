"""What counts as a credential, wherever the claim came from.

Phase 4 takes descriptions of servers from two kinds of outsider -- a Claude
subagent, and a public package registry -- and both of them describe environment
variables and HTTP headers that the entry will carry. Neither is trusted with
the question *is this a secret?*, because the project's second rule does not
admit a second opinion: no credential value appears in code, logs, reports,
command lines or generated files.

These primitives lived in :mod:`.agentic` and so applied only to what an agent
said. The deterministic pipeline reached the same decision by reading the
registry's own ``isSecret`` flag, which is somebody else's judgement about our
guarantee. A registry entry saying

.. code-block:: json

    {"name": "Authorization", "value": "Bearer sk-live-…", "isSecret": false}

would have had its value copied verbatim into ``.mcp.json`` in traditional mode,
while agent mode dropped the identical header. One rule, two enforcement
levels, and the weaker one on the path that runs when there is no agent to
supervise it.

So the rule lives here, both pipelines import it, and a third party's flag is
treated as a *hint that can only make us more careful*: ``isSecret: true`` is
believed, ``isSecret: false`` is checked.

**Import the predicate, not the pattern.** Sharing the regex turned out not to
be sharing the rule: both pipelines imported the same ``SENSITIVE_NAME`` object
and one of them then called ``.search()`` on it by hand, so a later refinement
-- the selector guard below -- reached only the deterministic path while the
test pinning both to the same object kept passing. Call :func:`is_secret_name`
or :func:`holds_a_credential`; a structural test fails the build if anything
outside this module matches the pattern itself.

**And the rule cuts both ways.** Erring towards "secret" is not free. A variable
wrongly classified has its value discarded and replaced by a placeholder nobody
can fill, which is a *broken entry* rather than a cautious one -- see
:func:`names_a_selector` for the one that stopped a server from starting.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

#: Reference schemes a secret may be named by. ``literal:`` is deliberately
#: absent: it is a value, and values are exactly what must not come back from
#: an agent or a registry.
SAFE_REFERENCE_SCHEMES = (
    "env",
    "file",
    "keyring",
    "command",
    "gcloud-adc",
    "gcloud-token",
    "prompt",
)

#: Every word list and pattern below is packaged as ``[mcp.secret_policy]`` and
#: set from it once per run by :func:`configure` (phase 4, either mode); these
#: are its fallbacks and equal it. Process-wide, like
#: :func:`initialize_claude_ai.core.redact.configure`: the callers classifying a
#: name have no ``Settings`` in reach, and threading it through would put the
#: rule in many places again -- the very thing this module exists to prevent.

#: Variable, header and argument names that must never carry an inline value
#: (``sensitive_words``).
#:
#: The words match as *words*. As a bare substring, ``auth`` matched
#: ``NODE_TLS_REJECT_UNAUTHORIZED``, so a TLS switch was classified as a
#: credential, its value discarded, and ``"${NODE_TLS_REJECT_UNAUTHORIZED}"``
#: written into ``.mcp.json`` -- a placeholder for a thing that is not secret
#: and has no value to place. The same net caught ``AUTHOR``, ``AUTHORITY``,
#: ``GIT_AUTHOR_NAME``, ``KEYSTONE_URL`` and ``MONKEY_PATCH``. Environment
#: variables are ``UPPER_SNAKE_CASE``, so the separators are the word boundaries.
SENSITIVE_WORDS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "auth",
    "authorization",
    "cookie",
    "session",
    "private",
    "bearer",
)

#: Trailing words that make a variable a *choice* rather than a value
#: (``selector_words``). Matched at the end of the name, because the last word
#: is the one that says what kind of thing the variable holds: ``AUTH_MODE`` is
#: a mode, ``AUTH_HEADER`` is not. See :func:`names_a_selector` for the entry
#: this broke.
SELECTOR_WORDS = (
    "mode",
    "type",
    "scheme",
    "method",
    "format",
    "style",
    "kind",
    "strategy",
    "algorithm",
    "version",
    "flavor",
    "flavour",
    "commit",
    "sha",
    "revision",
    "digest",
    "hash",
    "checksum",
    "uuid",
)

#: Trailing words that make a variable a *location* -- where something is --
#: rather than a value (``location_words``): ``SSH_AUTH_SOCK`` holds the path
#: of an agent's socket, ``…_KEY_FILE`` the path of a key. "auth" in the first
#: filed it as a credential: renamed to ``BUILD_EXAMPLE_NET_SSH_AUTH_SOCK``, which
#: no agent sets, and waited for.
LOCATION_WORDS = ("sock", "socket", "file", "path", "dir", "directory")

#: Names whose *shape* carries a credential, whatever words they use
#: (``carrier_patterns``): a header list (``API_HEADERS``) carries the
#: ``Authorization`` header, a ``BASIC_AUTH`` carries ``user:password``.
#: build.example.net's entry held its login token under ``API_HEADERS``, which no
#: credential word names, and it was written into the file as a setting's
#: default. Regular expressions, matched case-insensitively.
CARRIER_PATTERNS = (r"(^|_)HEADERS?$", r"(^|_)BASIC(_AUTH)?$")

#: An argument of the form ``--token=abc123`` or ``Bearer abc123``: a credential
#: on a command line, visible in ``ps`` to every user on the host
#: (``inline_credential_patterns``, matched case-insensitively).
INLINE_CREDENTIAL_PATTERNS = (
    r"(?:^|[\s=])(?:bearer|basic)\s+\S{8,}",
    r"--?[a-z0-9-]*(?:key|token|secret|password|credential)[a-z0-9-]*[=\s]+\S{8,}",
    r"[?&](?:access_token|api_key|apikey|token|key)=[^&\s]{8,}",
)

#: Values that are configuration however the variable is spelled: a flag, a
#: count, a timeout (``not_a_secret_words``, and a number of up to
#: ``not_a_secret_max_digits`` digits). Nothing shaped like this is a
#: credential, so a name that merely *reads* like one -- ``SESSION_TIMEOUT=30``
#: -- keeps its value instead of having it silently replaced by a reference to a
#: variable nobody sets. Preferring a test of the value to a longer list of
#: exempt names is the same instinct as the control probe: ask the thing itself.
NOT_A_SECRET_WORDS = ("true", "false", "yes", "no", "on", "off", "none", "null")
NOT_A_SECRET_MAX_DIGITS = 10

#: Fingerprints of the credentials existing entries on this machine already use
#: (:func:`remember_in_use`). A value equal to one is a credential under any
#: name: an agent that copied build.example.net's token from the old entry into a
#: variable called something harmless was still copying the token. Only
#: fingerprints are kept here; the values are never stored by this module.
_IN_USE: set[str] = set()

#: Defaults for :func:`looks_opaque`; the packaged values are
#: ``mcp.discovery.reuse.opaque_value_min_chars`` / ``opaque_value_min_entropy``.
OPAQUE_MIN_CHARS = 24
OPAQUE_MIN_ENTROPY = 4.0
#: ...or this share of the most its own alphabet allows: a hex token can never
#: reach 4 bits per character (16 symbols is 4 bits at most), so the absolute
#: threshold alone let one through. ``opaque_value_min_entropy_ratio``.
OPAQUE_MIN_RATIO = 0.85

#: What an opaque *configuration* value starts with: a path, a reference, a
#: document (``not_opaque_starts``). None of those is a credential however
#: random it looks.
NOT_OPAQUE_STARTS = ("/", "./", "../", "~", "$", "{", "[", '"', "'", "<", "-", "@")

#: How long a run of non-letters in a *description* has to be before it is
#: taken for a pasted credential (``pasted_value_min_chars``).
PASTED_VALUE_MIN_CHARS = 24
#: Fallback for ``[mcp.secret_policy] path_is_never_a_secret``: a value that is
#: one absolute path is where something lies (a key file, a bundle), never the
#: secret itself -- whatever its variable is called. build-example-net's
#: ``${…_OBS_SSH_KEY:-/root/.ssh/id_ed25519}`` was withheld from its trial and
#: then moved out of the file as a "literal credential", in two places with two
#: rules; this is the one rule every guard asks.
PATH_IS_NEVER_A_SECRET = True
PATH_VALUE = re.compile(r"(~|/)[^\s:;,]*")


def _words(words, *, at_end: bool) -> re.Pattern[str]:
    """Any of ``words`` as a whole ``UPPER_SNAKE`` word -- anywhere, or as the last one."""
    alternatives = "|".join(re.escape(str(w).strip()) for w in words if str(w).strip()) or "(?!)"
    tail = r"\s*$" if at_end else r"(?:[^a-z0-9]|$)"
    return re.compile(rf"(?i)(?:^|[^a-z0-9])({alternatives}){tail}")


def _any_of(patterns) -> re.Pattern[str]:
    """Any of ``patterns``, each kept whole -- one with its own ``|`` cannot swallow the next."""
    return re.compile("|".join(f"(?:{p})" for p in patterns if str(p)) or "(?!)", re.IGNORECASE)


def _plain_values(words, max_digits: int) -> re.Pattern[str]:
    alternatives = [re.escape(str(w).strip()) for w in words if str(w).strip()]
    if int(max_digits) > 0:
        alternatives.append(rf"\d{{1,{int(max_digits)}}}")
    return re.compile(rf"(?i)^\s*({'|'.join(alternatives) or '(?!)'})\s*$")


SENSITIVE_NAME = _words(SENSITIVE_WORDS, at_end=False)
SELECTOR_NAME = _words(SELECTOR_WORDS, at_end=True)
LOCATION_NAME = _words(LOCATION_WORDS, at_end=True)
INLINE_CREDENTIAL = _any_of(INLINE_CREDENTIAL_PATTERNS)
NOT_A_SECRET_VALUE = _plain_values(NOT_A_SECRET_WORDS, NOT_A_SECRET_MAX_DIGITS)
_NOT_OPAQUE_START = NOT_OPAQUE_STARTS
_PASTED_MIN = PASTED_VALUE_MIN_CHARS


def configure(settings) -> None:
    """Set the rule from ``[mcp.secret_policy]`` -- the same rule for both pipelines.

    A setting left out keeps its fallback, which equals the packaged value.
    """
    global \
        SENSITIVE_NAME, \
        SELECTOR_NAME, \
        LOCATION_NAME, \
        INLINE_CREDENTIAL, \
        NOT_A_SECRET_VALUE, \
        _NOT_OPAQUE_START, \
        _PASTED_MIN
    global _CARRIERS, _PATHS_SETTINGS
    policy = settings.section("mcp.secret_policy")
    _PATHS_SETTINGS = policy.bool_("path_is_never_a_secret", PATH_IS_NEVER_A_SECRET)
    _CARRIERS = tuple(policy.str_list("carrier_patterns", list(CARRIER_PATTERNS)))
    SENSITIVE_NAME = _words(policy.str_list("sensitive_words", list(SENSITIVE_WORDS)), at_end=False)
    SELECTOR_NAME = _words(policy.str_list("selector_words", list(SELECTOR_WORDS)), at_end=True)
    LOCATION_NAME = _words(policy.str_list("location_words", list(LOCATION_WORDS)), at_end=True)
    INLINE_CREDENTIAL = _any_of(policy.str_list("inline_credential_patterns", list(INLINE_CREDENTIAL_PATTERNS)))
    NOT_A_SECRET_VALUE = _plain_values(
        policy.str_list("not_a_secret_words", list(NOT_A_SECRET_WORDS)),
        policy.int_("not_a_secret_max_digits", NOT_A_SECRET_MAX_DIGITS),
    )
    _NOT_OPAQUE_START = tuple(policy.str_list("not_opaque_starts", list(NOT_OPAQUE_STARTS)))
    _PASTED_MIN = policy.int_("pasted_value_min_chars", PASTED_VALUE_MIN_CHARS)


_CARRIERS = CARRIER_PATTERNS
_PATHS_SETTINGS = PATH_IS_NEVER_A_SECRET


def is_a_path(value: str) -> bool:
    """Is ``value`` one absolute path and nothing else -- a location, never a secret (``path_is_never_a_secret``)?"""
    text = str(value or "").strip()
    return bool(
        _PATHS_SETTINGS and text not in ("/", "~") and PATH_VALUE.fullmatch(text) and not INLINE_CREDENTIAL.search(text)
    )


def carries_credentials(name: str) -> bool:
    """Is this a variable whose shape carries a credential -- a header list, a basic-auth pair?"""
    text = str(name or "")
    for pattern in _CARRIERS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def credential_fingerprint(values: Any) -> str:
    """One short fingerprint for the credential values a login check used; "" for none.

    Lets two checks be compared -- the same credential accepted by one
    candidate and refused by another -- without keeping or showing the value.
    """
    revealed = sorted(
        _fingerprint(str(v.reveal() if hasattr(v, "reveal") else v))
        for v in (values or [])
        if str(v.reveal() if hasattr(v, "reveal") else v or "").strip()
    )
    return _fingerprint("|".join(revealed))[:16] if revealed else ""


def secret_part_prints(values: Any) -> list[str]:
    """Short fingerprints of the *secret* parts of a login -- ``{key: value}`` -- never the values.

    A user name is not a secret and is shared by right; a password or token
    is the part that says "this is the same login". build.example.net was given
    build.opensuse.org's user name and password, refused them, and nothing
    noticed that another service had accepted the very same password. Keys
    that name no secret are left out, unless no key does.
    """
    items = [(str(k), v) for k, v in (values.items() if hasattr(values, "items") else [])]

    def text(v: Any) -> str:
        return str(v.reveal() if hasattr(v, "reveal") else v or "").strip()

    secret = [text(v) for k, v in items if is_secret_name(k) and text(v)]
    chosen = secret or [text(v) for _k, v in items if text(v)]
    return sorted({_fingerprint(v)[:16] for v in chosen})


def credential_parts(value: str) -> list[str]:
    """``value`` and what it carries: the ``user:password`` and password inside a Basic credential.

    So the same login is recognised whether it is written as two variables or
    as ``Authorization: Basic <base64>``. Only ever fingerprinted, never kept.
    """
    import base64
    import binascii

    text = str(value or "").strip()
    parts = [text] if text else []
    for token in re.findall(r"[A-Za-z0-9+/]{12,}={0,2}", text):
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        user, sep, password = decoded.partition(":")
        if sep and user and password and decoded.isprintable():
            parts += [decoded, password]
    return parts


#: Names of a credential that one server issues -- a token, an API key -- rather
#: than a user name and password, which may work on several servers. Equal to
#: ``mcp.discovery.auth.instance_bound_patterns``.
DEFAULT_INSTANCE_BOUND = (r"(^|_)(TOKEN|PAT|API_?KEY|KEY|SECRET|BEARER)($|_)",)


def instance_bound(name: str, settings: Any) -> bool:
    """Is a credential under ``name`` one server's (a token, an API key)? A password never is."""
    if re.search(r"PASS(WORD|WD)?($|_)", str(name), re.IGNORECASE):
        return False
    patterns = settings.str_list("mcp.discovery.auth.instance_bound_patterns", list(DEFAULT_INSTANCE_BOUND))
    return any(re.search(p, str(name), re.IGNORECASE) for p in patterns)


def remember_in_use(value: str) -> None:
    """Record that ``value`` is a credential this machine already uses (by fingerprint only)."""
    if str(value or "").strip():
        _IN_USE.add(_fingerprint(str(value)))


def is_in_use(value: str) -> bool:
    """Is ``value`` a credential an existing entry on this machine already uses?"""
    return bool(_IN_USE) and bool(str(value or "").strip()) and _fingerprint(str(value)) in _IN_USE


def carries_inline_credential(text: str) -> bool:
    """Does this argument, URL or header value carry a credential inline (``Bearer …``, ``--token=…``)?"""
    return bool(INLINE_CREDENTIAL.search(str(text or "")))


def is_secret_name(name: str) -> bool:
    """Does this variable or header name promise to hold a credential?"""
    text = str(name or "")
    if SELECTOR_NAME.search(text) or LOCATION_NAME.search(text):
        return False
    return bool(SENSITIVE_NAME.search(text)) or carries_credentials(text)


def names_a_selector(name: str) -> bool:
    """Is this variable a choice between named alternatives rather than a value?

    ``BUGZILLA_AUTH_MODE`` is the question this answers. It is not a key, a
    token or a secret -- it selects *how* to authenticate (``apikey``,
    ``basic``, ``anonymous``) -- but ``auth`` is a whole word in it, so the
    word-boundary rule that rescued ``NODE_TLS_REJECT_UNAUTHORIZED`` does not
    help. It was filed as a credential, its value discarded, the operator told
    to "export BUGZILLA_AUTH_MODE=<your real token>", and the entry shipped
    with a placeholder for a thing that is not secret and has no token to
    place. The server exited during start-up.

    ``NOT_A_SECRET_VALUE`` cannot catch it either: that asks the *value*, and
    ``apikey`` is neither a flag nor a number -- and here there was no value at
    all, only a name. So this asks the name, and it asks about the last word,
    because that is the word that says what kind of thing the variable holds.
    ``AUTH_MODE`` is a mode; ``AUTH_HEADER`` and ``API_KEY`` are not.
    """
    return bool(SELECTOR_NAME.search(str(name or "")))


def holds_a_credential(name: str, value: str = "") -> bool:
    """Should this name/value pair be treated as a secret?

    True when the *name* says credential and the value is not plainly a flag or
    a number, or when the value itself carries an inline credential whatever it
    is called. Errs towards yes: a false positive costs a variable the operator
    exports by hand, a false negative costs the guarantee.
    """
    text = str(value or "")
    if carries_inline_credential(text) or is_in_use(text):
        return True
    if is_a_path(text):
        return False
    return is_secret_name(name) and bool(text) and not NOT_A_SECRET_VALUE.match(text)


#: A pinned package or tag -- ``mcp-gitlab@2.1.67``, ``owner/repo@v0.4.1`` --
#: which is a launch line's business, not a credential.
_PINNED = re.compile(r"@v?\d+(?:\.\d+)+", re.IGNORECASE)


def looks_opaque(
    value: str,
    min_chars: int = OPAQUE_MIN_CHARS,
    min_entropy: float = OPAQUE_MIN_ENTROPY,
    min_ratio: float = OPAQUE_MIN_RATIO,
) -> bool:
    """Is this value a long random token -- a credential whatever it is called?

    :func:`holds_a_credential` asks the *name* and looks for an inline
    ``Bearer …``; a bare token under a name that promises nothing passes both.
    One did: a 44-character base64 string under ``SMELT_EXAMPLE_NET_HEADERS`` was
    copied from an existing CLI registration into ``.mcp.json``, because
    ``HEADERS`` is not a secret word and the value had no scheme in front of it.

    So this asks the value: a single token, at least ``min_chars`` long, with
    letters and digits, at least ``min_entropy`` bits of Shannon entropy per
    character, and nothing that makes it configuration -- a URL, a path, a
    ``${reference}``, a JSON document, a hostname, a file name, a pinned
    package. Random text
    passes; words and paths do not.
    """
    text = str(value or "").strip()
    if len(text) < max(1, int(min_chars)) or any(c.isspace() for c in text):
        return False
    if "://" in text or "${" in text or text.startswith(_NOT_OPAQUE_START):
        return False
    if NOT_A_SECRET_VALUE.match(text) or _PINNED.search(text):
        return False
    if not (any(c.isalpha() for c in text) and any(c.isdigit() for c in text)):
        return False
    # A dotted name ending in letters is a host or a file (`a.b.example.com`,
    # `dist/index.js`); a token does not end in `.<word>`.
    if re.search(r"\.[a-z]{2,}$", text, re.IGNORECASE):
        return False
    counts = Counter(text)
    entropy = -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())
    if entropy >= float(min_entropy):
        return True
    # Judged against what a *random* string of this length in its own alphabet
    # is expected to measure: 32 random hex digits carry at most 4 bits each,
    # so the absolute threshold alone called a hex token "not random". A short
    # sample never reaches the alphabet's maximum, so the expectation carries
    # the small-sample correction (Miller-Madow): log2(m) - (m-1)/(2 n ln 2).
    # A word, a path or a repeated pattern stays well below.
    symbols = min(_alphabet_size(text), len(text))
    expected = math.log2(symbols) - (symbols - 1) / (2 * len(text) * math.log(2)) if symbols > 1 else 0.0
    return min_ratio > 0 and expected > 0 and entropy >= float(min_ratio) * expected


def _alphabet_size(text: str) -> int:
    """The smallest common alphabet ``text`` is written in: hex, lower alphanumeric, alphanumeric, base64, printable."""
    for size, alphabet in (
        (16, set("0123456789abcdef")),
        (16, set("0123456789ABCDEF")),
        (36, set("0123456789abcdefghijklmnopqrstuvwxyz")),
        (62, set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")),
        (66, set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/=-_")),
    ):
        if set(text) <= alphabet:
            return size
    return 94


def looks_like_a_value(text: str) -> bool:
    """Is this *description* actually a credential someone pasted in?

    A shape is a sentence about a value -- ``username:key:secret`` -- and a
    value is a long opaque run of characters. The distinction is not perfectly
    decidable, so this errs towards discarding: losing a description costs a
    line of guidance, and keeping a secret costs the guarantee this whole
    program is built on.
    """
    stripped = str(text).strip()
    if carries_inline_credential(stripped):
        return True
    return any(len(token) >= _PASTED_MIN and not token.isalpha() for token in re.split(r"[\s,;]+", stripped))


__all__ = [
    "INLINE_CREDENTIAL",
    "INLINE_CREDENTIAL_PATTERNS",
    "NOT_A_SECRET_WORDS",
    "NOT_OPAQUE_STARTS",
    "SELECTOR_WORDS",
    "SENSITIVE_WORDS",
    "CARRIER_PATTERNS",
    "OPAQUE_MIN_RATIO",
    "carries_credentials",
    "carries_inline_credential",
    "is_in_use",
    "remember_in_use",
    "configure",
    "NOT_A_SECRET_VALUE",
    "SAFE_REFERENCE_SCHEMES",
    "SELECTOR_NAME",
    "SENSITIVE_NAME",
    "holds_a_credential",
    "looks_opaque",
    "is_secret_name",
    "looks_like_a_value",
    "names_a_selector",
]
