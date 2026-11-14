"""Opt-in real Windows/Office matrix for #128; writes only synthetic evidence.

python tests/integration/smoke_powerpoint_analysis.py
Requires both installed Buddy components, python-pptx, and no existing PowerPoint process.
"""

import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from backend.tests.support.powerpoint import manifest, output, source

from buddy_api.analysis.powerpoint import ACTION_TYPES, analysis_document
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.powerpoint import PowerPointProcessRequest, powerpoint_validation_request
from buddy_api.validation.runner import run_validation


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    evidence = root / "build/issue128/office"
    evidence.mkdir(parents=True, exist_ok=True)
    reports = []
    for width in (960, 720):
        content, extracted = source(width)
        deck_id = UUID(manifest(extracted).deck_id)
        doc = analysis_document(
            output(extracted, ACTION_TYPES),
            extracted,
            manifest(extracted),
            deck_id,
            uuid4(),
            uuid4(),
        )
        for action in doc.slides[0].actions:
            request = PowerPointProcessRequest(
                **powerpoint_validation_request(action, doc).model_dump(),
                deck_id=deck_id,
                source_revision=extracted.source_revision,
                source_sha256=extracted.source_sha256,
            )
            bundle = run_validation(
                content, request, ValidatorSettings(python=sys.executable, seconds=60)
            )
            folder = evidence / f"{width}-{action.type}"
            folder.mkdir(exist_ok=True)
            for kind, data in bundle.artifacts.items():
                (folder / (kind + (".json" if kind == "report" else ".png"))).write_bytes(data)
            report = {
                "width": width,
                "action": action.type,
                **bundle.result.model_dump(mode="json"),
            }
            reports.append(report)
            print(
                json.dumps(
                    {
                        "width": width,
                        "action": action.type,
                        "state": bundle.result.state,
                        "reasons": bundle.result.reasons,
                    }
                ),
                flush=True,
            )
            (evidence / "matrix.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
            if bundle.result.state != "validated":
                return 1
    (evidence / "matrix.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if all(report["state"] == "validated" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
