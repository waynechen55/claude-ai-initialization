"""The run's trust store: one answer to "is this certificate trusted", for everything.

build.example.net went missing from ``.mcp.json`` on a host whose store did not
hold Example's CA: discovery's client trusted certifi only, the certificate
failure was reported as an absent host, the YAML description behind Swagger UI
was never found, and each agent extracted a chain and lost it. Nothing here
names that host: every test is about a certificate or a page shape.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core import trust_store
from initialize_claude_ai.core.http import HttpClient, Response
from initialize_claude_ai.core.trust_store import (
    ANCHORED,
    PIN_MODES,
    TRUSTED,
    UNREACHABLE,
    UNTRUSTED,
    TrustFinding,
    TrustStore,
)
from initialize_claude_ai.mcp_auto_discovery import apispec, pathgen
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome
from initialize_claude_ai.phases import all_phases

HOST = "api.example.test"


# -- certificates ------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain() -> dict[str, str]:
    """A root, an intermediate it signed, and a leaf the intermediate signed."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def make(cn: str, issuer=None, *, ca: bool, san: str = ""):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        signer_name, signer_key = (issuer[0], issuer[1]) if issuer else (name, key)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(signer_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), False)
        cert = builder.sign(signer_key, hashes.SHA256())
        return name, key, cert.public_bytes(serialization.Encoding.PEM).decode()

    root = make("Example Root", ca=True)
    inter = make("Example Issuing CA", root, ca=True)
    leaf = make(HOST, inter, ca=False, san=HOST)
    return {"root": root[2], "inter": inter[2], "leaf": leaf[2]}


@pytest.fixture
def store_settings(settings, tmp_path):
    settings.set("mcp.trust.store_dir", str(tmp_path / "trust"))
    settings.set("mcp.trust.bundle_candidates", [])
    settings.set("mcp.trust.bundle", "")
    settings.set("mcp.trust.include_certifi", False)
    return settings


def fake_tls(
    monkeypatch, chain_pems, *, first=("tls-issuer", 20, "unable to get local issuer certificate"), then=("ok", 0, "")
):
    """The host's own store fails as ``first``; anything the run adds answers ``then``."""
    calls: list[bool] = []

    def handshake(host, port, context, timeout):
        added = any("Example" in str(c.get("subject")) for c in context.get_ca_certs())
        calls.append(added)
        return then if added else first

    monkeypatch.setattr(trust_store, "_handshake", handshake)
    monkeypatch.setattr(TrustStore, "_presented_chain", lambda self, h, p, t: list(chain_pems))
    return calls


# -- learning the presented chain ----------------------------------------------------


def test_a_missing_issuer_is_learned_from_what_the_target_presents(store_settings, chain, monkeypatch):
    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    store = TrustStore(store_settings)
    found = store.examine(f"https://{HOST}/api")
    assert found.status == ANCHORED and found.learned
    assert trust_store.common_name(found.issuer) == "Example Issuing CA"
    assert [trust_store.common_name(a["subject"]) for a in found.anchors] == ["Example Issuing CA"]
    assert found.needs_root()  # an intermediate: Python takes it, Node does not
    kept = list(store.anchor_dir().glob("*.pem"))
    assert len(kept) == 1 and "BEGIN CERTIFICATE" in kept[0].read_text()
    assert chain["leaf"].strip() not in kept[0].read_text()  # never the leaf itself


def test_a_kept_chain_is_used_by_the_next_run_without_learning_again(store_settings, chain, monkeypatch):
    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    TrustStore(store_settings).examine(f"https://{HOST}")
    later = TrustStore(store_settings)
    assert later.anchors() and later.adds_anything()
    found = later.examine(f"https://{HOST}")
    assert found.status == ANCHORED and not found.learned


@pytest.mark.parametrize(
    ("chain_of", "first", "target", "mode", "why"),
    [
        (["leaf"], ("tls-issuer", 20, "x"), True, "targets", "not learned"),
        (["leaf", "inter"], ("tls-issuer", 10, "certificate has expired"), True, "targets", "not learned"),
        (["leaf", "inter"], ("tls-issuer", 20, "x"), False, "targets", "not learned"),
        (["leaf", "inter"], ("tls-issuer", 20, "x"), True, "never", "not learned"),
    ],
    ids=["leaf-only", "expired", "not-a-target", "never"],
)
def test_what_is_never_learned(store_settings, chain, monkeypatch, chain_of, first, target, mode, why):
    store_settings.set("mcp.trust.pin_presented_chain", mode)
    fake_tls(monkeypatch, [chain[k] for k in chain_of], first=first)
    store = TrustStore(store_settings)
    found = store.examine(f"https://{HOST}", target=target)
    assert found.status == UNTRUSTED and why in found.detail
    assert not store.anchors()
    assert not (store.anchor_dir().exists() and any(store.anchor_dir().iterdir()))


def test_a_chain_that_does_not_verify_either_is_not_kept(store_settings, chain, monkeypatch):
    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]], then=("tls-issuer", 20, "still no"))
    store = TrustStore(store_settings)
    assert store.examine(f"https://{HOST}").status == UNTRUSTED
    assert not store.anchors()


def test_other_outcomes_are_named_for_what_they_are(store_settings, monkeypatch):
    store = TrustStore(store_settings)
    for outcome, status in (("ok", TRUSTED), ("net", UNREACHABLE), ("tls-other", UNTRUSTED)):
        monkeypatch.setattr(trust_store, "_handshake", lambda *a, o=outcome: (o, 0, o))
        assert store._examine(HOST, 443, target=True).status == status
    assert store.examine("http://plain.example.test") is None  # nothing to verify


def test_a_dry_run_learns_nothing_to_disk(store_settings, chain, monkeypatch):
    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    store = TrustStore(store_settings, dry_run=True)
    assert store.examine(f"https://{HOST}").status == ANCHORED
    assert store.bundle() == ""
    assert not Path(store_settings.str_("mcp.trust.store_dir")).exists()


def test_each_host_is_examined_once_even_under_concurrency(store_settings, chain, monkeypatch):
    calls = fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    store = TrustStore(store_settings)
    threads = [threading.Thread(target=store.examine, args=(f"https://{HOST}/p{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [False, True]  # the host's store once, the learned chain once


# -- what the run hands out ------------------------------------------------------------


def test_the_bundle_is_named_only_for_hosts_that_needed_it(store_settings, chain, monkeypatch):
    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    store = TrustStore(store_settings)
    assert store.bundle_for([f"https://{HOST}"]) == ""  # nothing anchored yet
    store.examine(f"https://{HOST}")
    bundle = store.bundle_for(["--url", f"https://{HOST}/api/v1"])
    assert bundle and Path(bundle).read_text().count("BEGIN CERTIFICATE") == 1
    for elsewhere in (f"https://x{HOST}", f"https://{HOST}-mirror.test", "https://example.test"):
        assert store.bundle_for([elsewhere]) == "", elsewhere
    assert store.bundle_for([f"api_url={HOST.upper()}"]) == bundle


def test_extra_authorities_are_trusted_and_missing_ones_skipped(store_settings, chain, tmp_path):
    ca = tmp_path / "root.pem"
    ca.write_text(chain["root"])
    store_settings.set("mcp.trust.extra_ca", f"{ca}, {tmp_path / 'absent.pem'}")
    store = TrustStore(store_settings)
    assert store.extra_files() == [str(ca)]
    assert any(c["subject"] for c in store.context().get_ca_certs())


def test_the_context_accepts_a_partial_chain_when_configured(store_settings):
    flag = getattr(trust_store.ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    if not flag:
        pytest.skip("this OpenSSL has no partial-chain flag")
    assert TrustStore(store_settings).context().verify_flags & flag
    store_settings.set("mcp.trust.partial_chain", False)
    context = TrustStore(store_settings)._context(include_run=True)
    assert context.verify_flags & flag == trust_store.ssl.create_default_context().verify_flags & flag


def test_the_http_client_uses_the_context_only_when_verifying(store_settings):
    context = TrustStore(store_settings).context()
    assert HttpClient(ssl_context=context).ssl_context is context
    assert HttpClient(ssl_context=context, verify_tls=False).ssl_context is None


def test_the_brief_says_nothing_for_a_host_that_verifies():
    assert trust_store.brief(None, "/b.pem") == ""
    assert trust_store.brief(TrustFinding(HOST, 443, TRUSTED, "ok"), "/b.pem") == ""
    anchored = TrustFinding(HOST, 443, ANCHORED, "x", issuer="CN=Example Issuing CA", anchors=[{"self_signed": False}])
    text = trust_store.brief(anchored, "/b.pem", ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_FILE"])
    assert "/b.pem" in text and "Example Issuing CA" in text and "Node does not" in text
    assert text.count("SSL_CERT_FILE") == 1
    untrusted = trust_store.brief(TrustFinding(HOST, 443, UNTRUSTED, "why"), "")
    assert "--mcp-trust-ca" in untrusted


def test_an_agent_prompt_is_unchanged_for_a_host_that_verifies(ctx):
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    pipeline = AgenticPipeline.__new__(AgenticPipeline)
    pipeline.ctx, pipeline.settings = ctx, ctx.settings
    target = TargetOutcome(target=HOST, base_url=f"https://{HOST}")
    assert pipeline._certificate_brief(target) == ""
    store = TrustStore.for_run(ctx)
    store._findings[f"{HOST}:443"] = TrustFinding(HOST, 443, UNTRUSTED, "self-signed")
    assert "--mcp-trust-ca" in pipeline._certificate_brief(target)


# -- API descriptions ------------------------------------------------------------------


def test_a_description_is_json_or_yaml_and_must_have_operations():
    assert apispec.parse('{"openapi": "3.0.0", "paths": {"/a": {}}}')["paths"]
    assert apispec.parse('{"openapi": "3.0.0", "paths": {}}') is None
    assert apispec.parse("<html></html>") is None
    assert apispec.looks_like_description("openapi: 3.0.0\ninfo: {}\n")
    assert not apispec.looks_like_description("name: something else\n")
    if apispec.yaml_available():
        assert apispec.parse("openapi: 3.0.0\npaths:\n  /a: {}\n")["paths"]


def test_links_are_resolved_and_kept_to_the_targets_own_host(settings):
    patterns = settings.str_list("mcp.discovery.api_spec_link_patterns")
    body = 'urls: [{ url: "API-v1.yaml" }, { url: "https://petstore.example.org/v2/swagger.json" }]'
    base = f"https://{HOST}/apidocs/"
    assert apispec.links(body, base, patterns, same_host_as=base) == [f"https://{HOST}/apidocs/API-v1.yaml"]
    assert len(apispec.links(body, base, patterns)) == 2


def test_the_doc_pages_come_from_configuration(settings):
    settings.set("mcp.discovery.api_doc_pages", ["/a/", "/b/"])
    assert pathgen.api_doc_urls(HOST, settings.section("mcp.discovery")) == [
        f"https://{HOST}/a/",
        f"https://{HOST}/b/",
    ]


class FakeClient:
    def __init__(self, answers: dict[str, Response]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, bool]] = []
        self.verifying = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, headers=None, **_kwargs):
        self.asked.append((url, bool(headers)))
        return self.answers.get(url, Response(url=url, status=404))


def gather(ctx, monkeypatch, verified: dict[str, Response], unverified: dict[str, Response] | None = None):
    clients = {True: FakeClient(verified), False: FakeClient(unverified or {})}
    monkeypatch.setattr(ctx, "http", lambda verify_tls=True, **_k: clients[verify_tls])
    pipeline = TraditionalPipeline(ctx)
    target = TargetOutcome(target=HOST, base_url=f"https://{HOST}")
    stage = StageOutcome(stage=Stage.ENDPOINT)
    pipeline._gather_hints(target, stage)
    return pipeline, target, stage, clients


def html(url: str, text: str) -> Response:
    return Response(url=url, status=200, headers={"content-type": "text/html"}, text=text)


def test_a_description_named_only_by_swagger_ui_is_found(ctx, monkeypatch):
    origin = f"https://{HOST}"
    spec = '{"openapi": "3.0.0", "paths": {"/about": {"get": {}}}}'
    answers = {
        origin: html(origin, "<html></html>"),
        f"{origin}/apidocs/": html(f"{origin}/apidocs/", '<script src="./swagger-initializer.js"></script>'),
        f"{origin}/apidocs/swagger-initializer.js": Response(
            url="", status=200, text='urls: [{ url: "API-v1.json", name: "v1"}]'
        ),
        f"{origin}/apidocs/API-v1.json": Response(url="", status=200, text=spec),
    }
    _, _, stage, _ = gather(ctx, monkeypatch, answers)
    assert stage.findings["openapi_url"] == f"{origin}/apidocs/API-v1.json"
    assert stage.findings["openapi_found_via"] == f"{origin}/apidocs/"


def test_a_certificate_failure_is_not_reported_as_an_absent_host(ctx, monkeypatch):
    origin = f"https://{HOST}"
    failure = Response(url=origin, status=0, error="[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
    pipeline, target, stage, clients = gather(
        ctx, monkeypatch, {origin: failure}, {origin: html(origin, "<html>hi</html>")}
    )
    assert stage.findings["origin_tls_error"] and stage.findings["origin_status"] == 200
    assert not any("unreachable" in r for r in stage.remediation)
    assert clients[False].asked and not any(sent for _, sent in clients[False].asked)  # no credentials
    target.stages.append(stage)
    advice = " ".join(pipeline._proposals(target, target.all_findings()))
    assert "does not trust" in advice and "--mcp-trust-ca" in advice
    assert "did not respond at all" not in advice


def test_a_host_that_is_really_absent_is_still_said_to_be(ctx, monkeypatch):
    origin = f"https://{HOST}"
    refused = Response(url=origin, status=0, error="connection refused")
    pipeline, target, stage, clients = gather(ctx, monkeypatch, {origin: refused})
    assert any("unreachable" in r for r in stage.remediation) and not clients[False].asked
    target.stages.append(stage)
    advice = " ".join(pipeline._proposals(target, target.all_findings()))
    assert "did not respond at all" in advice and "--mcp-trust-ca" not in advice


# -- the command line --------------------------------------------------------------------


def parse(*argv: str):
    return cli.build_parser().parse_args(cli._normalise_argv(list(argv)))


def test_the_trust_flags_map_to_their_settings():
    overrides = cli._overrides_from_args(parse("run", "--mcp-trust-ca", "/a.pem,/b.pem", "--mcp-trust-pin", "never"))
    assert "mcp.trust.extra_ca=/a.pem,/b.pem" in overrides
    assert "mcp.trust.pin_presented_chain=never" in overrides
    assert not any(o.startswith("mcp.trust.") for o in cli._overrides_from_args(parse("run")))
    with pytest.raises(SystemExit):
        parse("run", "--mcp-trust-pin", "sometimes")
    assert set(PIN_MODES) == {"targets", "never"}


def test_the_trust_flags_are_phase_four_flags():
    by_slug = {c.enable_key or c.slug: c for c in all_phases()}
    args = parse("run", "--mcp-trust-ca", "/a.pem")
    assert any("--mcp-trust-ca" in w for w in cli.inert_phase_flags(args, [by_slug["install"]]))
    assert cli.inert_phase_flags(args, [by_slug["mcp"]]) == []


def test_an_entry_names_the_run_bundle_only_when_it_reaches_an_anchored_host(ctx, store_settings, chain, monkeypatch):
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    fake_tls(monkeypatch, [chain["leaf"], chain["inter"]])
    store = ctx.state["trust_store"] = TrustStore(store_settings)  # not a dry run: it writes the bundle
    store.examine(f"https://{HOST}")

    def entry(url: str) -> dict:
        candidate = Candidate(
            kind="stdio",
            name="x",
            transport=Transport.STDIO,
            command=["npx", "-y", "some-server"],
            env={"API_URL": url},
        )
        return McpWriter(ctx).entry_for(candidate, "x")["env"]

    # The run's bundle, under the entry's own name (own_reference_names).
    assert entry(f"https://{HOST}/api")["NODE_EXTRA_CA_CERTS"] == (f"${{X_NODE_EXTRA_CA_CERTS:-{store.bundle()}}}")
    assert store.bundle() not in entry("https://elsewhere.example.test")["NODE_EXTRA_CA_CERTS"]


def test_the_address_note_says_certificate_rather_than_no_response(ctx, monkeypatch):
    from initialize_claude_ai.mcp_auto_discovery.address import AddressResolver
    from initialize_claude_ai.mcp_auto_discovery.address import parse as parse_address

    monkeypatch.setattr(ctx, "http", lambda **_k: FakeClient({}))
    failing = {"status": 0}
    monkeypatch.setattr(
        FakeClient, "head", lambda self, url, **_k: Response(url=url, error="refused", **failing), raising=False
    )
    monkeypatch.setattr(FakeClient, "get", lambda self, url, **_k: Response(url=url, error="refused", **failing))
    address = parse_address(HOST)
    assert "no response" in AddressResolver(ctx).resolve(address).notes[-1]
    TrustStore.for_run(ctx)._findings[f"{HOST}:443"] = TrustFinding(HOST, 443, UNTRUSTED, "self-signed")
    note = AddressResolver(ctx).resolve(address).notes[-1]
    assert "certificate not trusted" in note and "self-signed" in note


def test_a_description_cut_at_the_response_limit_is_not_taken_as_whole(ctx, monkeypatch):
    origin = f"https://{HOST}"
    spec = '{"openapi": "3.0.0", "paths": {"/about": {"get": {}}}}'
    monkeypatch.setattr(FakeClient, "max_bytes", len(spec), raising=False)
    first = pathgen.api_spec_urls(HOST, ctx.settings.section("mcp.discovery"))[0]
    pipeline, target, stage, _ = gather(ctx, monkeypatch, {first: Response(url=first, status=200, text=spec)})
    assert "openapi_spec" not in stage.findings
    assert stage.findings["openapi_unparsed_url"] == first and stage.findings["openapi_truncated"]
    target.stages.append(stage)
    assert "http.max_response_bytes" in " ".join(pipeline._proposals(target, target.all_findings()))
    assert origin in first
