"""Opt-in native PDF + Office evidence. Uses only repository-owned synthetic fixtures."""

import argparse
import json
import sys
from pathlib import Path

from buddy_api.extraction.runner import extract_pptx
from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.preflight import PreflightRunner, _private


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--office-viewport", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    _private(args.output)
    root = Path(__file__).resolve().parents[2]
    pdf = (root / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf").read_bytes()
    manifest, _ = open_pdf(pdf, deck_id="preflight-test", revision="test-1")
    slide_id = manifest.slides[0].slide_id
    pdf_action = dict(
        version="1.1",
        instance_id="test-zoom",
        purpose="Inspect synthetic orange block",
        deck_id="preflight-test",
        render_id=manifest.render_id,
        slide_id=slide_id,
        type="zoom_region",
        params={"region": dict(x=96, y=410, width=480, height=240)},
    )
    snapshots = [
        dict(
            source=pdf,
            source_format="pdf",
            deck_id="preflight-test",
            revision="test-1",
            slides={s.slide_id: {} for s in manifest.slides},
            catalog={"test": pdf_action},
        )
    ]
    if args.office_viewport:
        import io

        pptx = (root / "frontend/tests/fixtures/slide_actions/wide.pptx").read_bytes()
        extracted = extract_pptx(io.BytesIO(pptx), source_revision="test-1")
        slide = extracted.slides[0]
        obj = next(o for o in slide.objects if o.bounds and o.bounds.width and o.bounds.height)
        region = {key: value / 12700 for key, value in obj.bounds.model_dump().items()}
        action = dict(
            source_format="pptx",
            version="powerpoint-2",
            instance_id="test-highlight",
            purpose="Inspect synthetic object",
            deck_id="preflight-test",
            render_id="pptx-" + extracted.source_sha256,
            slide_id=slide.id,
            type="highlight_region",
            params=dict(region=region, source_shape_id=obj.source_id),
        )
        snapshots.append(
            dict(
                source=pptx,
                source_format="pptx",
                deck_id="preflight-test",
                revision="test-1",
                slides={
                    s.id: dict(
                        number=s.index + 1,
                        native_id=s.source_id,
                        width=s.width / 12700,
                        height=s.height / 12700,
                    )
                    for s in extracted.slides
                },
                catalog={"test": action},
                viewport=tuple(args.office_viewport),
            )
        )
    reports = []
    with PreflightRunner("synthetic-test-owner") as runner:
        for snapshot in snapshots:
            report = runner.run(
                "synthetic-test-owner",
                **snapshot,
                tools=[
                    {"op": "capture"},
                    {"op": "apply_action", "action_id": "test"},
                    {"op": "capture"},
                    {"op": "reset"},
                    {"op": "capture"},
                    {"op": "apply_action", "action_id": "test"},
                    {"op": "read_state"},
                ],
            )
            destination = args.output / snapshot["source_format"]
            destination.mkdir()
            for artifact in ["report.json", *[a["name"] for a in report["artifacts"]]]:
                (destination / artifact).write_bytes(
                    runner.artifact("synthetic-test-owner", report["run_id"], artifact)
                )
            reports.append(report)
            print(
                json.dumps(
                    dict(
                        format=snapshot["source_format"],
                        state=report["state"],
                        reason=report["reason"],
                    )
                ),
                flush=True,
            )
    (args.output / "summary.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if all(r["state"] == "completed" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
