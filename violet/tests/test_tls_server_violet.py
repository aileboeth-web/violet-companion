"""Violet stage 3: the HTTPS server does each TLS handshake in the connection's own thread.

With werkzeug's own HTTPS one connected-but-silent client (a browser pre-connection, a connection left over from the
certificate warning) blocked every other request: the login page showed, the Control Center lost its stylesheet and
scripts and was plain text.
"""

import datetime
import ipaddress
import socket
import ssl
import threading
import time
import urllib.request

import pytest
from flask import Flask, request

from wx_ilink_py.tls_server import make_tls_server


def _context(tmp_path):
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "violet-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


@pytest.fixture
def server(tmp_path):
    app = Flask(__name__)

    @app.get("/x")
    def x():
        return {"scheme": request.scheme, "secure": request.is_secure}

    srv = make_tls_server("127.0.0.1", 0, app, _context(tmp_path))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port, timeout=5.0):
    client = ssl.create_default_context()
    client.check_hostname, client.verify_mode = False, ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=client), urllib.request.ProxyHandler({}))
    with opener.open(f"https://127.0.0.1:{port}/x", timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def test_requests_are_https(server):
    status, body = _get(server)
    assert status == 200 and '"scheme":"https"' in body.replace(" ", "") and '"secure":true' in body.replace(" ", "")


def test_a_silent_connection_does_not_block_anyone_else(server):
    silent = [socket.create_connection(("127.0.0.1", server)) for _ in range(3)]    # connect, never handshake
    try:
        time.sleep(0.3)
        started = time.monotonic()
        for _ in range(5):
            assert _get(server, timeout=3.0)[0] == 200
        assert time.monotonic() - started < 3.0
    finally:
        for s in silent:
            s.close()


def test_plain_http_on_the_https_port_is_dropped_quietly(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"GET /x HTTP/1.1\r\nHost: a\r\n\r\n")
        try:
            data = s.recv(100)
        except (ConnectionResetError, socket.timeout):
            data = b""
    assert b"scheme" not in data
    assert _get(server)[0] == 200
