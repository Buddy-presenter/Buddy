from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest


@pytest.fixture
def backend() -> Iterator[ThreadingHTTPServer]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.entered.set()
            self.server.release.wait(3)
            self.send_response(self.server.response_code)
            self.send_header("Content-Type", self.server.content_type)
            self.end_headers()
            # Timeout tests deliberately close the client before a response.
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.wfile.write(self.server.body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.response_code = 200
    server.content_type = "application/json"
    server.body = b'{"status":"ok"}'
    server.entered = Event()
    server.release = Event()
    server.release.set()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
