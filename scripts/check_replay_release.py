"""Require independent consented holdout policy/player evidence for each source format."""

import argparse
from pathlib import Path

from buddy_api.replay import Gates, Trace, load_corpus, read_document, score


def check(root: Path) -> dict[str, object]:
    reports = {}
    for source_format in ("pdf", "pptx"):
        folder = root / source_format
        corpus, digest = load_corpus(folder / "holdout.json")
        if any(session.analysis.source_format != source_format for session in corpus.sessions):
            raise ValueError("Evidence must be evaluated separately for each format")
        gates = Gates.model_validate_json(read_document(folder / "holdout-gates.json"))
        trace = Trace.model_validate_json(read_document(folder / "service-policy-trace.json"))
        report = score(corpus, digest, trace, gates, release=True)
        reports[source_format] = report
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        reports = check(args.evidence_root)
    except (ValueError, OSError, TypeError):
        # Validation errors may include private input. Keep CI logs aggregate-only.
        parser.exit(
            2, "Both formats require valid private holdout, gate and service-policy trace files.\n"
        )
    for source_format, report in reports.items():
        print(source_format, "PASS" if report["passed"] else "FAIL", report["failures"])
    return 0 if all(report["passed"] for report in reports.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
