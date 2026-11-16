"""Can an operator believe this entry talks to the server they asked about?

The question anyone asks of a line in ``.mcp.json`` is *is this pointed at my
server?* Three real entries could not answer it, and one that looked like it
could not answer it in fact did. All four shaped this rule:

.. code-block:: json

    "confluence-example-com": {"command": "npx", "args": ["-y", "@kubb/mcp"]}

``@kubb/mcp`` is a TypeScript code generator. It handshakes -- it is a real MCP
server -- and there is no address of any kind in that entry, so nothing connects
it to Confluence and nothing in it could ever reach Confluence. **Refused.**

.. code-block:: json

    "confluence-example-com": {"type": "http", "url": "https://confluence.example.com"}

The right host and still wrong: a site's front door, which answers ``401`` to an
anonymous POST because it is a login page. It never completed a handshake, and
an origin with no path is not an endpoint. **Refused.**

.. code-block:: json

    "github-com": {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}

A *different* hostname from the target -- and GitHub's own MCP server, which
answered ``401`` as a real endpoint refusing an anonymous client. A vendor
serving MCP from a sibling domain is the ordinary shape of a hosted offering,
not a mistake: ``github.com`` is served from ``api.githubcopilot.com``,
``developers.google.com`` from ``developerknowledge.googleapis.com``.
**Believed**, and named in the briefing so the hop is visible.

.. code-block:: json

    "smelt-example-net": {"command": "npx", "args": ["-y", "mcp-graphql@2.0.4"],
                      "env": {"ENDPOINT": "https://smelt.example.net/graphql/"}}

A generic server, correctly configured. The package name says nothing about
SMELT and does not need to: the address is in the environment, where the server
reads it. **Believed.**

So the rule has a floor and a test:

**The floor.** A believed entry always contains an address -- an FQDN, a URL or
a URI. This falls out of the two ways to pass rather than being checked
separately: naming the target means its hostname is in the entry, and being
served elsewhere means a URL is. An entry with no address anywhere cannot be
checked by anybody and is refused whatever else is true of it.

**The test.** That address is either the target's own host, or a different host
*that answered as an MCP endpoint when we probed it*.

The asymmetry between a URL and a launcher is deliberate. A URL that answered
**is** the service -- we contacted it, at that address, and it replied in a way
only an MCP endpoint replies. A stdio process that handshakes proves only that
a process started; it says nothing about where that process points. So
``https://api.githubcopilot.com/mcp/`` is believed for ``github.com``, while
``JIRA_URL=https://somewhere-else.test`` in a launcher is not.

"Answered as an MCP endpoint" includes a credential wall. ``api.githubcopilot.com``
never completed a handshake -- it returned ``401`` with protocol-shaped evidence,
which is the same standard :mod:`.probes` already uses to tell a real endpoint
from Confluence's login page. Requiring a completed handshake would throw away
every server whose token the operator has not exported yet.

**And on a foreign host, answering is necessary but not sufficient**, which a
fifth entry taught:

.. code-block:: json

    "confluence-example-com": {"type": "http", "url": "https://mcp.atlassian.com/v2/mcp"}

Atlassian's own MCP service. It answered ``401`` with protocol-shaped evidence,
exactly as GitHub's did -- and it serves Atlassian **Cloud** tenants, while
``confluence.example.com`` is somebody's self-hosted Data Center wiki. A vendor's
multi-tenant cloud is not a customer's private installation of that vendor's
product, however much the two share a name. **Refused.**

Two things had to change for that, and neither is about the hostname:

*Evidence may not be its own source.* ``served_by`` is built from the tier-1
checklist and from the candidates themselves, so a URL candidate was normally in
its own -- "X is served from X, which this run found answering". A claim
restated is not a claim confirmed, so a URL's own address is withdrawn from the
set before it is judged. A launcher relaying to a host found separately keeps
it: that is a different finding, and the whole purpose of ``served_by``.

*Provenance has to say whose.* :func:`vendor_said_so` exists to answer *who said
so*, and it was matching the word "documentation" without asking whose -- so
Atlassian's documentation, describing Atlassian's address, vouched for a Example
target. It now requires the target's own organisation to appear, in the clause
making the claim or in the address itself, which is where a hosted offering
usually puts it. ``api.githubcopilot.com`` for ``github.com`` passes;
``mcp.atlassian.com`` for ``confluence.example.com`` does not.

That last test reads hostnames, and this module is otherwise built on *not*
inferring belonging from them. The distinction holds: belonging is still decided
by the probe and by the provenance marker. A hostname decides only whose claim
it is, which is the one question a hostname can answer.

**And there is a third way to pass, added last and stronger than either.** Every
test above reads the *entry* -- a command line, an environment value, a URL --
which is what somebody configured. But by the time this runs the server has
handshaked and described itself, and :attr:`Handshake.advertised` now carries
what it said: its ``instructions``, its tool and prompt descriptions, its
resource URIs, the defaults in its input schemas.

``build.opensuse.org`` is what the omission cost. Tier 2 returned
``@ateam-ai/mcp``, an npm keyword-search hit naming no address; it was refused,
correctly, and the address finished the run with nothing. The real Open Build
Service server would have been refused by the same rule for the same reason --
no address in the entry -- although it lists resource URIs under
``https://build.opensuse.org/…``. The entry cannot tell those two apart. The
handshake can, so :func:`server_said_so` asks it.

It sits below the entry tests in the order only because they are cheaper and
readable without running anything; as evidence it outranks them, being the
running server's account of what it does rather than a claim about what it was
configured to do. Two guards keep it honest: it matches by host through
:func:`same_service`, so a lookalike in a description is not the target, and it
reads only a handshake that completed.

**The same three facts also refuse.** One remote was registered for six Example
hosts and github.com:

.. code-block:: json

    "gitlab-example-net": {"type": "http", "url": "https://api.mcp.ai/p_examplep_administradores"}

Nothing about it is the target's. *Where* it is -- ``api.mcp.ai`` -- belongs
to neither Example nor GitLab. *What it is configured for* names no Example host.
*What it says it is*, once it answered, is "MCP.AI platform", a ``marketplace``
tool and ``examplep_administradores_consultar``: a catalog of other people's
tools, fronting a Brazilian regulator's database. Only a *claim* tied it to the
targets -- a registry search for "example" -- and that claim was read as a
substring. So the rule is stated once, for every server (:class:`IdentityRule`):
a claim (provenance, or this run's own finding) may tie a server on a
third-party host to the target only if the running server's own answer names
the target, and a server describing itself as a front for other servers never
does. Where nothing has answered yet the claim stands, for now, and the
shared-claim guard in the writer watches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .address import parse as parse_address
from .types import Candidate

GROUNDED = "grounded"
SERVED_ELSEWHERE = "served-elsewhere"
UNGROUNDED = "ungrounded"
#: Asked before the candidate was probed, about something only probing can
#: settle. Registrable -- an early check may not refuse on an answer it has not
#: heard yet -- and re-asked for real once the handshake exists.
UNDECIDED = "undecided"

#: *What* a verdict rests on, for a caller that has to weigh one verdict against
#: another -- see ``mcp.registration.shared_claim_bases``. A verdict resting on
#: the entry naming the target, or on the server naming it, is about this
#: target and no other; one resting only on provenance or on nothing yet is a
#: claim, and the same claim made for several unrelated targets is proof it
#: belongs to none of them.
BASIS_HOST = "host"
BASIS_NAMED = "named"
BASIS_SERVER = "server"
BASIS_FOUND = "found-serving"
BASIS_ORGANISATION = "organisation"
BASIS_VENDOR = "vendor"
BASIS_UNPROBED = "unprobed"

#: Paths that are not a path: an MCP endpoint lives somewhere, and a bare origin
#: is a website. Kept as data because ``""`` and ``"/"`` are two spellings of
#: the same thing.
FRONT_DOOR_PATHS = ("", "/")

#: Final labels that make a dotted token a filename rather than a hostname.
#: Kept short and boring on purpose -- this only decides the wording of a
#: refusal, never whether one happens.
FILE_EXTENSIONS = frozenset(
    {
        "js",
        "mjs",
        "cjs",
        "ts",
        "py",
        "rb",
        "sh",
        "bash",
        "pl",
        "php",
        "go",
        "rs",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "txt",
        "md",
        "log",
        "exe",
        "bat",
        "jar",
        "war",
        "zip",
        "tar",
        "gz",
        "lock",
        "sock",
        "pem",
        "crt",
    }
)

#: Provenance that ties an address to the target without the target being named
#: in the entry: the vendor said so. The target's own documentation, its
#: ``.well-known`` documents, a link on its landing page, or an official MCP
#: registry entry returned for *this target's* query -- all of them are somebody
#: with authority saying "our MCP server lives here".
#:
#: A generic keyword search is deliberately absent. "npm search hit" means a
#: word matched, which is how ``@kubb/mcp`` became Confluence's server and how a
#: multi-tenant host like ``bugzilla.fastmcp.app`` -- which can serve *any*
#: Bugzilla and must be told which -- looked like an answer for
#: ``bugzilla.opensuse.org``.
VENDOR_PROVENANCE = (
    "documentation",
    "landing page",
    "well-known",
    "llms.txt",
    "mcp registry",
    "official mcp registry",
    "registry remote",
)

#: How much of the server's own wording to quote back in a grounding reason.
#: Enough to recognise the resource URI or the sentence that named the target,
#: short enough that a refusal or an acceptance still reads as one line. The
#: text itself was already bounded and redacted when the handshake collected it
#: (``mcp.verification.advertised``); this is only about the report.
QUOTED_EVIDENCE_CHARS = 200

#: Anything with a scheme: ``https://…``, ``ws://…``, ``file://…``.
_URI = re.compile(r"[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)

#: A bare hostname: two or more labels ending in an alphabetic top level. The
#: final label has to be letters, which is what keeps a version out --
#: ``mcp-graphql@2.0.4`` and ``@atlassian-dc-mcp/jira@0.34.0`` are not addresses,
#: and an entry whose only dotted token is its own version number names nothing.
_FQDN = re.compile(
    r"(?<![\w.\-])(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,}(?![\w\-])",
    re.IGNORECASE,
)


@dataclass
class Grounding:
    """Whether an entry says what it talks to, and how we know."""

    kind: str = GROUNDED
    reason: str = ""
    #: Where the address was found, for the report: "the url", "the command",
    #: "env ENDPOINT".
    evidence: str = ""
    #: The host actually serving this target, when it is not the target itself.
    served_by: str = ""
    #: What the verdict rests on: one of the ``BASIS_*`` values, "" for a refusal.
    basis: str = ""

    @property
    def registrable(self) -> bool:
        return self.kind != UNGROUNDED

    def to_dict(self) -> dict[str, object]:
        return {
            "grounding": self.kind,
            "reason": self.reason,
            "evidence": self.evidence,
            "served_by": self.served_by,
            "grounding_basis": self.basis,
        }


def host_of(text: str) -> str:
    """The bare hostname in a URL or address, lowercased and undotted."""
    return (parse_address(text).host or "").strip(".").lower()


#: The original spelling, kept because most of this module uses it.
_host_of = host_of


def hosts_named(texts: Iterable[Any]) -> list[str]:
    """The hosts of every URL in ``texts`` -- a launch line, an ``env`` block -- in order.

    What a local entry talks to is written in its configuration, not in its
    command: ``uvx mcp-atlassian`` is jira.example.com's server or sd.example.com's
    depending on ``JIRA_URL``. Comparing two entries by command alone called
    those two one service and advised deleting the working one.
    """
    found: list[str] = []
    for text in texts:
        for match in _URI.findall(str(text or "")):
            host = host_of(match.rstrip("'\",;)"))
            if host and host not in found:
                found.append(host)
    return found


def same_service(host: str, other: str) -> bool:
    """Is ``other`` the target host, or a subdomain of it?

    Compares labels, never substrings: ``mcp.example.com`` counts for
    ``example.com``, and ``example.com.evil.net`` does not.
    """
    host, other = host.strip(".").lower(), other.strip(".").lower()
    if not host or not other:
        return False
    if host == other:
        return True
    return other.endswith("." + host) or host.endswith("." + other)


#: Public suffixes longer than one label, so the registrable domain is one label
#: more. The packaged list is ``mcp.grounding.multi_part_suffixes``; this is its
#: fallback, equal to it. It decides whether an address on a sibling host is
#: *believed with a flag* or *refused*, never whether a correct entry is silently
#: wrong. The full public suffix list is read as well when the system has one
#: (``mcp.grounding.public_suffix_file``) -- see :func:`configure_suffixes`.
MULTI_PART_SUFFIXES = (
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "me.uk",
    "ltd.uk",
    "plc.uk",
    "net.uk",
    "sch.uk",
    "nhs.uk",
    "com.au",
    "net.au",
    "org.au",
    "edu.au",
    "gov.au",
    "asn.au",
    "id.au",
    "co.jp",
    "ne.jp",
    "or.jp",
    "ac.jp",
    "go.jp",
    "ad.jp",
    "ed.jp",
    "gr.jp",
    "lg.jp",
    "co.nz",
    "net.nz",
    "org.nz",
    "ac.nz",
    "govt.nz",
    "geek.nz",
    "com.br",
    "net.br",
    "org.br",
    "gov.br",
    "edu.br",
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "com.tw",
    "net.tw",
    "org.tw",
    "edu.tw",
    "gov.tw",
    "com.hk",
    "net.hk",
    "org.hk",
    "edu.hk",
    "gov.hk",
    "com.sg",
    "net.sg",
    "org.sg",
    "edu.sg",
    "gov.sg",
    "com.my",
    "net.my",
    "org.my",
    "edu.my",
    "gov.my",
    "co.kr",
    "or.kr",
    "ne.kr",
    "ac.kr",
    "go.kr",
    "co.in",
    "net.in",
    "org.in",
    "ac.in",
    "gov.in",
    "co.id",
    "or.id",
    "ac.id",
    "go.id",
    "co.th",
    "or.th",
    "ac.th",
    "go.th",
    "com.vn",
    "edu.vn",
    "gov.vn",
    "com.ph",
    "net.ph",
    "org.ph",
    "com.pk",
    "net.pk",
    "org.pk",
    "co.il",
    "org.il",
    "ac.il",
    "gov.il",
    "com.tr",
    "net.tr",
    "org.tr",
    "edu.tr",
    "gov.tr",
    "com.sa",
    "net.sa",
    "org.sa",
    "com.eg",
    "com.ua",
    "net.ua",
    "org.ua",
    "com.pl",
    "net.pl",
    "org.pl",
    "co.za",
    "org.za",
    "ac.za",
    "gov.za",
    "co.ke",
    "or.ke",
    "ac.ke",
    "com.ng",
    "org.ng",
    "com.mx",
    "org.mx",
    "gob.mx",
    "edu.mx",
    "com.ar",
    "net.ar",
    "org.ar",
    "gob.ar",
    "com.co",
    "net.co",
    "org.co",
    "com.pe",
    "org.pe",
    "co.ve",
    "com.ve",
    "cl.cl",
    "co.at",
    "or.at",
    "ac.at",
    "gv.at",
    "com.es",
    "org.es",
    "nom.es",
    "com.pt",
    "org.pt",
    "com.gr",
    "org.gr",
    "com.cy",
    "co.hu",
    "org.hu",
    "com.ru",
    "net.ru",
    "org.ru",
)

#: The suffixes in force, set once per run by :func:`configure_suffixes` --
#: process-wide for the same reason as :func:`.address.configure_local_zones`:
#: the callers that ask "which organisation is this host" have no ``Settings``
#: in reach. ``(rules, wildcards, exceptions)``, each lower-case, without dots
#: at either end.
_SUFFIXES: tuple[frozenset[str], frozenset[str], frozenset[str]] = (
    frozenset(MULTI_PART_SUFFIXES),
    frozenset(),
    frozenset(),
)


def read_public_suffix_list(text: str, sections: Sequence[str] = ("icann",)) -> tuple[set[str], set[str], set[str]]:
    """``(rules, wildcards, exceptions)`` from the text of a public suffix list.

    The file's sections are marked ``===BEGIN ICANN DOMAINS===`` and
    ``===BEGIN PRIVATE DOMAINS===``; only the ``sections`` named are kept.
    ``*.ck`` is a wildcard (every label under ``ck`` is a suffix), ``!www.ck``
    an exception to one. Unmarked text is taken as ICANN.
    """
    wanted = {str(s).strip().lower() for s in sections}
    rules: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    section = "icann"
    for raw in str(text or "").splitlines():
        line = raw.strip()
        marker = re.search(r"===BEGIN (\w+) DOMAINS===", line)
        if marker:
            section = marker.group(1).lower()
            continue
        if not line or line.startswith("//") or section not in wanted:
            continue
        rule = line.split()[0].lower().strip(".")
        if rule.startswith("!"):
            exceptions.add(rule[1:])
        elif rule.startswith("*."):
            wildcards.add(rule[2:])
        elif "." in rule:
            rules.add(rule)
    return rules, wildcards, exceptions


def configure_suffixes(
    suffixes: Sequence[str] = MULTI_PART_SUFFIXES,
    *,
    public_suffix_file: str = "",
    sections: Sequence[str] = ("icann",),
) -> int:
    """Set the suffixes :func:`registrable_domain` knows; return how many.

    ``suffixes`` (``mcp.grounding.multi_part_suffixes``) always count; the
    public suffix list in ``public_suffix_file`` is added when the file can be
    read, so a host without one still has the packaged list rather than none.
    """
    global _SUFFIXES
    rules = {str(s).strip().strip(".").lower() for s in suffixes if str(s).strip().strip(".")}
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    if public_suffix_file:
        try:
            with open(public_suffix_file, encoding="utf-8") as handle:
                listed = read_public_suffix_list(handle.read(), sections)
        except OSError:
            listed = (set(), set(), set())
        rules |= listed[0]
        wildcards |= listed[1]
        exceptions |= listed[2]
    _SUFFIXES = (frozenset(rules), frozenset(wildcards), frozenset(exceptions))
    return len(rules) + len(wildcards)


def configure_suffixes_from(settings: Any) -> int:
    """:func:`configure_suffixes` from ``[mcp.grounding]``."""
    return configure_suffixes(
        settings.str_list("mcp.grounding.multi_part_suffixes", list(MULTI_PART_SUFFIXES)),
        public_suffix_file=settings.str_(
            "mcp.grounding.public_suffix_file", "/usr/share/publicsuffix/public_suffix_list.dat"
        ),
        sections=settings.str_list("mcp.grounding.public_suffix_sections", ["icann"]),
    )


def _suffix_length(labels: Sequence[str], suffixes: Sequence[str] | None) -> int:
    """How many trailing labels of ``labels`` are a public suffix (at least one)."""
    if suffixes is not None:
        rules, wildcards, exceptions = {str(s).lower().strip(".") for s in suffixes}, set(), set()
    else:
        rules, wildcards, exceptions = _SUFFIXES
    longest = 1
    for size in range(2, len(labels) + 1):
        tail = ".".join(labels[-size:])
        if tail in exceptions:
            return size - 1
        if tail in rules:
            longest = size
        elif ".".join(labels[-size + 1 :]) in wildcards:
            longest = size
    return longest


def registrable_domain(host: str, suffixes: Sequence[str] | None = None) -> str:
    """``api.opensuse.org`` and ``build.opensuse.org`` -> ``opensuse.org``.

    ``suffixes`` overrides the configured ones (:func:`configure_suffixes`).
    A host that *is* a public suffix is returned whole.
    """
    labels = [label for label in str(host or "").strip(".").lower().split(".") if label]
    if len(labels) < 2:
        return ".".join(labels)
    size = _suffix_length(labels, suffixes)
    return ".".join(labels[-min(size + 1, len(labels)) :])


def same_organisation(host: str, other: str) -> bool:
    """Do these two hostnames belong to the same operator?

    ``build.opensuse.org`` and ``api.opensuse.org`` do -- Open Build Service's
    web interface and its API, and the reason ``osc-mcp`` pointed at the second
    is a correct answer for the first. ``bugzilla.opensuse.org`` and
    ``bugzilla.fastmcp.app`` do not, which is the whole difference between a
    sibling host and a stranger's multi-tenant service.

    Weaker than :func:`same_service` on purpose, and never the *only* reason an
    entry is believed: requiring the same *host* is the mistake that refused
    four working servers, and this is a claim about who runs the machine rather
    than about which machine it is.
    """
    left, right = registrable_domain(host), registrable_domain(other)
    return bool(left) and left == right


# -- names: a substring is not a name ------------------------------------------
#
# Every test in this module that asks "does this text name X?" goes through the
# helpers below, and none of them matches a bare substring. The entry that made
# that a rule:
#
#     "gitlab-example-net": {"type": "http", "url": "https://api.mcp.ai/p_examplep_administradores"}
#
# -- a Brazilian insurance regulator's tool hosted on a third party's platform,
# found by an official-registry search for "example" and registered for six Example
# hosts and github.com. "example" is a substring of "examplep"; the registry name
# ``io.github.mcp-dir/examplep_administradores-mcp`` contains "github"; and the URL
# path contains both. Three substring tests read those as "names the product"
# and "the vendor said so", and nothing in the entry had anything to do with
# any of the targets.

#: Registry namespaces that name an *account on a hosting service* rather than
#: a domain somebody proved they own. The official MCP registry writes a server
#: published through a GitHub login as ``io.github.<account>/<server>``: the
#: account is whoever published it, and ``github`` in it says nothing about
#: GitHub. Any other namespace is reverse DNS for a domain the publisher had to
#: verify -- ``com.googleapis.developerknowledge/mcp`` is Google's. The
#: packaged list is ``mcp.packages.hosting_namespaces``; this is its fallback.
HOSTING_NAMESPACES = ("io.github", "io.gitlab", "io.bitbucket", "org.codeberg")

#: ``<reverse-dns namespace>/<server>`` -- the shape of an official registry
#: name. Looked for only after URIs are taken out, so a URL's path is never
#: read as one.
_REGISTRY_NAME = re.compile(
    r"(?<![\w.\-/:@])"
    r"([a-z0-9](?:[a-z0-9_\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_\-]*[a-z0-9])?)+)"
    r"/([\w.\-]+)",
    re.IGNORECASE,
)


def names_word(word: str, text: str) -> bool:
    """Does ``text`` contain ``word`` as a whole word, not inside another one?

    Anything that is not a letter or a digit is a boundary, so ``example`` is
    named by ``example-mcp``, ``Example's`` and ``mcp_example`` -- and not by ``examplep``.
    """
    word = str(word or "").strip().lower()
    if not word:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", str(text or "").lower()) is not None


def mentions_host(text: str, host: str) -> bool:
    """Does ``text`` name ``host`` (or a subdomain of it) as a hostname?

    Label boundaries, the same rule :func:`same_service` applies:
    ``https://mcp.jira.example.com`` names ``jira.example.com``, and
    ``jira.example.com.evil.net`` and ``notjira.example.com`` do not.
    """
    host = str(host or "").strip(".").lower()
    if not host:
        return False
    pattern = rf"(?<![a-z0-9\-]){re.escape(host)}(?![a-z0-9\-]|\.[a-z0-9])"
    return re.search(pattern, str(text or "").lower()) is not None


#: Fallback for ``[mcp.registration.identity.organisation_domains]``: domains an
#: organisation owns whose name merely begins with its word. Anything else that
#: begins with the word (githubcard.com) is somebody else's.
ORGANISATION_DOMAINS: dict[str, tuple[str, ...]] = {
    "github": ("githubcopilot.com", "githubusercontent.com", "githubassets.com"),
    "google": ("googleapis.com", "googleusercontent.com", "googlesource.com"),
}
_ORGANISATION_DOMAINS: dict[str, tuple[str, ...]] = dict(ORGANISATION_DOMAINS)
#: ``mcp.registration.identity.prefix_needs_listing``.
_PREFIX_NEEDS_LISTING = True


def configure_organisations(settings: Any) -> None:
    """Read ``organisation_domains`` / ``prefix_needs_listing`` (once per run, by :func:`identity_rule_of`)."""
    global _ORGANISATION_DOMAINS, _PREFIX_NEEDS_LISTING
    if settings is None:
        return
    table = settings.dict_("mcp.registration.identity.organisation_domains", ORGANISATION_DOMAINS) or {}
    _ORGANISATION_DOMAINS = {str(k).lower(): tuple(str(d).lower() for d in (v or [])) for k, v in table.items()}
    _PREFIX_NEEDS_LISTING = settings.bool_("mcp.registration.identity.prefix_needs_listing", True)


def _begins_with_organisation(word: str, host: str) -> bool:
    """Does ``host``'s registrable domain merely begin with ``word`` (listed or not)?"""
    word = str(word or "").strip().lower()
    labels = [label for label in str(host or "").strip(".").lower().split(".") if label]
    if not word or not labels:
        return False
    organisation = registrable_domain(".".join(labels)).split(".")[0]
    return bool(organisation) and organisation.startswith(word)


def names_organisation(word: str, host: str, *, loose: bool = True) -> bool:
    """Is ``word`` the organisation behind ``host``?

    A whole label always counts -- ``github`` in ``docs.github.com``. The
    registrable domain's own label may also merely *begin* with it, because
    that is how a vendor names the sibling domain it serves from:
    ``api.githubcopilot.com`` is GitHub's, ``developerknowledge.googleapis.com``
    is Google's. No other label is read that loosely, so ``examplep.mcp.ai`` is not
    Example's: ``mcp`` is the organisation there.
    """
    word = str(word or "").strip().lower()
    labels = [label for label in str(host or "").strip(".").lower().split(".") if label]
    if not word or not labels:
        return False
    if word in labels:
        return True
    if not loose:
        return False
    domain = registrable_domain(".".join(labels))
    organisation = domain.split(".")[0]
    if not organisation or not organisation.startswith(word):
        return False
    # A name merely beginning with the word is anybody's: githubcard.com begins
    # with "github" exactly as api.githubcopilot.com does, and
    # com.googleapis.developerknowledge is Google's while com.githubcard is not.
    # No rule over names tells them apart; which domains an organisation owns is
    # knowledge, kept as configuration (mcp.registration.identity.organisation_domains).
    return not _PREFIX_NEEDS_LISTING or domain in _ORGANISATION_DOMAINS.get(word, ())


def registry_owner(name: str, hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES) -> tuple[str, str]:
    """Who published a registry entry: ``("account", …)``, ``("domain", …)`` or ``("", "")``.

    ``io.github.mcp-dir/examplep_administradores-mcp`` -> ``("account", "mcp-dir")``.
    ``com.googleapis.developerknowledge/mcp`` ->
    ``("domain", "developerknowledge.googleapis.com")``. The part after the
    slash is the publisher's choice of name and says nothing about who they are,
    so it is never part of the answer.
    """
    namespace, slash, _slug = str(name or "").strip().partition("/")
    namespace = namespace.strip().lower()
    labels = [label for label in namespace.split(".") if label]
    if not slash or len(labels) < 2:
        return "", ""
    prefixes = sorted(
        {str(p).strip().strip(".").lower() for p in hosting_namespaces if str(p).strip()},
        key=len,
        reverse=True,
    )
    for prefix in prefixes:
        if namespace == prefix:
            return "", ""
        if namespace.startswith(prefix + "."):
            return "account", namespace[len(prefix) + 1 :]
    return "domain", ".".join(reversed(labels))


def _evidence_in(text: str, hosting_namespaces: Sequence[str], *, keep_slugs: bool) -> tuple[str, list[str]]:
    """Split a clause into the prose that may name something, and the hosts it names.

    URIs contribute their host and nothing else -- a path is somebody's choice
    of string, not a name. A registry name contributes its owner (a domain joins
    the hosts, an account stays in the prose); its server slug is kept only when
    the question is what the *package* is about (``keep_slugs``), never when it
    is who vouched for it. Bare FQDNs join the hosts.
    """
    prose = str(text or "")
    hosts: list[str] = []
    for uri in _URI.findall(prose):
        host = host_of(uri)
        if host:
            hosts.append(host)
        prose = prose.replace(uri, " ")

    def owner(match: re.Match[str]) -> str:
        kind, who = registry_owner(match.group(0), hosting_namespaces)
        if not kind:
            return match.group(0)
        slug = f" {match.group(2)}" if keep_slugs else ""
        if kind == "domain":
            hosts.append(who)
            return f" {slug} "
        return f" {who}{slug} "

    prose = _REGISTRY_NAME.sub(owner, prose)
    for fqdn in _FQDN.findall(prose):
        hosts.append(fqdn.lower())
    prose = _FQDN.sub(" ", prose)
    return prose, hosts


def text_names(
    word: str,
    text: str,
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
    *,
    keep_slugs: bool = True,
) -> bool:
    """Does this text name ``word`` -- as a word, a host's organisation, or a registry owner?"""
    prose, hosts = _evidence_in(text, hosting_namespaces, keep_slugs=keep_slugs)
    # A host named in the text counts as the organisation's by a whole label or
    # a listed domain (names_organisation): com.githubcard/mcp is githubcard.com speaking.
    return names_word(word, prose) or any(names_organisation(word, h) for h in hosts)


def hosting_namespaces_of(settings: Any) -> list[str]:
    """The configured hosting namespaces, for the callers that hold settings."""
    if settings is None:
        return list(HOSTING_NAMESPACES)
    return settings.str_list("mcp.packages.hosting_namespaces", list(HOSTING_NAMESPACES))


#: Words with which a server describes itself as a front for *other* servers:
#: a marketplace, a catalog of integrations, a directory to install from. Read
#: as whole words in its tool names and instructions. A server like that names
#: every product it can reach, so its naming the target is not identity.
GATEWAY_MARKERS = ("marketplace", "catalog", "catalogue")

#: How much of a server's self-description a refusal quotes.
DESCRIPTION_CHARS = 160


@dataclass(frozen=True)
class IdentityRule:
    """What a claim needs before it ties a third-party server to a target.

    ``enabled`` turns the rule off (``mcp.registration.identity.enabled``).
    ``generic_labels`` are the host labels that are not a product or an
    organisation -- the package stage's list, so both ask about the same words.
    ``accept_unanswered`` lets a claim stand for a server that has not given an
    answer to read, typically one behind a credential wall.
    """

    enabled: bool = True
    gateway_markers: tuple[str, ...] = GATEWAY_MARKERS
    generic_labels: tuple[str, ...] | None = None
    accept_unanswered: bool = True
    #: ``mcp.registration.identity.organisation_needs_tie``: a sibling host of
    #: the target's organisation counts only with a tie (:func:`organisation_tie`).
    organisation_needs_tie: bool = True
    #: ``mcp.registration.identity.third_party_needs_host``: on a host that is
    #: neither the target's nor its organisation's, and with nobody speaking for
    #: the target (:func:`vendor_said_so`), the server's own answer must name the
    #: target's *address*; a product word is what a service *about* the product
    #: says too (githubcard.com: "turns a GitHub user ... into a card").
    third_party_needs_host: bool = True
    #: ``mcp.registration.identity.foreign_login_refuses``: a server believed on
    #: its own words (or a third party's) whose every login variable names
    #: another organisation (APIFY_TOKEN for github.com) logs in to that one.
    foreign_login_refuses: bool = True
    #: ``mcp.registration.identity.login_generic_words``: the words of a login
    #: variable that name no organisation.
    login_generic_words: tuple[str, ...] = (
        "API",
        "KEY",
        "KEYS",
        "TOKEN",
        "TOKENS",
        "ACCESS",
        "PERSONAL",
        "SECRET",
        "AUTH",
        "AUTHORIZATION",
        "PASSWORD",
        "PASSWD",
        "PASS",
        "USER",
        "USERNAME",
        "LOGIN",
        "BEARER",
        "CLIENT",
        "ID",
        "MCP",
        "SERVER",
        "BRIDGE",
        "APP",
        "PAT",
        "OAUTH",
        "CREDENTIAL",
        "CREDENTIALS",
        "SESSION",
        "COOKIE",
        "PRIVATE",
        "BASE64",
        "OF",
        "HEADER",
        "HEADERS",
        "VALUE",
        "SSH",
        "FILE",
        "CERT",
        "BASIC",
    )

    def words(self, host: str) -> list[str]:
        """The product and organisation words of ``host``, three letters or more."""
        from .registry_client import GENERIC_HOST_LABELS, product_tokens  # imports this module

        generic = GENERIC_HOST_LABELS if self.generic_labels is None else self.generic_labels
        return [w for w in product_tokens(host, generic) if len(w) >= 3]


def identity_rule_of(settings: Any) -> IdentityRule:
    """The configured :class:`IdentityRule`, for the callers that hold settings."""
    if settings is None:
        return IdentityRule()
    from .registry_client import GENERIC_HOST_LABELS

    configure_organisations(settings)

    return IdentityRule(
        enabled=settings.bool_("mcp.registration.identity.enabled", True),
        gateway_markers=tuple(settings.str_list("mcp.registration.identity.gateway_markers", list(GATEWAY_MARKERS))),
        generic_labels=tuple(settings.str_list("mcp.packages.generic_host_labels", list(GENERIC_HOST_LABELS))),
        accept_unanswered=settings.bool_("mcp.registration.identity.accept_unanswered", True),
        organisation_needs_tie=settings.bool_("mcp.registration.identity.organisation_needs_tie", True),
        third_party_needs_host=settings.bool_("mcp.registration.identity.third_party_needs_host", True),
        foreign_login_refuses=settings.bool_("mcp.registration.identity.foreign_login_refuses", True),
        login_generic_words=tuple(
            str(w).upper()
            for w in settings.str_list(
                "mcp.registration.identity.login_generic_words", list(IdentityRule().login_generic_words)
            )
        ),
    )


def self_description(candidate: Candidate) -> list[str]:
    """What the running server said it is: its name, instructions, tools, prompts."""
    shake = candidate.handshake
    if shake is None or not shake.ok:
        return []
    parts = [shake.server_name, shake.instructions, *shake.tools, *shake.prompts, *shake.resources, *shake.advertised]
    return [str(p) for p in parts if str(p or "").strip()]


def _as_words(text: str) -> str:
    """``getJiraIssue`` read as ``get Jira Issue``, so a tool name can name a product."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))


def gateway_marker(candidate: Candidate, markers: Sequence[str] = GATEWAY_MARKERS) -> str:
    """The marker by which this server describes itself as a front, or ``""``."""
    shake = candidate.handshake
    if shake is None or not shake.ok:
        return ""
    texts = [shake.instructions, *shake.tools]
    for marker in markers:
        if any(names_word(str(marker).lower(), _as_words(t).lower()) for t in texts):
            return str(marker)
    return ""


def server_names_target(candidate: Candidate, host: str, words: Sequence[str]) -> str:
    """The target word the server's own answer names, or ``""``. See :class:`IdentityRule`."""
    said = server_said_so(candidate, host)
    if said:
        return host
    text = _as_words(" ".join(self_description(candidate))).lower()
    return next((w for w in words if names_word(w, text)), "")


def described_as(candidate: Candidate, limit: int = DESCRIPTION_CHARS) -> str:
    """A short, quotable account of what the server said it is."""
    shake = candidate.handshake
    if shake is None:
        return "nothing"
    bits = []
    if shake.server_name:
        bits.append(repr(shake.server_name))
    if shake.instructions:
        bits.append(repr(" ".join(shake.instructions.split())[:limit]))
    if shake.tools:
        bits.append("tools " + ", ".join(shake.tools[:6]))
    return "; ".join(bits) or "nothing about itself"


def belongs_to(where: str, host: str, words: Sequence[str], *, loose: bool = True) -> bool:
    """Is ``where`` the target's own host, its organisation's, or named after it?

    ``loose=False``: "named after it" only as a whole label -- a domain merely
    beginning with the word (githubcard.com) is not the organisation's.
    """
    return bool(where) and (
        same_service(host, where)
        or same_organisation(host, where)
        or any(names_organisation(w, where, loose=loose) for w in words)
    )


def organisation_tie(candidate: Candidate, host: str, where: str, rule: IdentityRule | None = None) -> str:
    """What ties ``where``, a sibling host of ``host``'s organisation, to ``host`` itself; "" when nothing does.

    One organisation runs many services. ``api.opensuse.org`` is Open Build
    Service's API host and the right place for build.opensuse.org's server to
    connect; ``dashboard.qa.example.net`` is Example's QA dashboard, which has
    nothing to do with smelt.example.net -- yet "same organisation" let it become
    smelt's "native endpoint", close every tier below it, and take the place
    of the GraphQL server that worked. So the organisation is a tie only with
    one of these:

    * ``where`` carries no label of its own but generic ones -- ``api.``,
      ``mcp.``, ``www.`` (``mcp.packages.generic_host_labels``): the
      organisation's front, not another product;
    * ``host`` itself is the organisation's (``www.example.com``);
    * ``where`` carries one of ``host``'s product words (``smelt-api.example.net``);
    * the server's own answer names one of them.

    ``mcp.registration.identity.organisation_needs_tie = false`` restores the
    older rule, where the organisation alone was enough.
    """
    rule = rule or IdentityRule()
    if not rule.organisation_needs_tie:
        return f"{where} belongs to the same organisation as {host}"
    from .registry_client import GENERIC_HOST_LABELS  # imports this module

    generic = {str(g).lower() for g in (GENERIC_HOST_LABELS if rule.generic_labels is None else rule.generic_labels)}

    def own_labels(name: str) -> list[str]:
        name = str(name or "").strip(".").lower()
        domain = registrable_domain(name)
        rest = name[: -len(domain)].rstrip(".") if domain and name.endswith(domain) else ""
        return [label for label in rest.split(".") if label]

    theirs = [label for label in own_labels(where) if label not in generic]
    if not theirs:
        return f"{where} is a front of {registrable_domain(where)} itself, not another of its products"
    mine = [label for label in own_labels(host) if label not in generic]
    if not mine:
        return f"{host} is the organisation's own host, and {where} is one of its services"
    organisation = registrable_domain(host).split(".")[0]
    products = [w for w in rule.words(host) if w != organisation and not w.startswith(organisation + "-")]
    for word in products:
        if any(names_word(word, label) for label in own_labels(where)):
            return f"{where} carries {host}'s product name {word!r}"
    text = _as_words(" ".join(self_description(candidate))).lower()
    for word in products:
        if names_word(word, text):
            return f"the server's own answer names {word!r}, {host}'s product"
    return ""


def organisation_only(candidate: Candidate, host: str, where: str, rule: IdentityRule | None = None) -> bool:
    """Is the organisation all ``where`` shares with ``host`` -- the same organisation, and no tie?"""
    return (
        bool(where)
        and not same_service(host, where)
        and same_organisation(host, where)
        and not organisation_tie(candidate, host, where, rule)
    )


def identity_doubt(candidate: Candidate, host: str, locations: Sequence[str], rule: IdentityRule | None = None) -> str:
    """Why a claim cannot tie this server to ``host``, or ``""`` when it may.

    Asked only of a verdict resting on a claim. ``locations`` are the hosts the
    entry connects to. If any of them is the target's, its organisation's, or
    named after it, the claim is the ordinary shape of a hosted offering and
    stands. Otherwise the server is on a third party's host, and the claim needs
    the server's own answer to name the target -- which a catalog of other
    servers' tools does not count as doing.
    """
    rule = rule or IdentityRule()
    if not rule.enabled:
        return ""
    words = rule.words(host)
    hosts = [(host_of(h) or str(h).lower()) for h in locations if h]
    if not hosts or any(belongs_to(h, host, words) for h in hosts):
        return ""
    # A domain merely beginning with the organisation's word that is not listed
    # as its own (prefix_needs_listing) is its own when the vendor said so by
    # name -- GitHub's documentation naming a sibling domain -- never for the
    # name alone (githubcard.com, from another publisher's registry entry).
    if any(_begins_with_organisation(w, h) for w in words for h in hosts) and vendor_said_so(candidate, host):
        return ""
    where = hosts[0]
    shake = candidate.handshake
    if shake is None or not shake.ok:
        if rule.accept_unanswered:
            return ""
        return (
            f"{where} belongs to neither {host} nor its organisation, and it gave no answer that "
            f"could show it serves {host}"
        )
    marker = gateway_marker(candidate, rule.gateway_markers)
    if marker:
        return (
            f"{where} belongs to neither {host} nor its organisation, and the server describes "
            f"itself as a front for other servers ({marker!r}; it said {described_as(candidate)}) "
            f"-- a catalog is not {host}'s server, whatever it lists"
        )
    if server_said_so(candidate, host):
        return ""
    if rule.third_party_needs_host and not vendor_said_so(candidate, host):
        # Nobody with authority for the target pointed here -- a keyword hit, a
        # registry entry another publisher owns (com.githubcard/mcp is
        # githubcard.com speaking, not GitHub) -- and your data would go to a
        # third party.
        # Your data would go to a third party: naming the product is what a
        # service *about* it says too. github.com got githubcard.com's card
        # maker ("turns a GitHub user or repository into a card") as its server.
        named = server_names_target(candidate, host, words)
        return (
            f"{where} belongs to neither {host} nor its organisation, and the server's own answer never "
            f"names {host} itself"
            + (f" -- it names {named!r}, as a service about it would" if named else "")
            + f"; it said {described_as(candidate)}. A third party's server must show it serves {host}"
        )
    if server_names_target(candidate, host, words):
        return ""
    return (
        f"{where} belongs to neither {host} nor its organisation, and the server's own answer "
        f"never names {host}{' or ' + ', '.join(words) if words else ''} -- it said "
        f"{described_as(candidate)}. A claim that it serves {host} is not borne out by what it "
        "says it is"
    )


def points_elsewhere(candidate: Candidate, target: str, rule: IdentityRule | None = None) -> bool:
    """Does this entry connect only to hosts that are not the target's in any sense?

    Separates "nothing ties it to the target" from "it demonstrably connects to
    someone else": an entry naming no address at all may be a generic server
    configured some other way, and is not called wrong for that.
    """
    rule = rule or IdentityRule()
    host = host_of(target)
    if not host:
        return False
    words = rule.words(host)
    hosts = locations_of(candidate)
    return bool(hosts) and not any(belongs_to(h, host, words) for h in hosts)


def locations_of(candidate: Candidate) -> list[str]:
    """The hosts an entry connects to: its URL's, or every address its launch names.

    Hosts only -- never the URL or the command line, which may carry a
    credential -- so the answer is safe to report.
    """
    locations = [candidate.url] if candidate.kind != "stdio" and candidate.url else addresses_in(candidate)
    hosts: list[str] = []
    for location in locations:
        found = host_of(location) or str(location or "").lower()
        if found and found not in hosts:
            hosts.append(found)
    return hosts


def addresses_in(candidate: Candidate) -> list[str]:
    """Every address-shaped token in the entry, for the refusal message.

    Only ever used to say *which* refusal this is -- "it names no address at
    all" reads very differently from "it names something else".

    A filename is not an address, and saying so matters more than it sounds: a
    real refusal read "this entry names bridge.js rather than
    developers.google.com", which describes the entry as pointing somewhere
    else when in truth it pointed nowhere at all. The remedy the operator needs
    is different in the two cases.
    """
    found: list[str] = []
    for text in [*candidate.full_command(), *_vouched_env(candidate).values()]:
        blob = str(text)
        uris = _URI.findall(blob)
        for match in uris:
            if match not in found:
                found.append(match)
        for match in _FQDN.findall(blob):
            # The host inside a URL is not a second address. Reporting
            # "https://jira.example.com, jira.example.com" would read as two places
            # the entry mentions when it is one.
            if match in found or any(match in uri for uri in uris):
                continue
            if _looks_like_a_filename(match, blob):
                continue
            found.append(match)
    return found


def _vouched_env(candidate: Candidate) -> dict[str, str]:
    """The entry's variables that may count as evidence of what it talks to.

    All of them, except those this program filled in itself from the target's
    address without the variable's name saying which service it is for
    (:attr:`Candidate.unvouched_env`). Evidence may not be its own source: a
    value we wrote cannot also be the proof that the package we wrote it into
    is for the target.
    """
    unvouched = getattr(candidate, "unvouched_env", None) or set()
    return {k: v for k, v in candidate.env.items() if k not in unvouched}


def _unvouched_note(candidate: Candidate) -> str:
    """Why a filled-in address was not read, for a refusal that would otherwise puzzle."""
    unvouched = sorted(getattr(candidate, "unvouched_env", None) or ())
    if not unvouched:
        return ""
    return (
        f" ({', '.join(unvouched)} carries the target's address only because this run filled it "
        "in -- a generic variable the entry declared -- so it is not evidence of what the "
        "package is for)"
    )


def _looks_like_a_filename(token: str, blob: str) -> bool:
    """``bridge.js``, ``server.py``, ``manifest.json`` -- a name, not a host.

    Two signals, either of which is enough: the last label is a known file
    extension, or the token sits inside something with a path separator, which
    a hostname on its own never does.
    """
    if token.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS:
        return True
    for word in str(blob).split():
        if token in word and "/" in word and "://" not in word:
            return True
    return False


def answered_as_mcp(candidate: Candidate) -> bool:
    """Did this address reply the way only an MCP endpoint replies?

    A completed handshake, or a credential wall carrying protocol-shaped
    evidence -- the standard :mod:`.probes` already applies, and the one that
    separates GitHub's ``401`` from Confluence's login page. Not "connected":
    an endpoint whose token nobody has exported yet is still an endpoint.
    """
    return bool(candidate.verified or candidate.needs_credential)


def vendor_said_so(
    candidate: Candidate,
    target: str = "",
    markers: Sequence[str] = VENDOR_PROVENANCE,
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
) -> str:
    """Did the target itself, or a registry entry for it, point at this address?

    This is the only thing that separates an address the vendor published from
    one a keyword search turned up, and hostnames cannot do it:
    ``api.githubcopilot.com`` is GitHub's own and ``bugzilla.fastmcp.app`` is a
    third party's, and nothing in the two names says which is which.

    What does say it is *how we came to hear of it*. GitHub's endpoint is in
    GitHub's documentation; Google's is in the official registry under
    ``com.googleapis.*``; ``bugzilla.fastmcp.app`` came from a web search. The
    first two are the vendor speaking; the third is a stranger's suggestion, and
    a multi-tenant host has to be *told* which instance to serve before it means
    anything.

    *Whose* is read from names, never substrings (see :func:`text_names`): the
    organisation has to be a whole word of the clause, a host the clause or the
    entry names, or the owner of a registry name. A registry entry published
    from ``io.github.<account>`` is that account speaking, not GitHub; its
    server slug is the publisher's own choice of words; and a URL's path is
    nobody's name. All three once vouched for
    ``https://api.mcp.ai/p_examplep_administradores`` as Example's and GitHub's.
    """
    clauses = [str(candidate.source or ""), *[str(n) for n in candidate.notes]]
    anchor = _provenance_anchor(target)
    # The address is the other place the owner's name can appear, and for a
    # hosted offering it usually does: `api.githubcopilot.com` is GitHub
    # speaking about GitHub. `mcp.atlassian.com` is Atlassian speaking about
    # Atlassian, while the target is somebody else's Confluence. Hosts only --
    # the path after them is not a name.
    addressed = [_host_of(a) or a.lower() for a in [str(candidate.url or ""), *addresses_in(candidate)] if a]
    # A host merely beginning with the organisation's word vouches for itself
    # only when listed as the organisation's (names_organisation): githubcard.com does not.
    owned = bool(anchor) and any(names_organisation(anchor, h) for h in addressed)
    for clause in clauses:
        lowered = clause.lower()
        for marker in markers:
            if not marker or str(marker).lower() not in lowered:
                continue
            if anchor and not owned and not text_names(anchor, clause, hosting_namespaces, keep_slugs=False):
                # A marker with no owner is not provenance. "documentation"
                # matched *Atlassian's* documentation while the target was
                # confluence.example.com -- the vendor of the product describing
                # the vendor's own multi-tenant cloud, which does not serve a
                # customer's self-hosted instance of it. The question this
                # function exists to answer is "who said so", and the answer has
                # to be the target's own organisation for it to mean anything.
                continue
            return str(marker)
    return ""


def _provenance_anchor(target: str) -> str:
    """The word that marks a claim as coming from the target's own side.

    The organisation's label -- ``example`` from ``confluence.example.com``, ``github``
    from ``github.com``, ``google`` from ``developers.google.com``. Deliberately
    the registrable domain's first label rather than the full hostname: a vendor
    writes its own name, not an FQDN, both in its documentation and in the
    sibling domain it serves from, and requiring the FQDN would refuse GitHub's
    documented ``api.githubcopilot.com`` along with Atlassian's cloud.

    This is not "infer belonging from a hostname" -- belonging is still decided
    by the probe and by the provenance marker. This decides only *whose claim it
    is*, which is the one question a hostname can actually answer.
    """
    host = _host_of(target) or str(target or "").strip().lower()
    domain = registrable_domain(host) if host else ""
    return domain.split(".")[0] if domain else ""


def endpoint_hosts(outcome: Any) -> list[str]:
    """Hosts this run has evidence are serving ``outcome``'s target.

    The tier-1 checklist records the URL of every native endpoint found, and the
    candidates that verified carry theirs. Those hosts are *this run's own
    findings*, which is a far better test of "does this address belong to the
    target" than comparing hostnames -- and it is what lets a bridge built
    against ``developerknowledge.googleapis.com`` be believed for
    ``developers.google.com`` without believing anything else.
    """
    record = getattr(outcome, "due_diligence", None)
    urls: list[str] = []
    for field_name in ("native_endpoints", "auth_walls", "needs_bridge"):
        urls.extend(getattr(record, field_name, None) or [])
    for group in ("verified", "provisional"):
        urls.extend(c.url for c in (getattr(outcome, group, None) or []) if getattr(c, "url", ""))
    hosts: list[str] = []
    for url in urls:
        host = _host_of(str(url))
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def assess(
    candidate: Candidate,
    target: str,
    *,
    allow_front_door: bool = False,
    served_by: Sequence[str] = (),
    probed: bool = True,
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
    identity: IdentityRule | None = None,
) -> Grounding:
    """Decide whether this entry can be believed for ``target``.

    ``served_by`` is the set of hosts *this run* found serving the target -- see
    :func:`endpoint_hosts`. An entry naming one of those is believed, because
    the evidence tying it to the target is the run's own, not a guess from the
    spelling of a hostname.
    """
    host = _host_of(target)
    if not host:
        return Grounding(GROUNDED, "no target host to check against")
    known = [h for h in (_host_of(h) or h.strip().lower() for h in served_by) if h]

    # A claim may not be its own corroboration. ``served_by`` is built from the
    # tier-1 checklist *and from the candidates themselves*, so a URL candidate
    # is normally in it -- and "X is served from X, which this run found
    # answering for the target" is the candidate's own assertion handed back as
    # independent evidence. That circle registered https://mcp.atlassian.com/v2/mcp
    # for confluence.example.com: Atlassian Cloud's multi-tenant endpoint, which
    # serves `*.atlassian.net` tenants and knows nothing of a self-hosted Data
    # Center wiki.
    #
    # Only the candidate's *own* address is withdrawn, and only for a URL, which
    # is the only kind of candidate that can be the address it names. A bridge
    # relaying to a host the endpoint stage found separately is corroborated by
    # a different finding, which is the whole point of `served_by`.
    if candidate.kind != "stdio" and candidate.url:
        itself = _host_of(candidate.url)
        known = [h for h in known if not (itself and same_service(h, itself))]
    if candidate.kind != "stdio":
        return _assess_url(
            candidate,
            host,
            allow_front_door=allow_front_door,
            known=known,
            probed=probed,
            hosting_namespaces=hosting_namespaces,
            identity=identity,
        )
    # A launcher's verdict does not depend on a handshake -- nothing about
    # running a process says where it connects -- so it is the same before and
    # after probing, and `probed` is not consulted.
    return _assess_launcher(candidate, host, known=known, hosting_namespaces=hosting_namespaces, identity=identity)


def server_said_so(candidate: Candidate, host: str, quote_chars: int = QUOTED_EVIDENCE_CHARS) -> str:
    """Did the running server itself name this host? Returns where, or ``""``.

    The strongest tie there is, and the last one this program learned to use.
    Every other test reads the *entry* -- a command line, an environment value,
    a URL -- which says what somebody configured. This reads what the server
    said after it was already talking: its ``instructions``, its tool and prompt
    descriptions, its resource URIs, the defaults in its input schemas.

    A resource URI is the clearest of them. An Open Build Service server lists
    ``https://build.opensuse.org/…``; ``@ateam-ai/mcp``, which reached
    ``build.opensuse.org`` on an npm keyword search, lists nothing of the kind.
    That distinction is invisible in the entry and obvious in the handshake, and
    it is exactly the distinction the entry-only rule could not make -- so the
    correct server and the irrelevant one were refused alike, and the address
    ended with nothing.

    Matched by host, through :func:`same_service`, never as a bare substring:
    ``build.opensuse.org.evil.net`` in a description is not this target.
    """
    shake = candidate.handshake
    if not host or shake is None or not shake.ok:
        return ""
    for text in shake.advertised:
        for address in _addresses_in_text(str(text)):
            other = _host_of(address) or address
            if other and same_service(host, other):
                return str(text)[:quote_chars]
    return ""


def _addresses_in_text(text: str) -> list[str]:
    """Every URI and bare FQDN in one piece of prose."""
    found = [m.group(0) for m in _URI.finditer(text)]
    found += [m.group(0) for m in _FQDN.finditer(text)]
    return found


def _names_target(candidate: Candidate, host: str) -> tuple[str, str]:
    """Where in the entry the target's own address appears, if it does."""
    places: list[tuple[str, str]] = [("the command", " ".join(candidate.full_command()))]
    places += [(f"env {name}", str(value)) for name, value in _vouched_env(candidate).items()]
    places += [(f"header {name}", str(value)) for name, value in candidate.headers.items()]
    if candidate.url:
        places.append(("the url", candidate.url))
    for where, text in places:
        # By label, never as a substring: `gitlab.example.com.evil.net` in an
        # argument does not name gitlab.example.com.
        if mentions_host(str(text), host):
            return where, str(text)
    return "", ""


def foreign_login(candidate: Candidate, host: str, rule: IdentityRule | None = None) -> str:
    """Why the server logs in to another organisation than ``host``'s, in words; "" when it does not.

    ``@mambalabsdev/mcp-github-organization-signal-scanner`` named github.com in
    its tools and waited for ``APIFY_TOKEN``: a scraper run on Apify, about
    GitHub, not GitHub's server. Every login variable it reads names an
    organisation (a word outside ``login_generic_words``) that is neither one of
    ``host``'s words nor one of its labels.
    """
    from .secret_policy import is_secret_name, names_a_selector

    rule = rule or IdentityRule()
    if not rule.foreign_login_refuses:
        return ""
    keys = [str(k) for k in candidate.secret_env] or [
        str(k) for k in candidate.env if is_secret_name(str(k)) and not names_a_selector(str(k))
    ]
    if not keys:
        return ""
    mine = {w.upper() for w in rule.words(host)} | {label.upper() for label in host.split(".") if label}
    generic = {w.upper() for w in rule.login_generic_words}
    foreign: list[tuple[str, str]] = []
    for key in keys:
        words = [w for w in re.split(r"[^A-Za-z0-9]+", key.upper()) if w and not w.isdigit()]
        named = [w for w in words if w not in generic]
        if not named or any(w in mine for w in named):
            return ""  # a login of the target's own, or one naming nobody
        foreign.append((key, named[0]))
    key, word = foreign[0]
    return (
        f"it logs in to {word.lower()} ({key}), not to {host}: every login it reads is another organisation's, "
        f"so it works on {word.lower()}'s side -- a service about {host}, not {host}'s server"
    )


def _assess_launcher(
    candidate: Candidate,
    host: str,
    *,
    known: Sequence[str] = (),
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
    identity: IdentityRule | None = None,
) -> Grounding:
    """A stdio entry: it has to say which service it talks to.

    A launcher is never probed at an address -- verification starts a process
    and speaks to its pipes -- so nothing about *running* it shows where it
    connects. Only what we can read in the entry does.

    Naming the target is the strongest form and the usual one:
    ``--bugzilla-server https://bugzilla.opensuse.org``. Failing that, an
    address the vendor published, or one this run already found serving the
    target, is accepted and flagged -- ``osc-mcp`` talks to
    ``api.opensuse.org``, which is Open Build Service's own API host and where
    the server for ``build.opensuse.org`` genuinely lives.

    What is refused is an entry that names nothing, and an entry naming an
    address that is neither the target's, nor published by it, nor anything
    this run saw -- because then nobody, including the operator, can tell what
    it talks to.
    """
    where, _text = _names_target(candidate, host)
    if where:
        return Grounding(GROUNDED, f"{where} names {host}", where, basis=BASIS_NAMED)

    # Before reading the entry any further, ask the server. It is already
    # running and it has already told us what it does; a resource URI under the
    # target's own host settles the question more firmly than any string in the
    # command line, because it is the server speaking rather than whoever
    # configured it. See `server_said_so`.
    said = server_said_so(candidate, host)
    if said:
        foreign = foreign_login(candidate, host, identity)
        if foreign:
            return Grounding(UNGROUNDED, f"the server names {host} ({said}), but {foreign}")
        return Grounding(
            GROUNDED,
            f"the server itself names {host} in what it advertises ({said})",
            "the handshake",
            basis=BASIS_SERVER,
        )

    others = addresses_in(candidate)
    if not others:
        return Grounding(
            UNGROUNDED,
            f"this entry names no address at all -- not {host}, not anything else -- and the "
            f"server did not name it either, in its instructions, its tool descriptions or its "
            "resource URIs, so nothing says which service it talks to" + _unvouched_note(candidate),
        )
    for other in known:
        if other and other != host and any(mentions_host(a, other) for a in others):
            if organisation_only(candidate, host, other, identity):
                continue  # recorded on the organisation's word alone: not evidence
            doubt = identity_doubt(candidate, host, [other], identity)
            if doubt:
                return Grounding(UNGROUNDED, doubt)
            return Grounding(
                SERVED_ELSEWHERE,
                f"it is configured for {other}, which this run found serving {host}",
                "the entry",
                other,
                BASIS_FOUND,
            )
    siblings: list[str] = []
    for other in others:
        sibling = _host_of(other) or other
        if same_organisation(host, sibling):
            tie = organisation_tie(candidate, host, sibling, identity)
            if not tie:
                siblings.append(sibling)
                continue
            return Grounding(
                SERVED_ELSEWHERE,
                f"it is configured for {sibling}, the same organisation as {host} -- a service's "
                f"API host is where a server for it usually connects ({tie})",
                "the entry",
                sibling,
                BASIS_ORGANISATION,
            )
    told = vendor_said_so(candidate, host, hosting_namespaces=hosting_namespaces)
    if told:
        doubt = identity_doubt(candidate, host, others, identity) or foreign_login(candidate, host, identity)
        if doubt:
            return Grounding(UNGROUNDED, f"{told} named it, but {doubt}")
        return Grounding(
            SERVED_ELSEWHERE,
            f"it is configured for {others[0]}, named by {told} rather than by {host} itself",
            "the entry",
            _host_of(others[0]) or others[0],
            BASIS_VENDOR,
        )
    if siblings:
        return Grounding(
            UNGROUNDED,
            f"this entry is configured for {siblings[0]}, another service of {host}'s organisation: "
            f"nothing ties it to {host} itself -- {siblings[0]} carries none of {host}'s product "
            "words and the server's answer names none -- so it talks to a different service"
            + _unvouched_note(candidate),
        )
    return Grounding(
        UNGROUNDED,
        f"this entry is configured for {', '.join(others[:2])}, which is not {host}, was not "
        f"published by it, and is not something this run found serving it -- so nothing shows "
        "which service it talks to" + _unvouched_note(candidate),
    )


def _assess_url(
    candidate: Candidate,
    host: str,
    *,
    allow_front_door: bool,
    known: Sequence[str] = (),
    probed: bool = True,
    hosting_namespaces: Sequence[str] = HOSTING_NAMESPACES,
    identity: IdentityRule | None = None,
) -> Grounding:
    """A URL entry: an endpoint, and one we can tie to this target.

    ``probed`` says whether this candidate has been given the chance to answer.
    Two of the tests below rest on whether it did, and asking them of a
    candidate that has not been probed yet does not produce "no" -- it produces
    nonsense. It produced exactly that: the ingest-time check runs *before*
    verification, so ``answered_as_mcp`` was false for everything, and every
    off-host URL was refused before it could be dialled. Google's own endpoint
    was thrown away three times in one run on that basis, and GitHub's once.

    So an unprobed candidate is never refused on an answer nobody has heard.
    It is :data:`UNDECIDED`, which is registrable, and the real verdict is
    taken at publish when the handshake exists.
    """
    url = candidate.url or ""
    where = _host_of(url)
    if not where:
        return Grounding(UNGROUNDED, "the entry has no URL to check")

    path = parse_address(url).path or ""
    # "It never handshaked" is only known after probing; a bare origin that has
    # not been tried yet may still turn out to be an endpoint.
    if probed and path in FRONT_DOOR_PATHS and not allow_front_door and not candidate.verified:
        return Grounding(
            UNGROUNDED,
            f"https://{where} is the site's front door, not an endpoint: it has no path, and "
            "it never completed a handshake -- a login page refusing an anonymous POST looks "
            "exactly like this",
        )

    if same_service(host, where):
        return Grounding(GROUNDED, f"the url is on {where}", "the url", basis=BASIS_HOST)

    # A different hostname. Ordinary for a hosted MCP service -- github.com is
    # served from api.githubcopilot.com -- and ordinary too for a *multi-tenant*
    # host that serves anybody's instance and has to be told which. The two look
    # identical from the hostname, so the hostname is not what decides.
    named, _text = _names_target(candidate, host)
    if named:
        return Grounding(
            SERVED_ELSEWHERE,
            f"{where} is told to serve {host} -- {named} names it",
            named,
            where,
            BASIS_NAMED,
        )
    if probed and not answered_as_mcp(candidate):
        return Grounding(
            UNGROUNDED,
            f"the entry points at {where}, which is not {host}, and which never answered as an "
            "MCP endpoint -- so nothing shows it serves the target",
        )
    sibling_only = organisation_only(candidate, host, where, identity)
    if not sibling_only and any(other and same_service(other, where) for other in known):
        doubt = identity_doubt(candidate, host, [where], identity)
        if doubt:
            return Grounding(UNGROUNDED, doubt)
        return Grounding(
            SERVED_ELSEWHERE,
            f"{host} is served from {where}, which this run found answering as its MCP endpoint",
            "the url",
            where,
            BASIS_FOUND,
        )
    if same_organisation(host, where) and not sibling_only:
        return Grounding(
            SERVED_ELSEWHERE,
            f"{host} is served from {where}, the same organisation, which answered as an MCP "
            f"endpoint ({organisation_tie(candidate, host, where, identity)})",
            "the url",
            where,
            BASIS_ORGANISATION,
        )
    # The server's own account of itself, which outranks provenance: a remote
    # endpoint that lists resources under the target's host has demonstrated
    # what it serves, where "the vendor documented this address" is only a
    # claim about it.
    said = server_said_so(candidate, host)
    if said:
        return Grounding(
            SERVED_ELSEWHERE,
            f"{host} is served from {where}, which names {host} in what it advertises ({said})",
            "the handshake",
            where,
            BASIS_SERVER,
        )
    told = vendor_said_so(candidate, host, hosting_namespaces=hosting_namespaces)
    if told:
        doubt = identity_doubt(candidate, host, [where], identity)
        if doubt:
            return Grounding(UNGROUNDED, f"{told} named it, but {doubt}")
        return Grounding(
            SERVED_ELSEWHERE,
            f"{host} is served from {where}, named by {told} and answering as an MCP endpoint",
            "the url",
            where,
            BASIS_VENDOR,
        )
    if not probed:
        # Everything left to test needs an answer. Say so rather than guessing:
        # refusing here would discard a vendor's own endpoint before it was
        # dialled, and accepting here would promise something unproven.
        return Grounding(
            UNDECIDED,
            f"{where} is not {host}, and whether it serves it cannot be known until it has been "
            "probed -- the registration rule is applied again once it has answered",
            "the url",
            where,
            BASIS_UNPROBED,
        )
    if sibling_only:
        return Grounding(
            UNGROUNDED,
            f"{where} answered as an MCP endpoint, but it is another service of {host}'s "
            f"organisation, not {host}: it carries none of {host}'s product words, {host} is "
            "named nowhere in the entry, and the server's answer names neither -- the same "
            "organisation runs many services",
        )
    # It answered, but only a keyword search ever connected it to this target,
    # and it does not say which instance it serves. bugzilla.fastmcp.app will
    # host anybody's Bugzilla; without `bugzilla_url` it is not this one's.
    return Grounding(
        UNGROUNDED,
        f"{where} answered as an MCP endpoint, but nothing ties it to {host}: it is not the "
        f"target's host, {host} is named nowhere in the entry, and it was found by a search "
        "rather than published by the target. A server that can serve any instance has to be "
        "told which one",
    )


__all__ = [
    "hosts_named",
    "MULTI_PART_SUFFIXES",
    "configure_suffixes",
    "configure_suffixes_from",
    "read_public_suffix_list",
    "BASIS_FOUND",
    "BASIS_HOST",
    "BASIS_NAMED",
    "BASIS_ORGANISATION",
    "BASIS_SERVER",
    "BASIS_UNPROBED",
    "BASIS_VENDOR",
    "FRONT_DOOR_PATHS",
    "GATEWAY_MARKERS",
    "GROUNDED",
    "HOSTING_NAMESPACES",
    "IdentityRule",
    "SERVED_ELSEWHERE",
    "UNDECIDED",
    "UNGROUNDED",
    "Grounding",
    "addresses_in",
    "answered_as_mcp",
    "assess",
    "belongs_to",
    "described_as",
    "gateway_marker",
    "hosting_namespaces_of",
    "identity_doubt",
    "identity_rule_of",
    "locations_of",
    "mentions_host",
    "names_organisation",
    "organisation_only",
    "organisation_tie",
    "names_word",
    "points_elsewhere",
    "registry_owner",
    "same_service",
    "self_description",
    "server_names_target",
    "text_names",
]
