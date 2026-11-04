"""A trust-store file an entry names must carry the chain to a self-signed root.

smelt.example.net's entry named ``NODE_EXTRA_CA_CERTS=…/ca-bundle.pem``. The file
held "Example CA 2023.1" and "Example CA Root", but not the "Example Trust Root"
above them. Python 3.13 and curl accept a chain that ends at any trusted
certificate, so every check this program ran passed. Node accepts only a chain
that ends at a self-signed root it holds, so ``mcp-graphql`` started, listed its
tools, and failed every request with ``fetch failed``.

The pattern, for every server: judge a file the way the runtime that reads it
will, not the way this program happens to. These tests run a real TLS server
whose chain has the same shape -- a leaf, then an intermediate issued by a root
it does not send -- so the verdicts come from a real handshake.
"""

from __future__ import annotations

import datetime
import shutil
import socket
import ssl
import sys
import threading

import pytest

from initialize_claude_ai.core import trust_store
from initialize_claude_ai.core.trust_store import TrustStore
from initialize_claude_ai.mcp_auto_discovery import trust
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

# Captured at import, before conftest replaces them with offline stand-ins for
# every other test: these tests need the real handshake.
REAL_HANDSHAKE = trust_store._handshake
REAL_PRESENTED_CHAIN = TrustStore._presented_chain

HOST = "localhost"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """A root, an intermediate it signed, and a leaf for localhost the intermediate signed."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def make(cn, issuer=None, *, ca, san=""):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        signer_name, signer_key = issuer if issuer else (name, key)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(signer_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()), False)
        )
        if ca:
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), False)
        cert = builder.sign(signer_key, hashes.SHA256())
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        return (name, key), pem, key_pem

    root_id, root, _ = make("Example Trust Root", ca=True)
    inter_id, inter, _ = make("Example Issuing CA", root_id, ca=True)
    _, leaf, leaf_key = make(HOST, inter_id, ca=False, san=HOST)

    base = tmp_path_factory.mktemp("pki")
    files = {
        "chain": base / "server-chain.pem",
        "key": base / "server.key",
        "intermediate_only": base / "intermediate-only.pem",
        "complete": base / "complete.pem",
    }
    files["chain"].write_text(leaf + inter)
    files["key"].write_text(leaf_key)
    files["intermediate_only"].write_text(inter)
    files["complete"].write_text(inter + root)
    return {k: str(v) for k, v in files.items()}


@pytest.fixture(scope="module")
def server(pki):
    """A TLS server on localhost presenting the leaf and the intermediate, not the root."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki["chain"], pki["key"])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.5)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(raw, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    listener.close()
    thread.join(timeout=2)


@pytest.fixture
def real_tls(monkeypatch, settings, pki, tmp_path):
    if sys.version_info < (3, 13) and shutil.which("openssl") is None:
        # Before 3.13 the ssl module cannot return the presented chain; the
        # program reads it with `mcp.trust.chain_command` (openssl s_client).
        pytest.skip("before Python 3.13 the presented chain is read with openssl, which is not installed")
    monkeypatch.setattr(trust_store, "_handshake", REAL_HANDSHAKE)
    monkeypatch.setattr(TrustStore, "_presented_chain", REAL_PRESENTED_CHAIN)
    settings.set("mcp.trust.store_dir", str(tmp_path / "trust"))
    settings.set("mcp.trust.bundle_candidates", [])
    settings.set("mcp.trust.bundle", pki["complete"])
    settings.set("mcp.trust.include_certifi", False)
    return settings


def store(settings) -> TrustStore:
    """A store as the run builds it (``TrustStore.for_run``): with a runner, which
    reads the presented chain before Python 3.13."""
    from initialize_claude_ai.core.proc import Runner

    return TrustStore(settings, runner=Runner(dry_run=False, default_timeout=10))


# -- the check itself --------------------------------------------------------------


def test_an_intermediate_alone_is_not_a_complete_chain(real_tls, pki, server) -> None:
    assert store(real_tls).chain_complete(HOST, server, pki["intermediate_only"]) is False


def test_a_bundle_with_the_root_is(real_tls, pki, server) -> None:
    assert store(real_tls).chain_complete(HOST, server, pki["complete"]) is True


def test_a_python_style_check_would_have_passed_it(pki, server) -> None:
    """The gap itself: what this program's own checks accepted."""
    context = ssl.create_default_context(cafile=pki["intermediate_only"])
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    if not getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0):
        pytest.skip("this Python cannot verify partial chains")
    with socket.create_connection((HOST, server), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=HOST):
            pass


@pytest.mark.parametrize("which", ["missing", "unreachable"])
def test_no_verdict_is_not_a_failure(real_tls, pki, which) -> None:
    if which == "missing":
        assert store(real_tls).chain_complete(HOST, 443, "/nonexistent/bundle.pem") is None
    else:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        assert store(real_tls).chain_complete(HOST, port, pki["complete"]) is None


def test_the_missing_root_is_named(real_tls, server) -> None:
    assert store(real_tls).missing_root(HOST, server) == "Example Trust Root"


# -- what an entry gets ---------------------------------------------------------------


def test_an_incomplete_bundle_is_replaced_by_one_that_carries_the_chain(real_tls, pki, server) -> None:
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"]}, [(HOST, server)], real_tls, store(real_tls)
    )
    assert check.replaced == {"NODE_EXTRA_CA_CERTS": pki["complete"]}
    assert "Example Trust Root" in check.notes[0]
    assert check.unfixable == []


def test_a_complete_bundle_is_left_alone(real_tls, pki, server) -> None:
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["complete"]}, [(HOST, server)], real_tls, store(real_tls)
    )
    assert check.replaced == {} and check.notes == [] and check.unfixable == []


def test_with_no_bundle_to_turn_to_a_root_requiring_variable_is_unfixable(real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"], "SSL_CERT_FILE": pki["intermediate_only"]},
        [(HOST, server)],
        real_tls,
        store(real_tls),
    )
    assert len(check.unfixable) == 1 and "NODE_EXTRA_CA_CERTS" in check.unfixable[0]
    assert "--mcp-trust-ca" in check.unfixable[0]
    assert any("SSL_CERT_FILE" in n for n in check.notes), "a lenient runtime's variable is only noted"


def test_the_check_can_be_switched_off(real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.check_named_bundles", False)
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"]}, [(HOST, server)], real_tls, store(real_tls)
    )
    assert check.replaced == {} and check.unfixable == []


def test_only_https_hosts_are_checked() -> None:
    hosts = trust.https_hosts(
        [
            "https://smelt.example.com/graphql/",
            "http://plain.example.com",
            "--server https://bz.example.com:8443/x",
            "${ENDPOINT}",
        ]
    )
    assert hosts == [("smelt.example.com", 443), ("bz.example.com", 8443)]


def graphql(port: int, bundle: str) -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="mcp-graphql",
        transport=Transport.STDIO,
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"ENDPOINT": f"https://{HOST}:{port}/graphql/", "NODE_EXTRA_CA_CERTS": bundle},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, tools=["q"], round_trip=True)
    return candidate


def test_the_written_entry_names_the_bundle_that_works(ctx, real_tls, pki, server) -> None:
    candidate = graphql(server, pki["intermediate_only"])
    entry = McpWriter(ctx).entry_for(candidate, "smelt-example")
    reference = entry["env"]["NODE_EXTRA_CA_CERTS"]
    assert reference.endswith(f":-{pki['complete']}}}") and not reference.startswith("${NODE_EXTRA_CA_CERTS"), (
        "the complete bundle, through a name no shell export replaces"
    )
    assert any("Example Trust Root" in n for n in candidate.notes)


def test_an_entry_no_bundle_can_complete_is_refused_in_the_stage_and_at_publish(ctx, real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    candidate = graphql(server, pki["intermediate_only"])
    writer = McpWriter(ctx)
    assert "self-signed root" in writer.trust_refusal(candidate)
    pipeline = TraditionalPipeline(ctx)
    assert not pipeline.registrable(candidate, TargetOutcome(target=f"https://{HOST}"))
    assert "self-signed root" in pipeline.why_not_registrable(candidate, TargetOutcome(target=f"https://{HOST}"))


def test_refusing_can_be_switched_off(ctx, real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    real_tls.set("mcp.trust.refuse_incomplete_chain", False)
    candidate = graphql(server, pki["intermediate_only"])
    assert McpWriter(ctx).trust_refusal(candidate) == ""
    assert any("--mcp-trust-ca" in n for n in candidate.notes), "written anyway, never silently"


# -- a variable that adds to its runtime's own store ----------------------------------


def test_the_runtimes_own_store_is_asked_of_the_runtime(real_tls, pki, server, tmp_path) -> None:
    """Node adds NODE_EXTRA_CA_CERTS to roots it ships; the root may be there."""
    import sys

    from initialize_claude_ai.core.proc import Runner

    root_only = tmp_path / "runtime-roots.pem"
    root_only.write_text(open(pki["complete"]).read().split("-----END CERTIFICATE-----", 1)[1].strip() + "\n")
    command = [sys.executable, "-c", f"import sys; sys.stdout.write(open({str(root_only)!r}).read())"]
    real_tls.set("mcp.trust.runtime_stores", {"NODE_EXTRA_CA_CERTS": command})
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    runtime = TrustStore(real_tls, runner=Runner(dry_run=False, default_timeout=10))

    assert runtime.runtime_store(command), "the runtime printed its store"
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"]}, [(HOST, server)], real_tls, runtime
    )
    assert check.replaced == {} and check.unfixable == [], "the runtime's own roots complete the chain"


def test_a_runtime_that_is_not_installed_contributes_nothing(real_tls) -> None:
    from initialize_claude_ai.core.proc import Runner

    runtime = TrustStore(real_tls, runner=Runner(dry_run=False, default_timeout=10))
    assert runtime.runtime_store(["initialize-claude-ai-no-such-runtime", "-e", "x"]) == ""


def test_with_no_bundle_that_carries_the_chain_nothing_is_replaced(real_tls, pki, server) -> None:
    """A replacement is only ever a file proven to carry the chain."""
    real_tls.set("mcp.trust.bundle", "/nonexistent/system-bundle.pem")
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"]}, [(HOST, server)], real_tls, store(real_tls)
    )
    assert check.replaced == {}, "nothing that serves every host was available"
