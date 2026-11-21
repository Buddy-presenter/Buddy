"""Owner-authorized requests and trusted publication of immutable QA evidence."""

import asyncio
import hashlib
import json
import sys
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from buddy_api.analysis.contracts import (
    ContentAction,
    EvidenceReference,
    PowerPointContentAction,
    ValidationInput,
    digest,
)
from buddy_api.analysis.store import Analyses
from buddy_api.persistence.models import (
    ActionCheck,
    AnalysisVersion,
    Deck,
    ProcessingJob,
    SlideAction,
)
from buddy_api.preflight.config import PreflightSettings
from buddy_api.preflight.contracts import PreflightReport, PreflightRequest
from buddy_api.preflight.runner import PreflightBundle, run_preflight
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService, blocking

RESERVATION = 32 * 1024 * 1024


def artifact_key(row: ActionCheck, name: str) -> str:
    return f"sources/{row.owner_id}/{row.deck_id}/validation/{row.id}/{name}." + (
        "json" if name == "report" else "png"
    )


class PreflightService:
    def __init__(
        self,
        uploads: UploadService,
        settings: PreflightSettings | None,
        *,
        platform: str = sys.platform,
    ):
        # The trusted desktop host OS; injectable so Linux CI can model that host.
        self.uploads, self.settings, self.platform = uploads, settings, platform

    @staticmethod
    def public(row: ActionCheck) -> PreflightReport:
        context, result = row.request_data or {}, row.result or {}
        return PreflightReport(
            id=row.id,
            analysis_id=context["analysis_id"],
            instance_id=context["instance_id"],
            phase=context["phase"],
            attempt=context.get("attempt", 0),
            state=result.get("state", "pending"),
            reasons=[result["reason"]] if result.get("reason") else [],
            corrected=bool(result.get("corrected")),
            evidence=[
                {
                    k: v
                    for k, v in a.items()
                    if k in {"id", "kind", "sha256", "size_bytes", "attempt"}
                }
                for a in row.artifacts
            ],
        )

    async def owned(self, owner: UUID, identifier: UUID) -> ActionCheck:
        async with self.uploads.transaction() as db:
            row = await db.scalar(
                select(ActionCheck)
                .join(Deck, Deck.id == ActionCheck.deck_id)
                .where(
                    ActionCheck.id == identifier,
                    ActionCheck.owner_id == owner,
                    ActionCheck.check_kind == "preflight",
                    Deck.deletion_requested_at.is_(None),
                )
            )
            if row is None:
                raise UploadError(404, "not_found", "Preflight report not found.")
            return row

    async def evidence(self, owner: UUID, identifier: UUID, artifact: UUID) -> tuple[bytes, str]:
        await self.owned(owner, identifier)
        async with self.uploads.parser_slot:
            row = await self.owned(owner, identifier)
            value = next((a for a in row.artifacts if a["id"] == str(artifact)), None)
            if row.status != "complete" or value is None:
                raise UploadError(404, "not_found", "Preflight evidence not found.")
            name = value["name"]
            if name not in {
                "report",
                *(f"attempt-{i}-{role}_image" for i in range(3) for role in ("before", "after")),
            }:
                raise UploadError(503, "invalid_evidence", "Preflight evidence is unavailable.")
            mime = "application/json" if name == "report" else "image/png"
            data = await blocking(
                lambda: self.uploads.storage.read(
                    artifact_key(row, name),
                    version=value["version"],
                    size=value["size_bytes"],
                    sha256=value["sha256"],
                    source_format="pdf",
                    content_type=mime,
                    max_bytes=16 * 1024 * 1024,
                )
            )
            return data, mime

    async def request(
        self,
        owner: UUID,
        analysis_id: UUID,
        key: UUID,
        values: PreflightRequest,
        *,
        automatic: bool = False,
    ) -> PreflightReport:
        async with self.uploads.transaction() as db:
            analyses = Analyses(db)
            analysis = await analyses.get(analysis_id, owner_id=owner)
            current = await analyses.latest_for_deck(analysis.deck_id, owner_id=owner)
            deck = await db.get(Deck, analysis.deck_id)
            provenance = analysis.provenance or {}
            if (
                current.id != analysis.id
                or deck is None
                or (deck.source_revision, deck.source_sha256)
                != (provenance.get("source_revision"), provenance.get("source_sha256"))
            ):
                raise UploadError(
                    409, "stale_analysis", "Refresh this presentation before preflight."
                )
            document = await analyses.read(analysis_id, owner_id=owner)
            action = next(
                (
                    a
                    for s in document.slides
                    for a in s.actions
                    if a.action.instance_id == values.instance_id
                ),
                None,
            )
            if action is None:
                raise UploadError(404, "not_found", "Saved action not found.")
            context = dict(
                analysis_id=str(analysis_id),
                analysis_sha256=analysis.content_sha256,
                instance_id=values.instance_id,
                action_sha256=digest(action.action),
                source_revision=deck.source_revision,
                source_sha256=deck.source_sha256,
                **values.model_dump(exclude={"instance_id"}),
                phase="queued",
                attempt=0,
                automatic=automatic,
            )
            assert document.inputs is not None
            original = next(
                a
                for s in document.inputs.slides
                for a in s.actions
                if a.instance_id == values.instance_id
            )
            context["original_action_sha256"] = digest(original)
            fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
            await self.uploads.admission_lock(db)
            existing = await db.scalar(
                select(ActionCheck).where(
                    ActionCheck.owner_id == owner, ActionCheck.retry_key == key
                )
            )
            if existing is not None:
                if existing.check_kind != "preflight" or existing.request_sha256 != fingerprint:
                    raise UploadError(
                        409, "idempotency_conflict", "Retry key belongs to another check."
                    )
                return self.public(existing)
            pending = await db.scalar(
                select(func.count())
                .select_from(ActionCheck)
                .where(
                    ActionCheck.owner_id == owner,
                    ActionCheck.check_kind == "preflight",
                    ActionCheck.status == "pending",
                )
            )
            if pending and pending >= (24 if automatic else 32):
                raise UploadError(
                    429, "preflight_capacity", "Preflight queue is full; retry later.", 30
                )
            row = ActionCheck(
                id=uuid4(),
                owner_id=owner,
                deck_id=deck.id,
                retry_key=key,
                request_sha256=fingerprint,
                check_kind="preflight",
                request_data=context,
                status="pending",
                storage_bytes=0,
                artifacts=[],
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            db.add(row)
            await db.flush()
            return self.public(row)

    async def progress(self, identifier: UUID, attempt: int) -> None:
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if (
                row is not None
                and row.status == "pending"
                and (row.request_data or {}).get("phase") == "running"
            ):
                row.request_data = {**(row.request_data or {}), "attempt": attempt}

    async def process_one(self) -> bool:
        if self.uploads.parser_slot.locked():
            return False
        async with self.uploads.parser_slot:
            async with self.uploads.transaction() as db:
                row = await db.scalar(
                    select(ActionCheck)
                    .where(
                        ActionCheck.check_kind == "preflight",
                        ActionCheck.status == "pending",
                        ActionCheck.request_data["phase"].as_string() == "queued",
                    )
                    .order_by(
                        ActionCheck.request_data["automatic"].as_boolean().asc().nulls_first(),
                        ActionCheck.created_at,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return False
                row.request_data = {**(row.request_data or {}), "phase": "running"}
                row.expires_at = datetime.now(UTC) + timedelta(seconds=480)
                await self.uploads.admission_lock(db)
                quota_exceeded = (
                    await self.uploads.storage_usage(db, row.owner_id) + RESERVATION
                    > self.uploads.settings.quota_bytes
                )
                if not quota_exceeded:
                    row.storage_bytes = RESERVATION
                identifier, owner, context = row.id, row.owner_id, dict(row.request_data)
            cancelled = Event()
            try:
                if quota_exceeded:
                    await self.fail(identifier, "storage_quota_exceeded")
                    return True
                async with self.uploads.transaction() as db:
                    analyses = Analyses(db)
                    record = await analyses.get(UUID(context["analysis_id"]), owner_id=owner)
                    document = await analyses.read(record.id, owner_id=owner)
                    deck = await db.get(Deck, row.deck_id)
                    if (
                        deck is None
                        or deck.deletion_requested_at is not None
                        or (deck.source_revision, deck.source_sha256, record.content_sha256)
                        != (
                            context["source_revision"],
                            context["source_sha256"],
                            context["analysis_sha256"],
                        )
                    ):
                        raise UploadError(409, "stale_analysis", "The source changed.")
                    action = next(
                        a.action
                        for s in document.slides
                        for a in s.actions
                        if a.action.instance_id == context["instance_id"]
                    )
                    if digest(action) != context["action_sha256"]:
                        raise UploadError(409, "stale_action", "The saved action changed.")
                    assert document.inputs is not None
                    manifest = document.inputs.manifest.model_dump(mode="json")
                    slides = {
                        s["slide_id"]: (
                            dict(
                                number=i,
                                native_id=s["native_slide_id"],
                                width=s["source"]["width"],
                                height=s["source"]["height"],
                            )
                            if document.source_format == "pptx"
                            else {}
                        )
                        for i, s in enumerate(manifest["slides"], 1)
                    }
                    snapshot = dict(
                        owner=str(owner),
                        source_format=document.source_format,
                        source_sha256=context["source_sha256"],
                        revision=context["source_revision"],
                        deck_id=str(deck.id),
                        slides=slides,
                        action=action.model_dump(mode="json"),
                        model_id=self.settings.model if self.settings else "unconfigured",
                        profile=context["profile"],
                        correct=context["correct"],
                        viewport=context["viewport"],
                    )
                    source_key, source_version, source_size = (
                        deck.source_key,
                        deck.source_version_id,
                        deck.source_size_bytes,
                    )
                    if not source_key or not source_version or source_size is None:
                        raise UploadError(409, "source_unavailable", "Saved source is unavailable.")
                if self.settings is None or context["runtime_platform"] != self.platform:
                    bundle = PreflightBundle(
                        dict(
                            state="unavailable",
                            reason="trusted_preflight_runtime_unavailable",
                            history=[],
                        ),
                        {},
                    )
                else:
                    content = await blocking(
                        lambda: self.uploads.storage.read(
                            source_key,
                            version=source_version,
                            size=source_size,
                            sha256=context["source_sha256"],
                            source_format=snapshot["source_format"],
                        )
                    )
                    loop = asyncio.get_running_loop()

                    def progress(attempt: int) -> None:
                        asyncio.run_coroutine_threadsafe(
                            self.progress(identifier, attempt), loop
                        ).result(timeout=10)

                    task = asyncio.create_task(
                        asyncio.to_thread(
                            run_preflight, content, snapshot, self.settings, cancelled, progress
                        )
                    )
                    try:
                        bundle = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled.set()
                        await task
                        raise
                await self.publish(identifier, bundle)
            except asyncio.CancelledError:
                await self.fail(identifier, "interrupted")
                raise
            except Exception:
                # Private parser/provider exceptions must never become user-facing logs.
                await self.fail(identifier, "preflight_failed")
            return True

    async def fail(self, identifier: UUID, reason: str) -> None:
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if row is None or row.check_kind != "preflight" or row.status == "complete":
                return
            if row.status == "pending":
                row.result = dict(state="unavailable", reason=reason)
                row.request_data = {**(row.request_data or {}), "phase": "failed"}
                row.status = "cleanup"
        await self.cleanup(identifier)
        await self.publish(
            identifier, PreflightBundle(dict(state="unavailable", reason=reason, history=[]), {})
        )

    async def publish(self, identifier: UUID, bundle: PreflightBundle) -> None:
        # Fence publication against expiry cleanup across API replicas. Keep this
        # one bounded transaction locked through uploads so lost acknowledgements
        # cannot create unaccounted late objects after a cleanup has completed.
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if row is None or row.check_kind != "preflight" or row.status != "pending":
                raise ValueError("Preflight lease lost")
            await self._publish(identifier, bundle, db)

    async def _publish(
        self, identifier: UUID, bundle: PreflightBundle, publication_db: AsyncSession
    ) -> None:
        row = await self.owned_report_row(identifier)
        if row.status != "pending":
            raise ValueError("Preflight lease lost")
        report, context = dict(bundle.report), row.request_data or {}
        state = report.get("state")
        if state not in {"validated", "rejected", "unavailable"}:
            raise ValueError("Invalid trusted report")
        corrected = report.get("corrected_action") if state == "validated" else None
        runtime = report.get("preflight", {})
        if state == "validated" and (
            report.get("semantic_verified") is not True
            or runtime.get("source_unchanged") is not True
            or runtime.get("runtime_platform") != context["runtime_platform"]
            or report.get("display_profile") != context["profile"]
            or report.get("action_id") != context["instance_id"]
            or report.get("original_action_sha256") != context["action_sha256"]
            or runtime.get("source_sha256") != context["source_sha256"]
        ):
            raise ValueError("Preflight execution binding mismatch")
        artifacts: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="buddy-preflight-evidence-") as directory:
            for name, data in bundle.artifacts.items():
                if name == "report":
                    kind, attempt, mime = "report", None, "application/json"
                else:
                    names = {
                        f"attempt-{i}-{role}_image": (role + "_image", i)
                        for i in range(3)
                        for role in ("before", "after")
                    }
                    if name not in names or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("Invalid preflight artifact")
                    kind, attempt = names[name]
                    mime = "image/png"
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Evidence limit")
                sha = hashlib.sha256(data).hexdigest()
                path = Path(directory) / name
                path.write_bytes(data)
                # All seven possible keys are known before upload, including lost acknowledgements.
                version = await blocking(
                    partial(
                        self.uploads.storage.put,
                        artifact_key(row, name),
                        path,
                        size=len(data),
                        sha256=sha,
                        revision=str(row.id),
                        content_type=mime,
                    )
                )
                artifacts.append(
                    dict(
                        id=str(uuid5(row.id, name)),
                        name=name,
                        kind=kind,
                        attempt=attempt,
                        sha256=sha,
                        size_bytes=len(data),
                        version=version,
                    )
                )
        async with nullcontext(publication_db) as db:
            stored = await db.get(ActionCheck, identifier, with_for_update=True)
            if stored is None or stored.status != "pending":
                raise ValueError("Preflight lease lost")
            analyses = Analyses(db)
            analysis = await db.get(AnalysisVersion, UUID(context["analysis_id"]))
            deck = await db.get(Deck, row.deck_id)
            current = await db.scalar(
                select(AnalysisVersion.id)
                .where(
                    AnalysisVersion.deck_id == row.deck_id,
                    AnalysisVersion.owner_id == row.owner_id,
                )
                .order_by(AnalysisVersion.version.desc())
                .limit(1)
            )
            stale = (
                analysis is None
                or current != analysis.id
                or deck is None
                or deck.deletion_requested_at is not None
                or (deck.source_revision, deck.source_sha256, analysis.content_sha256)
                != (
                    context["source_revision"],
                    context["source_sha256"],
                    context["analysis_sha256"],
                )
            )
            if not stale:
                assert analysis is not None
                latest_document = await analyses.read(analysis.id, owner_id=row.owner_id)
                current_action = next(
                    (
                        a.action
                        for s in latest_document.slides
                        for a in s.actions
                        if a.action.instance_id == context["instance_id"]
                    ),
                    None,
                )
                stale = current_action is None or digest(current_action) != context["action_sha256"]
            stored.artifacts = artifacts
            stored.storage_bytes = sum(a["size_bytes"] for a in artifacts)
            stored.status = "complete"
            stored.request_data = {
                **context,
                "phase": "stale" if stale else "complete",
                "attempt": len(report.get("history", [])),
            }
            stored.result = dict(
                state="unavailable" if stale else state,
                reason="stale_analysis" if stale else report.get("reason"),
                corrected=bool(
                    corrected and digest_json(corrected) != context["original_action_sha256"]
                ),
            )
            if not stale:
                assert analysis is not None
                provenance = analysis.provenance or {}
                action_model = (
                    PowerPointContentAction if analysis.source_format == "pptx" else ContentAction
                )
                execution = action_model.model_validate(corrected) if corrected else None
                if execution is not None and digest(execution) == context["original_action_sha256"]:
                    execution = None
                if state == "validated" and (
                    not artifacts
                    or runtime.get("renderer_version") != provenance.get("renderer_version")
                ):
                    raise ValueError("Renderer/evidence mismatch")
                evidence = ValidationInput(
                    source_format="pptx" if analysis.source_format == "pptx" else "pdf",
                    runtime_version=runtime.get("office_version"),
                    runtime_platform=runtime.get("runtime_platform"),
                    validation_id=row.id,
                    analysis_id=analysis.id,
                    analysis_sha256=context["analysis_sha256"],
                    instance_id=context["instance_id"],
                    action_sha256=digest(execution)
                    if execution
                    else context["original_action_sha256"],
                    original_action_sha256=context["original_action_sha256"] if execution else None,
                    executed_action=execution,
                    validator_version="desktop-preflight-1",
                    renderer_version=provenance["renderer_version"],
                    display_profile=context["profile"],
                    validation_kind="visual",
                    semantic_verified=state == "validated",
                    state="needs-review" if state == "unavailable" else state,
                    reasons=()
                    if state == "validated"
                    else (report.get("reason") or "visual_check_failed",),
                    evidence=tuple(
                        EvidenceReference.model_validate(
                            {k: a[k] for k in ("id", "kind", "sha256")}
                        )
                        for a in artifacts
                    ),
                )
                result = await analyses.record_validation(owner_id=row.owner_id, result=evidence)
                job = await db.get(ProcessingJob, analysis.job_id, with_for_update=True)
                if job and job.status in {"analysis_ready", "analysis_partial", "succeeded"}:
                    job.status = (
                        "analysis_ready"
                        if result.publication == "published"
                        else "analysis_partial"
                    )

    async def owned_report_row(self, identifier: UUID) -> ActionCheck:
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier)
            if row is None or row.check_kind != "preflight":
                raise ValueError("Unknown trusted preflight")
            return row

    async def cleanup(self, identifier: UUID) -> None:
        names = [
            "report",
            *(f"attempt-{i}-{role}_image" for i in range(3) for role in ("before", "after")),
        ]
        async with self.uploads.transaction() as db:
            current = await db.get(ActionCheck, identifier, with_for_update=True)
            if current is None or current.check_kind != "preflight" or current.status != "cleanup":
                return
            # Keep the cleanup lease locked until all possible lost-acknowledgement
            # keys are removed. A late failure must never delete completed proof.
            for name in names:
                await blocking(partial(self.uploads.storage.remove, artifact_key(current, name)))
            current.status, current.storage_bytes, current.artifacts = "pending", 0, []

    async def reconcile(self) -> None:
        async with self.uploads.transaction() as db:
            rows = list(
                await db.scalars(
                    select(ActionCheck)
                    .where(
                        ActionCheck.check_kind == "preflight",
                        ActionCheck.status.in_(("pending", "cleanup")),
                    )
                    .limit(32)
                )
            )
        for row in rows:
            if row.status == "pending" and row.expires_at < datetime.now(UTC):
                await self.fail(row.id, "preflight_interrupted")
            if row.status == "cleanup":
                await self.cleanup(row.id)
                await self.publish(
                    row.id,
                    PreflightBundle(
                        dict(
                            state="unavailable",
                            reason=(row.result or {}).get("reason", "preflight_interrupted"),
                            history=[],
                        ),
                        {},
                    ),
                )

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile()
                worked = await self.process_one()
                await self.schedule()
            except UploadError:
                worked = False
            await asyncio.sleep(0.1 if worked else 2)

    async def schedule(self) -> None:
        """New immutable graphs/actions get fresh runs; completed failures need explicit retry."""
        if self.settings is None or self.platform not in {"win32", "darwin"}:
            return
        settings = self.settings
        newer = aliased(AnalysisVersion)
        async with self.uploads.transaction() as db:
            rows = list(
                (
                    await db.execute(
                        select(SlideAction, AnalysisVersion)
                        .join(AnalysisVersion, AnalysisVersion.id == SlideAction.analysis_id)
                        .join(Deck, Deck.id == AnalysisVersion.deck_id)
                        .join(ProcessingJob, ProcessingJob.id == AnalysisVersion.job_id)
                        .where(
                            Deck.deletion_requested_at.is_(None),
                            ProcessingJob.status.in_(
                                ("succeeded", "analysis_ready", "analysis_partial")
                            ),
                            Deck.source_sha256
                            == AnalysisVersion.provenance["source_sha256"].as_string(),
                            ~select(newer.id)
                            .where(
                                newer.deck_id == AnalysisVersion.deck_id,
                                newer.version > AnalysisVersion.version,
                            )
                            .exists(),
                            ~select(ActionCheck.id)
                            .where(
                                ActionCheck.check_kind == "preflight",
                                ActionCheck.request_data["analysis_id"].as_string()
                                == cast(AnalysisVersion.id, String),
                                ActionCheck.request_data["instance_id"].as_string()
                                == SlideAction.instance_id,
                                ActionCheck.request_data["profile"].as_string() == settings.profile,
                                ActionCheck.request_data["runtime_platform"].as_string()
                                == self.platform,
                            )
                            .exists(),
                        )
                        .order_by(AnalysisVersion.created_at, SlideAction.position)
                        .limit(8)
                    )
                ).all()
            )
        for action, analysis in rows:
            await self.request(
                analysis.owner_id,
                analysis.id,
                uuid5(action.id, f"preflight:{self.platform}:{settings.profile}"),
                PreflightRequest(
                    instance_id=action.instance_id,
                    profile=settings.profile,
                    runtime_platform="win32" if self.platform == "win32" else "darwin",
                    viewport=(settings.viewport_width, settings.viewport_height),
                ),
                automatic=True,
            )


def digest_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
