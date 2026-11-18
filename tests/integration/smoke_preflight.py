"""Opt-in native PDF + Office evidence. Uses only repository-owned synthetic fixtures."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PySide6.QtGui import QImage

from buddy_api.extraction.runner import extract_pptx
from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.powerpoint_zoom_evidence import distance
from buddy_desktop.preflight import PreflightRunner, _private


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--office-viewport", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"))
    parser.add_argument(
        "--office-action", choices=("highlight_region", "zoom_region"), default="highlight_region"
    )
    parser.add_argument(
        "--qa-snapshots",
        action="store_true",
        help="Write synthetic source and snapshot files for the visual QA command.",
    )
    parser.add_argument("--profile", choices=("wide-1x", "wide-2x"), default="wide-1x")
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
            instance_id="test-" + args.office_action,
            purpose="Emphasize the title: Clear slides. Confident delivery.",
            deck_id="preflight-test",
            render_id="pptx-" + extracted.source_sha256,
            slide_id=slide.id,
            type=args.office_action,
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
    for snapshot in snapshots:
        snapshot["profile"] = args.profile if snapshot["source_format"] == "pdf" else "wide-1x"
    if args.qa_snapshots:
        for snapshot in snapshots:
            source_format = snapshot["source_format"]
            source = snapshot["source"]
            snapshot_file = {
                key: value for key, value in snapshot.items() if key not in {"source", "catalog"}
            }
            snapshot_file.update(
                owner="synthetic-test-owner",
                source_sha256=hashlib.sha256(source).hexdigest(),
                catalog={action["instance_id"]: action for action in snapshot["catalog"].values()},
            )
            (args.output / f"source.{source_format}").write_bytes(source)
            (args.output / f"{source_format}-snapshot.json").write_text(
                json.dumps(snapshot_file, indent=2),
                encoding="utf-8",
            )
        return 0
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
            if report["state"] == "completed":
                trace = report["trace"]
                images = [
                    QImage.fromData(
                        runner.artifact("synthetic-test-owner", report["run_id"], f"{i}.png")
                    )
                    for i in range(3)
                ]
                checks = dict(
                    baseline_repeated=trace[1]["baseline"] == trace[5]["baseline"],
                    result_repeated=trace[1]["after"] == trace[5]["after"],
                    visible_change=distance(images[0], images[1]) > 0.001,
                    reset_restored=distance(images[0], images[2]) < 0.01,
                )
                report["smoke_checks"] = checks
                if not all(checks.values()):
                    report.update(state="needs-review", reason="smoke_visual_checks_failed")
            for artifact in ["report.json", *[a["name"] for a in report["artifacts"]]]:
                (destination / artifact).write_bytes(
                    json.dumps(report).encode()
                    if artifact == "report.json"
                    else runner.artifact("synthetic-test-owner", report["run_id"], artifact)
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
