"""Owner-authorized requests and trusted publication of immutable QA evidence."""

import asyncio
import hashlib
import json
import logging
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
from buddy_api.preflight.judge import VERDICT_SCHEMA, VisualJudge
from buddy_api.preflight.runner import PreflightBundle, run_preflight
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService, blocking

logger = logging.getLogger(__name__)

RESERVATION = 32 * 1024 * 1024
DESKTOP_JUDGE_MODEL = "eu.anthropic.claude-sonnet-4-6"
# Same bar as the server's PDF visual QA: weaker verdicts never enable an action.
MIN_CONFIDENCE = 0.8
DESKTOP_LEASE = timedelta(minutes=10)
MAX_PNG = 3_750_000
DESKTOP_FAILURES = frozenset(
    {
        "powerpoint_requires_windows_office",
        "powerpoint_worker_busy_use_dedicated_session",
        "powerpoint_startup_failed",
        "powerpoint_launch_failed",
        "powerpoint_baseline_failed",
        "unsupported_display_profile",
        "desktop_capture_unavailable",
        "audience_viewport_occluded",
        "unsafe_office_content",
        "cancelled",
        "time_limit",
        "worker_failed",
        "source_mismatch",
    }
)


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
        visual_qa: Any = None,
        judge_region: str | None = None,
        judge_factory: Any = None,
    ):
        # The trusted desktop host OS; injectable so Linux CI can model that host.
        self.uploads, self.settings, self.platform = uploads, settings, platform
        # PDF: the server's headless visual QA (#135). PowerPoint: the owner's desktop
        # captures screenshots and this server judges them (desktop capture).
        self.visual_qa = visual_qa
        self.judge_region = settings.region if settings else judge_region
        self.judge_factory = judge_factory

    def server_capture_available(self) -> bool:
        return self.visual_qa is not None or (
            self.settings is not None and self.platform in {"win32", "darwin"}
        )

    def desktop_capture_available(self) -> bool:
        return self.judge_factory is not None or self.judge_region is not None

    def _judge(self) -> Any:
        if self.judge_factory is not None:
            return self.judge_factory()
        assert self.judge_region is not None
        return VisualJudge(self.judge_region, DESKTOP_JUDGE_MODEL)

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
                # PowerPoint needs the owner's desktop Office to capture; PDF is checked
                # entirely on the server.
                capture="desktop" if analysis.source_format == "pptx" else "server",
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

    async def _snapshot(
        self, owner: UUID, deck_id: UUID, context: dict[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, str, int]]:
        """Re-check the request against current saved state; build the player input."""
        async with self.uploads.transaction() as db:
            analyses = Analyses(db)
            record = await analyses.get(UUID(context["analysis_id"]), owner_id=owner)
            document = await analyses.read(record.id, owner_id=owner)
            deck = await db.get(Deck, deck_id)
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
                model_id=self.settings.model if self.settings else DESKTOP_JUDGE_MODEL,
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
        return snapshot, (source_key, source_version, source_size)

    async def _complete_server(
        self, identifier: UUID, updated: Any, context: dict[str, Any]
    ) -> None:
        """Record the server visual QA outcome on this request (no extra evidence)."""
        found = next(
            (
                a
                for s in updated.slides
                for a in s.actions
                if a.action.instance_id == context["instance_id"]
            ),
            None,
        )
        validation = found.validation if found is not None else None
        if found is not None and found.usable:
            state, reason = "validated", None
        elif found is not None and found.state == "rejected":
            state = "rejected"
            reason = (validation.reasons[0] if validation and validation.reasons else None) or (
                "visual_check_failed"
            )
        else:
            state = "unavailable"
            reason = (
                validation.reasons[0]
                if validation and validation.reasons
                else "visual_check_failed"
            )
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if row is None or row.status != "pending":
                return
            row.status, row.storage_bytes, row.artifacts = "complete", 0, []
            row.request_data = {**(row.request_data or {}), "phase": "complete", "attempt": 1}
            row.result = dict(state=state, reason=reason, corrected=False)

    async def _requeue(self, identifier: UUID) -> None:
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if row is not None and row.status == "pending":
                row.request_data = {**(row.request_data or {}), "phase": "queued"}
                row.storage_bytes = 0
                row.expires_at = datetime.now(UTC) + timedelta(hours=24)

    async def claim(self, owner: UUID, identifier: UUID) -> dict[str, Any]:
        """Hand a queued PowerPoint check to the owner's desktop to capture."""
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            context = dict((row.request_data or {}) if row else {})
            if (
                row is None
                or row.owner_id != owner
                or row.check_kind != "preflight"
                or context.get("capture") != "desktop"
            ):
                raise UploadError(404, "not_found", "Preflight check not found.")
            if row.status != "pending" or context.get("phase") not in {"queued", "running"}:
                raise UploadError(409, "check_finished", "This check already finished.")
            if not self.desktop_capture_available():
                raise UploadError(503, "preflight_unavailable", "Action checks are unavailable.")
            deck_id = row.deck_id
        snapshot, _ = await self._snapshot(owner, deck_id, context)
        async with self.uploads.transaction() as db:
            row = await db.get(ActionCheck, identifier, with_for_update=True)
            if row is None or row.status != "pending":
                raise UploadError(409, "check_finished", "This check already finished.")
            # A re-claim (for example after a crash) restarts the attempt sequence.
            row.request_data = {
                **(row.request_data or {}),
                "phase": "running",
                "attempt": 0,
                "judgements": [],
            }
            row.expires_at = datetime.now(UTC) + DESKTOP_LEASE
        return snapshot

    async def _running_desktop(self, db: AsyncSession, owner: UUID, identifier: UUID) -> Any:
        row = await db.get(ActionCheck, identifier, with_for_update=True)
        context = (row.request_data or {}) if row else {}
        if (
            row is None
            or row.owner_id != owner
            or row.check_kind != "preflight"
            or context.get("capture") != "desktop"
        ):
            raise UploadError(404, "not_found", "Preflight check not found.")
        if row.status != "pending" or context.get("phase") != "running":
            raise UploadError(409, "check_not_running", "Start this check first.")
        return row

    async def judge(
        self,
        owner: UUID,
        identifier: UUID,
        sequence: int,
        target: dict[str, Any],
        before: bytes,
        after: bytes,
    ) -> dict[str, Any] | None:
        """The server, never the desktop, decides each captured attempt."""
        for image in (before, after):
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > MAX_PNG:
                raise UploadError(422, "invalid_capture", "Send two PNG screenshots.")
        if len(json.dumps(target, allow_nan=False)) > 16384:
            raise UploadError(422, "invalid_capture", "The check target is too large.")
        async with self.uploads.transaction() as db:
            row = await self._running_desktop(db, owner, identifier)
            judgements = list((row.request_data or {}).get("judgements", []))
            if sequence != len(judgements) + 1 or sequence > 3:
                raise UploadError(409, "attempt_order", "Attempts must be sent in order.")
            row.expires_at = datetime.now(UTC) + DESKTOP_LEASE
        verdict: dict[str, Any] | None = None
        judge = self._judge()
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(judge, target, before, after), timeout=60
            )
            from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

            Draft202012Validator(VERDICT_SCHEMA).validate(raw)
            assert isinstance(raw, dict)
            verdict = raw
            if verdict["verdict"] == "validated" and verdict["confidence"] < MIN_CONFIDENCE:
                # Match the PDF path: an uncertain approval is not an approval.
                verdict = {**verdict, "verdict": "rejected", "issue": "uncertain"}
        except Exception:
            # Provider errors may echo private slide content; never log them.
            verdict = None
        finally:
            close = getattr(judge, "close", None)
            if close:
                close()
        async with self.uploads.transaction() as db:
            row = await self._running_desktop(db, owner, identifier)
            judgements = list((row.request_data or {}).get("judgements", []))
            if sequence != len(judgements) + 1:
                raise UploadError(409, "attempt_order", "Attempts must be sent in order.")
            judgements.append(
                dict(
                    sequence=sequence,
                    before=hashlib.sha256(before).hexdigest(),
                    after=hashlib.sha256(after).hexdigest(),
                    verdict=verdict,
                )
            )
            row.request_data = {
                **(row.request_data or {}),
                "judgements": judgements,
                "attempt": sequence,
            }
        return verdict

    async def complete_desktop(
        self, owner: UUID, identifier: UUID, report: dict[str, Any], artifacts: dict[str, bytes]
    ) -> PreflightReport:
        """Publish a desktop capture only if it matches the verdicts this server issued."""
        async with self.uploads.transaction() as db:
            row = await self._running_desktop(db, owner, identifier)
            judgements = list((row.request_data or {}).get("judgements", []))
        history = report.get("history")
        if not isinstance(history, list) or not 1 <= len(history) <= 3:
            raise UploadError(422, "invalid_report", "The check report is incomplete.")
        if len(history) != len(judgements):
            raise UploadError(409, "report_mismatch", "The report does not match this check.")
        for index, (entry, judged) in enumerate(zip(history, judgements, strict=True)):
            captured = {
                role: artifacts.get(f"attempt-{index}-{role}_image") for role in ("before", "after")
            }
            verdict = judged["verdict"]
            expected = "unavailable" if verdict is None else verdict["verdict"]
            if (
                not isinstance(entry, dict)
                or entry.get("version") != index
                or any(
                    data is None or hashlib.sha256(data).hexdigest() != judged[role]
                    for role, data in captured.items()
                )
                or entry.get("state") != expected
            ):
                raise UploadError(409, "report_mismatch", "The report does not match this check.")
        final = judgements[-1]["verdict"]
        decided = "unavailable" if final is None else final["verdict"]
        if report.get("state") != decided or (decided == "validated") != (
            report.get("semantic_verified") is True
        ):
            raise UploadError(409, "report_mismatch", "The report does not match this check.")
        allowed = {
            "report",
            *(f"attempt-{i}-{role}_image" for i in range(3) for role in ("before", "after")),
        }
        if set(artifacts) - allowed or sum(map(len, artifacts.values())) > 32 * 1024 * 1024:
            raise UploadError(422, "invalid_report", "Unexpected check evidence.")
        await self.publish(identifier, PreflightBundle(report, artifacts))
        return self.public(await self.owned(owner, identifier))

    async def fail_desktop(self, owner: UUID, identifier: UUID, reason: str) -> PreflightReport:
        async with self.uploads.transaction() as db:
            await self._running_desktop(db, owner, identifier)
        await self.fail(
            identifier, reason if reason in DESKTOP_FAILURES else "desktop_check_failed"
        )
        return self.public(await self.owned(owner, identifier))

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
        # The server visual QA (#135) takes the execution slot itself; holding it
        # here would make every PDF check fail as "capacity full".
        slot = nullcontext() if self.visual_qa is not None else self.uploads.parser_slot
        async with slot:
            async with self.uploads.transaction() as db:
                row = await db.scalar(
                    select(ActionCheck)
                    .where(
                        ActionCheck.check_kind == "preflight",
                        ActionCheck.status == "pending",
                        ActionCheck.request_data["phase"].as_string() == "queued",
                        # Desktop-capture checks wait for the owner's Buddy to claim them.
                        func.coalesce(ActionCheck.request_data["capture"].as_string(), "server")
                        == "server",
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
                snapshot, (source_key, source_version, source_size) = await self._snapshot(
                    owner, row.deck_id, context
                )
                if self.visual_qa is not None and snapshot["source_format"] == "pdf":
                    # PDFs are verified by the server's headless visual QA (#135).
                    updated = await self.visual_qa.verify(
                        owner, UUID(context["analysis_id"]), context["instance_id"], identifier
                    )
                    await self._complete_server(identifier, updated, context)
                    return True
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
            except UploadError as exc:
                # Capacity/stale conditions are transient for the server checker: requeue.
                if exc.status == 429 and self.visual_qa is not None:
                    await self._requeue(identifier)
                    return False
                await self.fail(identifier, "preflight_failed")
            except Exception as exc:
                # Private parser/provider exceptions must never become user-facing logs;
                # the exception type alone helps operators diagnose failures.
                logger.warning("Preflight check failed: %s", type(exc).__name__)
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
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the worker alive through transient database/provider errors;
                # never log their contents (they can include private deck data).
                worked = False
            await asyncio.sleep(0.1 if worked else 2)

    async def schedule(self) -> None:
        """New PDF graphs/actions get fresh server checks; failures need explicit retry.

        PowerPoint checks are created by the owner's desktop, which captures them.
        """
        if not self.server_capture_available():
            return
        profile = self.settings.profile if self.settings else "wide-1x"
        viewport = (
            (self.settings.viewport_width, self.settings.viewport_height)
            if self.settings
            else (1280, 720)
        )
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
                            Deck.source_format == "pdf",
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
                uuid5(action.id, f"preflight:server:{profile}"),
                PreflightRequest(
                    instance_id=action.instance_id,
                    profile=profile,
                    runtime_platform="win32" if self.platform == "win32" else "darwin",
                    viewport=viewport,
                ),
                automatic=True,
            )


def digest_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
