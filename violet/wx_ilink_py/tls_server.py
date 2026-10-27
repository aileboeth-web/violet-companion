"""The HTTPS server of the Control Center (Violet stage 3).

werkzeug's own HTTPS wraps the LISTENING socket: every TLS handshake then runs inside accept(), in the one thread that
accepts all connections. A single client that connects and never finishes its handshake -- a browser's speculative
pre-connection, a connection left behind by a certificate warning, a mobile network that drops mid-way -- blocks
every other connection until it goes away. The login page (one request, styles inline) still showed, but the Control
Center's stylesheet and scripts (ten more requests) timed out: the page was plain, unstyled text.

Here the listening socket stays plain; each accepted connection does its handshake in its own worker thread, with a
time limit, so a slow or idle client only ever holds up itself.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any

from werkzeug.serving import ThreadedWSGIServer

HANDSHAKE_SECONDS = 10.0
IDLE_SECONDS = 120.0          # a kept-alive connection with nothing to say is closed after this


class ThreadedTLSServer(ThreadedWSGIServer):
    def __init__(self, host: str, port: int, app: Any, ssl_context: ssl.SSLContext, **kwargs: Any):
        super().__init__(host, port, app, ssl_context=None, **kwargs)   # plain listening socket
        self.ssl_context = ssl_context        # werkzeug reads this: wsgi.url_scheme = "https", SSL error handling

    def finish_request(self, request: socket.socket, client_address: Any) -> None:  # type: ignore[override]
        """Runs in the connection's own thread (ThreadingMixIn)."""
        request.settimeout(HANDSHAKE_SECONDS)
        try:
            tls = self.ssl_context.wrap_socket(request, server_side=True)       # the handshake, here
        except (ssl.SSLError, OSError):
            return                            # not TLS, too slow, gone: only this connection is dropped
        tls.settimeout(IDLE_SECONDS)
        try:
            super().finish_request(tls, client_address)
        finally:
            try:
                tls.close()
            except OSError:
                pass


def make_tls_server(host: str, port: int, app: Any, ssl_context: ssl.SSLContext) -> ThreadedTLSServer:
    return ThreadedTLSServer(host, port, app, ssl_context)
