"""Actual Qt -> loopback HTTP -> authenticated API -> migrated PostgreSQL."""

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.database import database_settings as database_settings
from support.database import migrate

from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import ProcessingJob
from buddy_api.persistence.repositories import Repositories
from buddy_desktop.auth import Tokens
from buddy_identity.config import IdentityConfig, SessionExpired


@pytest.mark.postgres
@pytest.mark.parametrize("size", [(800, 600), (1120, 760)])
def test_end_to_end_library_and_native_layout(
    database_settings, desktop_window, monkeypatch, qtbot, qapp, size
):
    async def seed():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            async with db.sessions() as session, session.begin():
                repos = Repositories.for_session(session)
                owner = await repos.users.create("owner")
                other = await repos.users.create("other")
                await repos.decks.create(owner_id=other.id, title="Private other account deck")
                for title, status in [
                    ("Quarterly results", "running"),
                    ("Product roadmap", "failed"),
                    ("Research notes " + "W" * 220, "pending"),
                ]:
                    deck = await repos.decks.create(owner_id=owner.id, title=title)
                    deck.created_at = datetime.now(UTC)
                    job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
                    job.status = status
                    job.attempts = 1
        finally:
            await db.close()

    asyncio.run(seed())

    def verify(self, token):
        if token != "test-token":
            raise SessionExpired("invalid")
        return "owner"

    monkeypatch.setattr(CognitoVerifier, "verify", verify)
    identity = IdentityConfig(
        "eu-central-1",
        "eu-central-1_test",
        "client123",
        "https://buddy.auth.eu-central-1.amazoncognito.com",
    )
    settings = Settings(
        Environment.TEST,
        ("testserver",),
        database=database_settings,
        identity=identity,
    )
    old_style = qapp.styleSheet()
    qapp.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))
    with TestClient(create_app(settings)) as client:

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                result = client.get(
                    self.path,
                    headers={"Authorization": self.headers.get("Authorization", "")},
                )
                self.send_response(result.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(result.content)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        window = None
        try:
            window = desktop_window(f"http://127.0.0.1:{server.server_port}", request_timeout=5)
            window.resize(*size)
            window.account.session = SimpleNamespace(
                tokens=Tokens("test-token", "refresh", time.monotonic() + 3600),
                cancelled=Event(),
            )
            window.account.session_changed.emit()
            window.navigation.setCurrentRow(1)
            library = window.library
            qtbot.waitUntil(lambda: not library.runner.busy, timeout=10000)
            assert library.total == 3
            assert library.table.rowCount() == 3
            assert all("Private" not in item.title for item in library.last_page.items)
            assert library.table.viewport().width() > 0
            for control in [
                library.search,
                library.sort,
                library.refresh,
                library.table,
                library.next,
            ]:
                assert library.rect().contains(control.geometry())
            assert library.table.columnWidth(0) > 100
            qtbot.waitUntil(lambda: library.count.width() >= library.count.sizeHint().width())
            screenshots = Path("build")
            screenshots.mkdir(exist_ok=True)
            assert window.grab().save(str(screenshots / f"library-{size[0]}.png"))

            async def finish():
                db = Database(database_settings)
                try:
                    async with db.sessions() as session, session.begin():
                        job = await session.scalar(
                            select(ProcessingJob).where(ProcessingJob.status == "running")
                        )
                        job.status = "failed"
                finally:
                    await db.close()

            asyncio.run(finish())
            library.search.setText("quarterly")
            library.reload()
            qtbot.waitUntil(lambda: not library.runner.busy, timeout=10000)
            assert library.total == 1 and library.table.item(0, 1).text() == "Failed"
            assert "Contact support" in library.detail.toPlainText()
        finally:
            if window:
                window.close()
                qtbot.waitUntil(lambda: not window.library.runner.busy, timeout=10000)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            qapp.setStyleSheet(old_style)
