"""Bridge owner-loaded Review data to the local visual QA operator."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.preflight import _private
from buddy_desktop.review_service import ReviewAction, ReviewAnalysis
from buddy_desktop.services import ServiceError


@dataclass(frozen=True)
class ReviewQAResult:
    state: str
    reason: str | None
    report: Path
    observation: str | None = None


def review_qa_operator() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    script = Path(__file__).resolve().parents[3] / "scripts" / "visual_qa.py"
    return script if script.is_file() else None


def run_review_qa(
    analysis: ReviewAnalysis,
    action: ReviewAction,
    content: bytes,
    *,
    owner: str,
    profile: str,
    viewport: tuple[int, int],
    reports: Path,
    correct: bool = False,
) -> ReviewQAResult:
    script = review_qa_operator()
    if script is None:
        raise ServiceError("The local visual QA operator is unavailable in this build.")
    digest, revision = hashlib.sha256(content).hexdigest(), analysis.source_revision
    if not revision or digest != analysis.source_sha256 or action.raw is None:
        raise ServiceError(
            "Reopen this deck from Library to load its exact saved source and action."
        )
    if not any(action in slide.actions for slide in analysis.slides):
        raise ServiceError("The selected action does not belong to this analysis.")
    deck = str(analysis.deck_id)
    source_format = analysis.source_format
    slides: dict[str, dict[str, int | float]]
    if source_format == "pdf":
        manifest, _ = open_pdf(content, deck_id=deck, revision=revision)
        slides = {slide.slide_id: {} for slide in manifest.slides}
    else:
        slides = {
            slide.slide_id: dict(
                number=index,
                native_id=slide.native_slide_id,
                width=slide.width,
                height=slide.height,
            )
            for index, slide in enumerate(analysis.slides, 1)
        }
    snapshot = dict(
        owner=owner,
        deck_id=deck,
        source_format=source_format,
        source_sha256=digest,
        revision=revision,
        profile=profile,
        viewport=viewport,
        slides=slides,
        catalog={action.instance_id: action.raw},
    )
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(reports)
    owner_reports = reports / hashlib.sha256(owner.encode()).hexdigest()
    owner_reports.mkdir(exist_ok=True, mode=0o700)
    output = owner_reports / uuid4().hex
    with TemporaryDirectory(prefix="buddy-review-qa-") as temporary:
        inputs = Path(temporary)
        _private(inputs)
        source = inputs / f"source.{source_format}"
        source.write_bytes(content)
        spec = inputs / "snapshot.json"
        spec.write_text(json.dumps(snapshot, allow_nan=False), encoding="utf-8")
        environment = dict(os.environ)
        environment.setdefault("AWS_PROFILE", "buddy-staging-tools")
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--snapshot",
                    str(spec),
                    "--source",
                    str(source),
                    "--action",
                    action.instance_id,
                    "--output",
                    str(output),
                    "--region",
                    "eu-central-1",
                    "--model",
                    "eu.anthropic.claude-sonnet-4-6",
                    *(["--correct"] if correct else []),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=450 if correct else 165,
                check=False,
            )
            report_path = output / "report.json"
            report = json.loads(report_path.read_bytes())
            state, reason = report["state"], report["reason"]
            observation = (report.get("model_verdict") or {}).get("observation")
            if not isinstance(observation, str):
                observation = None
            if state not in {"validated", "rejected", "unavailable"} or (
                reason is not None and not isinstance(reason, str)
            ):
                raise ValueError("Unexpected report")
        except (OSError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
            raise ServiceError("The local visual QA operator could not finish.") from exc
    return ReviewQAResult(state, reason, report_path, observation)
