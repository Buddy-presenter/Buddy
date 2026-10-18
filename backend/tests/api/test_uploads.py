"""Real PostgreSQL/auth boundaries; injected failures are confined to S3/provider I/O."""

import asyncio
import hashlib
import io
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSessionTransaction
from starlette.requests import Request

from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.persistence.models import Deck, DeckUpload, ProcessingJob
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import PDF_TYPE, PPTX_TYPE, UploadError, UploadMetadata
from buddy_api.uploads.storage import SourceStorage
from buddy_identity.config import IdentityConfig

pytestmark = pytest.mark.postgres
presentation = Presentation()
slide = presentation.slides.add_slide(presentation.slide_layouts[6])
slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = "Buddy upload fixture"
source_stream = io.BytesIO()
presentation.save(source_stream)
SOURCE = source_stream.getvalue()


class Cloud:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.fail_put = False
        self.fail_after_put = False
        self.fail_delete = False
        self.content_types = {}

    def put_object(self, **kwargs):
        self.puts += 1
        if self.fail_put:
            raise OSError("private-storage-key-and-credentials")
        key = kwargs["Key"]
        if key in self.objects:
            raise OSError("precondition failed")
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["ServerSideEncryption"] == "aws:kms"
        assert "ACL" not in kwargs
        content = kwargs["Body"].read()
        assert len(content) == kwargs["ContentLength"]
        assert hashlib.sha256(content).hexdigest() == kwargs["Metadata"]["sha256"]
        version = str(uuid4())
        self.objects[key] = (version, content)
        self.content_types[key] = kwargs["ContentType"]
        if self.fail_after_put:
            raise OSError("acknowledgement lost")
        return {"VersionId": version}

    def get_object(self, **kwargs):
        version, content = self.objects[kwargs["Key"]]
        assert kwargs["VersionId"] == version
        return {
            "ContentLength": len(content),
            "ContentType": self.content_types[kwargs["Key"]],
            "Body": io.BytesIO(content),
        }

    def list_object_versions(self, **kwargs):
        return {
            "Versions": [
                {"Key": key, "VersionId": value[0]}
                for key, value in self.objects.items()
                if key.startswith(kwargs["Prefix"])
            ],
            "IsTruncated": False,
        }

    def delete_object(self, **kwargs):
        if self.fail_delete:
            raise OSError("private cleanup diagnostics")
        self.objects.pop(kwargs["Key"], None)
        return {}

    def head_bucket(self, **kwargs):
        return {}

    def close(self):
        pass


@pytest.fixture
def upload_app(migrated_database, monkeypatch):
    cloud = Cloud()
    subjects = {"account-a": str(uuid4()), "account-b": str(uuid4())}
    monkeypatch.setattr(
        CognitoVerifier,
        "verify",
        lambda self, token: VerifiedIdentity(subjects[token], f"{token}@example.test"),
    )
    identity = IdentityConfig(
        "eu-central-1",
        "eu-central-1_Example",
        "client123",
        "https://buddy.auth.eu-central-1.amazoncognito.com",
    )

    @contextmanager
    def launch(**overrides):
        config = UploadSettings(
            bucket="buddy-test-private",
            region="eu-central-1",
            kms_key="arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111",
            **overrides,
        )
        app = create_app(
            Settings(
                Environment.TEST,
                ("testserver",),
                database=migrated_database,
                identity=identity,
                uploads=config,
            ),
            upload_storage=SourceStorage(config, cloud),
        )
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer account-a"
            yield client, cloud, app.state.uploads

    return launch


def metadata(content=SOURCE):
    return {
        "filename": "sample.pptx",
        "title": "Private sample",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_type": PPTX_TYPE,
    }


def initialize(client, key=None, body=None):
    return client.post(
        "/api/v1/uploads", headers={"Idempotency-Key": str(key or uuid4())}, json=body or metadata()
    )


def transfer(client, identifier, content=SOURCE):
    return client.put(
        f"/api/v1/uploads/{identifier}/content",
        content=content,
        headers={"Content-Type": PPTX_TYPE},
    )


def test_pdf_upload_storage_database_private_download_and_pptx_preservation(upload_app):
    fixture = Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    content = fixture.read_bytes()
    body = {**metadata(content), "filename": "slides.pdf", "content_type": PDF_TYPE}
    with upload_app() as (client, cloud, service):
        key = uuid4()
        reserved = initialize(client, key, body)
        identifier = reserved.json()["data"]["id"]
        wrong_type = transfer(client, identifier, content)
        assert wrong_type.status_code == 415
        completed = client.put(
            f"/api/v1/uploads/{identifier}/content",
            content=content,
            headers={"Content-Type": PDF_TYPE},
        )
        assert completed.status_code == 200, completed.text
        deck = completed.json()["data"]["deck"]
        assert deck["source_format"] == "pdf" and deck["slide_count"] == 3
        storage_key = next(iter(cloud.objects))
        assert storage_key.endswith("/original.pdf")
        assert cloud.objects[storage_key][1] == content
        assert cloud.content_types[storage_key] == PDF_TYPE
        assert initialize(client, key, body).json()["data"]["deck"] == deck
        listing = client.get("/api/v1/decks").json()["data"]["items"][0]
        assert listing["can_present"] and listing["status"] == "uploaded"
        downloaded = client.get(f"/api/v1/decks/{identifier}/source")
        assert downloaded.content == content
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-source-revision"] == deck["source_revision"]
        client.headers["Authorization"] = "Bearer account-b"
        assert client.get(f"/api/v1/decks/{identifier}/source").status_code == 404
        client.headers["Authorization"] = "Bearer account-a"
        cloud.objects[storage_key] = (cloud.objects[storage_key][0], b"x" * len(content))
        assert client.get(f"/api/v1/decks/{identifier}/source").status_code == 503
        ppt = initialize(client).json()["data"]["id"]
        assert transfer(client, ppt).status_code == 200
        assert client.get(f"/api/v1/decks/{ppt}/source").status_code == 409

        async def persisted():
            async with service.transaction() as session:
                stored = await session.get(Deck, UUID(identifier))
                assert stored.source_format == "pdf"
                assert stored.source_sha256 == hashlib.sha256(content).hexdigest()
                assert await session.scalar(select(func.count()).select_from(ProcessingJob)) == 0

        client.portal.call(persisted)


def test_original_bytes_private_identity_and_complete_retry_survive_restart(upload_app):
    key = uuid4()
    with upload_app() as (client, cloud, service):
        first = initialize(client, key)
        assert first.status_code == 200, first.text
        identifier = first.json()["data"]["id"]
        completed = transfer(client, identifier)
        assert completed.status_code == 200, completed.text
        deck = completed.json()["data"]["deck"]
        assert deck["status"] == "uploaded"
        assert deck["sha256"] == hashlib.sha256(SOURCE).hexdigest()
        assert list(cloud.objects.values())[0][1] == SOURCE
        assert "sources/" not in completed.text
        assert "storage_key" not in completed.text
        assert client.get("/api/v1/decks").json()["data"]["items"][0]["status"] == "uploaded"
        assert initialize(client, key).json()["data"]["deck"]["id"] == deck["id"]
        assert transfer(client, identifier).json()["data"]["deck"]["id"] == deck["id"]
        assert cloud.puts == 1

        async def persisted():
            async with service.transaction() as session:
                stored = await session.get(Deck, deck["id"])
                assert stored.source_revision == deck["source_revision"]
                assert stored.source_size_bytes == len(SOURCE)
                assert stored.source_version_id
                assert await session.scalar(select(func.count()).select_from(ProcessingJob)) == 0

        client.portal.call(persisted)
        client.headers["Authorization"] = "Bearer account-b"
        assert client.get(f"/api/v1/uploads/{identifier}").status_code == 403
        assert transfer(client, identifier).status_code == 403
        assert client.get("/api/v1/decks").json()["data"]["total"] == 0
    with upload_app() as (client, cloud, _):
        assert initialize(client, key).json()["data"]["deck"]["id"] == deck["id"]
        assert cloud.puts == 1


def test_same_key_conflicts_but_deliberate_duplicate_is_a_new_deck(upload_app):
    with upload_app() as (client, cloud, _):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        first = transfer(client, identifier).json()["data"]["deck"]["id"]
        changed = metadata()
        changed["title"] = "Other request"
        assert initialize(client, key, changed).status_code == 409
        second_id = initialize(client).json()["data"]["id"]
        second = transfer(client, second_id).json()["data"]["deck"]["id"]
        assert second != first
        assert len(cloud.objects) == 2


@pytest.mark.parametrize("after_put", [False, True])
def test_storage_failure_cleanup_and_same_key_retry(upload_app, after_put):
    with upload_app() as (client, cloud, _):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        cloud.fail_after_put, cloud.fail_put = after_put, not after_put
        failed = transfer(client, identifier)
        assert failed.status_code == 503
        assert "private-storage" not in failed.text
        assert not cloud.objects
        assert client.get("/api/v1/decks").json()["data"]["total"] == 0
        cloud.fail_after_put = cloud.fail_put = False
        retry = initialize(client, key)
        assert retry.json()["data"]["id"] == identifier
        assert transfer(client, identifier).json()["data"]["status"] == "complete"
        assert client.get("/api/v1/decks").json()["data"]["total"] == 1


@pytest.mark.parametrize("committed", [False, True])
def test_database_commit_failure_never_deletes_an_accepted_source(
    upload_app, monkeypatch, committed
):
    original = AsyncSessionTransaction.__aexit__
    injected = False

    async def fail_commit(self, exception_type, exception, traceback):
        nonlocal injected
        completing_upload = any(
            isinstance(record, DeckUpload) and record.status == "complete"
            for record in self.session.identity_map.values()
        )
        if not injected and exception_type is None and completing_upload:
            injected = True
            if committed:
                await original(self, None, None, None)
            else:
                failure = SQLAlchemyError("private database diagnostics")
                await original(self, type(failure), failure, None)
            raise SQLAlchemyError("private commit acknowledgement diagnostics")
        return await original(self, exception_type, exception, traceback)

    # Inject at SQL transaction I/O; preserve actual service logic and PostgreSQL
    # writes, constraints and finalization in both failure scenarios.
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", fail_commit)
    with upload_app() as (client, cloud, _):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        assert transfer(client, identifier).status_code == 503
        assert injected
        assert bool(cloud.objects) is committed
        assert client.get("/api/v1/decks").json()["data"]["total"] == int(committed)
        if committed:
            assert initialize(client, key).json()["data"]["status"] == "complete"
            assert cloud.puts == 1


def test_failed_cleanup_is_durable_and_reconciled_after_restart(upload_app):
    with upload_app() as (client, cloud, _):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        cloud.fail_after_put = cloud.fail_delete = True
        assert transfer(client, identifier).status_code == 503
        assert client.get(f"/api/v1/uploads/{identifier}").json()["data"]["status"] == "cleanup"
        assert cloud.objects
        cloud.fail_after_put = cloud.fail_delete = False
    with upload_app() as (client, cloud, service):

        async def wait_for_startup_cleanup():
            # Startup reconciliation may hold the row lock already. A concurrent
            # manual pass intentionally skips it; observe the durable result of
            # the actual startup pass instead of assuming a second pass joins it.
            async with asyncio.timeout(5):
                while True:
                    async with service.transaction() as session:
                        record = await session.get(DeckUpload, UUID(identifier))
                        if record.status == "failed":
                            return
                    await asyncio.sleep(0.01)

        client.portal.call(wait_for_startup_cleanup)
        status = client.get(f"/api/v1/uploads/{identifier}").json()["data"]
        assert status["status"] == "failed" and status["retryable"]
        assert not cloud.objects
        assert initialize(client, key).json()["data"]["id"] == identifier
        assert transfer(client, identifier).status_code == 200


def test_owner_quota_counts_completed_sources_and_reserved_bytes(upload_app):
    with upload_app(quota_bytes=len(SOURCE) * 2) as (client, _, _):
        identifier = initialize(client).json()["data"]["id"]
        assert transfer(client, identifier).status_code == 200
        assert initialize(client).status_code == 200
        rejected = initialize(client)
        assert rejected.status_code == 413
        assert rejected.json()["error"]["code"] == "storage_quota_exceeded"
        client.headers["Authorization"] = "Bearer account-b"
        assert initialize(client).status_code == 200


def test_concurrency_pending_duplicate_and_expired_reservations(upload_app):
    with upload_app(owner_concurrency=1) as (client, _, service):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        assert initialize(client, key).status_code == 200
        assert initialize(client).status_code == 429

        async def expire():
            async with service.transaction() as session:
                record = await session.get(DeckUpload, identifier)
                record.status = "receiving"
                record.lease_id = uuid4()
                record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await service.reconcile()

        client.portal.call(expire)
        assert client.get(f"/api/v1/uploads/{identifier}").json()["data"]["status"] == "failed"
        assert initialize(client, key).json()["data"]["status"] == "pending"


def test_checksum_size_and_malformed_file_create_no_deck(upload_app):
    with upload_app() as (client, cloud, _):
        key = uuid4()
        identifier = initialize(client, key).json()["data"]["id"]
        assert transfer(client, identifier, SOURCE[:-1]).status_code == 422
        assert initialize(client, key).json()["data"]["status"] == "pending"
        assert transfer(client, identifier, SOURCE + b"extra").status_code == 413
        bad = b"This is not OOXML"
        invalid_id = initialize(client, body=metadata(bad)).json()["data"]["id"]
        response = transfer(client, invalid_id, bad)
        assert response.status_code == 422
        assert client.get(f"/api/v1/uploads/{invalid_id}").json()["data"]["retryable"] is False
        assert not cloud.objects
        assert client.get("/api/v1/decks").json()["data"]["total"] == 0


@pytest.mark.parametrize("bypass", ["mixed_case_xml", "embedded_package"])
def test_parser_validation_bypasses_never_reach_storage_or_create_decks(upload_app, bypass):
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(SOURCE)) as original,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as malicious,
    ):
        for item in original.infolist():
            malicious.writestr(item, original.read(item))
        if bypass == "mixed_case_xml":
            malicious.writestr("unused.RELS", b"<root>" + b"x" * (9 * 1024 * 1024) + b"</root>")
        else:
            malicious.writestr(
                "ppt/slides/_rels/unused.xml.rels",
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="attachment" '
                b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                b'relationships/package" '
                b'Target="../media/attached.xlsx"/></Relationships>',
            )
            malicious.writestr("ppt/media/attached.xlsx", b"unsupported embedded file")
    content = output.getvalue()
    with upload_app() as (client, cloud, _):
        identifier = initialize(client, body=metadata(content)).json()["data"]["id"]
        response = transfer(client, identifier, content)
        assert response.status_code == 422
        expected = "invalid_pptx" if bypass == "mixed_case_xml" else "unsupported_pptx"
        assert response.json()["error"]["code"] == expected
        status = client.get(f"/api/v1/uploads/{identifier}").json()["data"]
        assert status["status"] == "failed" and status["retryable"] is False
        assert cloud.puts == 0 and not cloud.objects
        assert client.get("/api/v1/decks").json()["data"]["total"] == 0


def test_authentication_owner_overrides_and_metadata_limits(upload_app):
    with upload_app() as (client, _, _):
        client.headers.pop("Authorization")
        assert initialize(client).status_code == 401
        client.headers["Authorization"] = "Bearer account-a"
        body = {**metadata(), "owner_id": str(uuid4())}
        assert initialize(client, body=body).status_code == 422
        assert (
            initialize(client, body={**metadata(), "filename": "../sample.pptx"}).status_code == 422
        )
        assert client.post("/api/v1/uploads", json=metadata()).status_code == 422
        large = client.post(
            "/api/v1/uploads",
            content=b" " * 9000,
            headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
        )
        assert large.status_code == 413


def test_concurrent_admission_and_double_transfer_claims_are_bounded(upload_app):
    with upload_app(owner_concurrency=1) as (client, cloud, service):
        owner = UUID(client.get("/api/v1/auth/me").json()["data"]["id"])

        async def race():
            results = await asyncio.gather(
                *(
                    service.initialize(owner, uuid4(), UploadMetadata.model_validate(metadata()))
                    for _ in range(4)
                ),
                return_exceptions=True,
            )
            accepted = [result for result in results if not isinstance(result, Exception)]
            assert len(accepted) == 1
            rejected = [result for result in results if isinstance(result, UploadError)]
            assert len(rejected) == 3 and all(result.status == 429 for result in rejected)
            identifier = accepted[0].id
            claims = await asyncio.gather(
                service.claim(owner, identifier),
                service.claim(owner, identifier),
                return_exceptions=True,
            )
            assert (
                sum(isinstance(result, UploadError) and result.status == 409 for result in claims)
                == 1
            )

        client.portal.call(race)
        assert not cloud.objects


def test_global_upload_capacity_applies_across_distinct_owners(upload_app):
    with upload_app(global_concurrency=1) as (client, _, _):
        assert initialize(client).status_code == 200
        client.headers["Authorization"] = "Bearer account-b"
        limited = initialize(client)
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "30"


def test_stale_transfer_cannot_complete_or_cancel_a_newer_retry(upload_app):
    with upload_app() as (client, _, service):
        key = uuid4()
        identifier = UUID(initialize(client, key).json()["data"]["id"])
        owner = UUID(client.get("/api/v1/auth/me").json()["data"]["id"])

        async def scenario():
            old = await service.claim(owner, identifier)
            await service.mark_cleanup(old, "upload_interrupted")
            await service.cleanup(identifier)
            await service.initialize(owner, key, UploadMetadata.model_validate(metadata()))
            new = await service.claim(owner, identifier)
            assert old.lease_id != new.lease_id
            with pytest.raises(UploadError) as error:
                await service.finalize(old, "late-version", 1)
            assert error.value.status == 409
            await service.mark_cleanup(old, "storage_unavailable")
            assert (await service.get(owner, identifier)).status == "receiving"

        client.portal.call(scenario)
        assert client.get("/api/v1/decks").json()["data"]["total"] == 0


@pytest.mark.parametrize("timeout", [False, True])
def test_interrupted_stream_or_deadline_leaves_retryable_durable_state(upload_app, timeout):
    with upload_app() as (client, cloud, service):
        key = uuid4()
        identifier = UUID(initialize(client, key).json()["data"]["id"])
        owner = UUID(client.get("/api/v1/auth/me").json()["data"]["id"])

        async def scenario():
            messages = iter(
                [
                    {"type": "http.request", "body": SOURCE[:128], "more_body": True},
                    {"type": "http.disconnect"},
                ]
            )

            async def receive():
                if timeout:
                    await asyncio.sleep(1)
                return next(messages)

            if timeout:
                service.settings = service.settings.model_copy(update={"seconds": 0.01})
            request = Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "headers": [(b"content-type", PPTX_TYPE.encode())],
                },
                receive,
            )
            with pytest.raises(UploadError) as error:
                await service.receive(owner, identifier, request)
            assert error.value.status == 408
            assert error.value.code == ("upload_timeout" if timeout else "upload_interrupted")

        client.portal.call(scenario)
        assert not cloud.objects
        assert client.get(f"/api/v1/uploads/{identifier}").json()["data"]["retryable"]
        assert initialize(client, key).json()["data"]["id"] == str(identifier)
