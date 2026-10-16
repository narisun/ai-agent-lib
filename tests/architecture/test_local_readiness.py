"""Readiness checks complete their HTTP exchange before closing the connection."""

import http.client
import socketserver
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools.local_processes import Service, is_ready


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", True),
        (b"HTTP/1.1 503 Starting\r\nContent-Length: 2\r\n\r\n{}", False),
        # Receiving a 200 header is not enough: broken bodies must remain unready.
        (b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}", False),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n", True),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n", False),
    ],
    ids=["ready", "starting", "truncated-body", "chunked", "truncated-chunked-body"],
)
def test_readiness_requires_a_complete_response(response: bytes, expected: bool) -> None:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(8192)
            self.request.sendall(response)

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            service = Service("test", (), Path.cwd(), server.server_address[1])
            assert is_ready(service) is expected
        finally:
            server.shutdown()
            worker.join(timeout=5)


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionResetError(), http.client.BadStatusLine("bad")]
)
def test_failed_probe_closes_connection_and_stays_unready(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = Mock()
    connection.getresponse.side_effect = error
    monkeypatch.setattr(http.client, "HTTPConnection", Mock(return_value=connection))
    assert not is_ready(Service("test", (), Path.cwd(), 8765))
    connection.close.assert_called_once()
