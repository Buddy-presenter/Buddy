"""Qt upload -> authenticated HTTP -> PostgreSQL/S3 boundary -> Qt PDF playback."""

import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID

import pytest
from backend.tests.api.conftest import migrated_database as migrated_database
from backend.tests.api.test_uploads import upload_app as upload_app
from backend.tests.support.database import database_settings as database_settings
from PySide6.QtWidgets import QFileDialog

from buddy_api.extraction.service import extract_and_store
from buddy_desktop.auth import Tokens

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/slides/Buddy_PDF_Geometry.pdf"


@pytest.mark.postgres
def test_pdf_upload_and_open_from_library_with_real_transport(
    upload_app, desktop_window, qtbot, monkeypatch, qapp
):
    old_style = qapp.styleSheet()
    qapp.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))
    with upload_app() as (client, cloud, service):

        class Handler(BaseHTTPRequestHandler):
            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                response = client.request(
                    self.command,
                    self.path,
                    content=body,
                    headers={
                        key: value for key, value in self.headers.items() if key.lower() != "host"
                    },
                )
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(response.content)

            do_GET = do_POST = do_PUT = forward

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            window = desktop_window(f"http://127.0.0.1:{server.server_port}", request_timeout=5)
            window.account.session = SimpleNamespace(
                tokens=Tokens("account-a", "refresh", time.monotonic() + 3600), cancelled=Event()
            )
            window.account.session_changed.emit()
            window.navigation.setCurrentRow(1)
            library = window.library
            qtbot.waitUntil(lambda: not library.runner.busy, timeout=10000)
            monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(FIXTURE), ""))
            library.upload_button.click()
            qtbot.waitUntil(
                lambda: library.table.rowCount() == 1 and not library.runner.busy, timeout=15000
            )
            assert library.table.item(0, 3).text() == "PDF"
            assert library.open_button.isEnabled()
            assert cloud.puts == 1
            library.open_button.click()
            qtbot.waitUntil(lambda: window.present.executor.manifest is not None, timeout=15000)
            assert window.navigation.currentRow() == 2
            manifest = window.present.executor.manifest
            assert len(manifest.slides) == 3
            assert manifest.pdf_content == FIXTURE.read_bytes()

            # Extraction uses the same persisted revision and stable page identities.
            async def compare():
                async with service.transaction() as session:
                    from buddy_api.persistence.models import Deck

                    deck = await session.get(Deck, UUID(manifest.deck_id))
                result = await extract_and_store(
                    service.database,
                    FIXTURE,
                    owner_id=deck.owner_id,
                    deck_id=deck.id,
                    source_revision=deck.source_revision,
                )
                return result.document

            extracted = client.portal.call(compare)
            assert [s["id"] for s in extracted["slides"]] == [s.slide_id for s in manifest.slides]
            screenshots = Path("build")
            screenshots.mkdir(exist_ok=True)
            assert window.grab().save(str(screenshots / "pdf-present.png"))
            window.present.enter_fullscreen()
            assert window.present.fullscreen.isFullScreen()
            window.account.session = None
            window.account.session_changed.emit()
            assert window.present.executor.manifest is None
            assert window.present.surface.slide is None
            assert window.present.fullscreen is None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            qapp.setStyleSheet(old_style)
