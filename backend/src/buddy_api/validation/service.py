"""Authorize PDF checks and durably publish private, owner-scoped evidence."""

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select

from buddy_api.persistence.models import ActionCheck, Deck
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService, blocking
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import (
    ArtifactReference,
    ProcessRequest,
    ProcessResult,
    StoredResult,
    ValidationRequest,
)
from buddy_api.validation.runner import run_validation

KINDS = {
    "before_image": ("png", "image/png"),
    "after_image": ("png", "image/png"),
    "report": ("json", "application/json"),
}


class ValidationService:
    def __init__(self, uploads: UploadService, settings: ValidatorSettings):
        self.uploads, self.settings = uploads, settings

    @staticmethod
    def key(row: ActionCheck, kind: str) -> str:
        return f"sources/{row.owner_id}/{row.deck_id}/validation/{row.id}/{kind}.{KINDS[kind][0]}"

    @staticmethod
    def public(row: ActionCheck) -> StoredResult:
        if row.status != "complete" or row.result is None:
            raise UploadError(
                409, "validation_pending", "Validation has not completed; retry later."
            )
        return StoredResult(
            id=row.id,
            deck_id=row.deck_id,
            result=ProcessResult.model_validate(row.result),
            evidence=[
                ArtifactReference.model_validate(
                    {k: value[k] for k in ("id", "kind", "sha256", "size_bytes")}
                )
                for value in row.artifacts
            ],
        )

    async def owned(self, owner: UUID, identifier: UUID) -> ActionCheck:
        async with self.uploads.transaction() as session:
            row = await session.scalar(
                select(ActionCheck).where(
                    ActionCheck.id == identifier, ActionCheck.owner_id == owner
                )
            )
            if row is None:
                raise UploadError(404, "not_found", "Validation not found.")
            return row

    async def read(self, owner: UUID, identifier: UUID) -> StoredResult:
        return self.public(await self.owned(owner, identifier))

    async def evidence(self, owner: UUID, identifier: UUID, artifact: UUID) -> tuple[bytes, str]:
        row = await self.owned(owner, identifier)
        if row.status != "complete":
            raise UploadError(404, "not_found", "Evidence not found.")
        value = next((item for item in row.artifacts if item["id"] == str(artifact)), None)
        if value is None:
            raise UploadError(404, "not_found", "Evidence not found.")
        mime = KINDS[value["kind"]][1]
        async with self.uploads.parser_slot:
            content = await blocking(
                lambda: self.uploads.storage.read(
                    self.key(row, value["kind"]),
                    version=value["version"],
                    size=value["size_bytes"],
                    sha256=value["sha256"],
                    source_format="pdf",
                    content_type=mime,
                    max_bytes=16 * 1024 * 1024,
                )
            )
        return content, mime

    async def validate(
        self,
        owner: UUID,
        deck_id: UUID,
        key: UUID,
        inputs: ValidationRequest,
        *,
        source_content: bytes | None = None,
    ) -> StoredResult:
        if self.uploads.parser_slot.locked():
            raise UploadError(
                429, "validation_capacity", "Validation capacity is full; retry later.", 30
            )
        async with self.uploads.parser_slot:
            async with self.uploads.transaction() as session:
                deck = await session.scalar(
                    select(Deck).where(Deck.id == deck_id, Deck.owner_id == owner)
                )
                if deck is None:
                    raise UploadError(404, "not_found", "Presentation not found.")
                revision, digest = deck.source_revision, deck.source_sha256
                if deck.source_format != "pdf" or not revision or not digest:
                    raise UploadError(
                        409, "unsupported_source", "Action validation requires a saved PDF."
                    )
                request = ProcessRequest(
                    deck_id=deck_id,
                    source_revision=revision,
                    source_sha256=digest,
                    **inputs.model_dump(),
                )
                fingerprint = hashlib.sha256(
                    json.dumps(
                        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                await self.uploads.admission_lock(session)
                row = await session.scalar(
                    select(ActionCheck)
                    .where(ActionCheck.owner_id == owner, ActionCheck.retry_key == key)
                    .with_for_update()
                )
                if row:
                    if row.request_sha256 != fingerprint:
                        raise UploadError(
                            409, "idempotency_conflict", "Retry key belongs to another validation."
                        )
                    if row.status != "failed":
                        return self.public(row)
                if (
                    await self.uploads.storage_usage(session, owner) + 32 * 1024 * 1024
                    > self.uploads.settings.quota_bytes
                ):
                    raise UploadError(
                        413,
                        "storage_quota_exceeded",
                        "Validation evidence exceeds your private storage quota.",
                    )
                if row:
                    row.status = "pending"
                    row.expires_at = datetime.now(UTC) + timedelta(minutes=5)
                else:
                    row = ActionCheck(
                        id=uuid4(),
                        owner_id=owner,
                        deck_id=deck_id,
                        retry_key=key,
                        request_sha256=fingerprint,
                        status="pending",
                        artifacts=[],
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    )
                    session.add(row)
                row.storage_bytes = 32 * 1024 * 1024
                await session.flush()
            try:
                source_key, version, size = (
                    deck.source_key,
                    deck.source_version_id,
                    deck.source_size_bytes,
                )
                if not source_key or not version or not size:
                    raise UploadError(409, "source_unavailable", "Saved source is unavailable.")
                content = (
                    source_content
                    if source_content is not None
                    else await blocking(
                        lambda: self.uploads.storage.read(
                            source_key,
                            version=version,
                            size=size,
                            sha256=digest,
                            source_format="pdf",
                        )
                    )
                )
                if (
                    len(content) != size
                    or hashlib.sha256(content).hexdigest() != digest
                    or not content.startswith(b"%PDF-")
                ):
                    raise UploadError(
                        409, "source_mismatch", "Saved source does not match its accepted revision."
                    )
                cancelled = Event()
                task = asyncio.create_task(
                    asyncio.to_thread(run_validation, content, request, self.settings, cancelled)
                )
                try:
                    bundle = await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled.set()
                    await task
                    raise
                artifacts: list[dict[str, Any]] = []
                with tempfile.TemporaryDirectory(prefix="buddy-evidence-") as directory:
                    for kind, data in bundle.artifacts.items():
                        sha = hashlib.sha256(data).hexdigest()
                        path = Path(directory) / (kind + "." + KINDS[kind][0])
                        path.write_bytes(data)
                        saved_version = await blocking(
                            partial(
                                self.uploads.storage.put,
                                self.key(row, kind),
                                path,
                                size=len(data),
                                sha256=sha,
                                revision=str(row.id),
                                content_type=KINDS[kind][1],
                            )
                        )
                        artifacts.append(
                            {
                                "id": str(uuid5(row.id, kind)),
                                "kind": kind,
                                "sha256": sha,
                                "size_bytes": len(data),
                                "version": saved_version,
                            }
                        )
                async with self.uploads.transaction() as session:
                    stored = await session.get(ActionCheck, row.id, with_for_update=True)
                    assert (
                        stored is not None
                        and stored.owner_id == owner
                        and stored.status == "pending"
                    )
                    stored.result = bundle.result.model_dump(mode="json")
                    stored.artifacts = artifacts
                    stored.storage_bytes = sum(item["size_bytes"] for item in artifacts)
                    stored.status = "complete"
                    await session.flush()
                    return self.public(stored)
            except (Exception, asyncio.CancelledError):
                async with self.uploads.transaction() as session:
                    current = await session.get(ActionCheck, row.id, with_for_update=True)
                    if current is not None and current.status != "complete":
                        current.status = "cleanup"
                await asyncio.shield(self.cleanup(row.id))
                raise

    async def cleanup(self, identifier: UUID) -> None:
        async with self.uploads.transaction() as session:
            row = await session.get(ActionCheck, identifier, with_for_update=True)
            if row is None or row.status in {"complete", "failed"}:
                return
            if row.status == "pending" and row.expires_at > datetime.now(UTC):
                return
            row.status = "cleanup"
            for kind in KINDS:
                await blocking(partial(self.uploads.storage.remove, self.key(row, kind)))
            row.status = "failed"
            row.storage_bytes = 0

    async def reconcile(self) -> None:
        async with self.uploads.transaction() as session:
            ids = list(
                await session.scalars(
                    select(ActionCheck.id)
                    .where(
                        (ActionCheck.status == "cleanup")
                        | (
                            (ActionCheck.status == "pending")
                            & (ActionCheck.expires_at <= datetime.now(UTC))
                        )
                    )
                    .limit(16)
                )
            )
        for identifier in ids:
            await self.cleanup(identifier)
