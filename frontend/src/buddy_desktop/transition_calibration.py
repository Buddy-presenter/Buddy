"""Build aggregate calibration at a preselected threshold on development data only."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from buddy_desktop.transition_policy import MODEL, VERSION, load_calibration


def build(path: Path, threshold: float, output: Path) -> None:
    if not math.isfinite(threshold) or not 0.5 <= threshold <= 1 or output.exists():
        raise ValueError
    with path.open("rb") as handle:
        data = handle.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError
    raw = json.loads(data)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"protocol", "split", "model", "source_format", "permission", "samples"}
        or (
            raw["protocol"] != "transition-calibration-input.v1"
            or raw["split"] != "development"
            or raw["model"] != MODEL
            or raw["source_format"] not in {"pdf", "pptx"}
        )
    ):
        raise ValueError
    records = raw["samples"]
    if not isinstance(records, list) or not 1 <= len(records) <= 100000:
        raise ValueError
    identifiers: set[str] = set()
    selected = []
    for row in records:
        if not isinstance(row, dict) or set(row) != {"id", "confidence", "correct"}:
            raise ValueError
        identity, confidence = row["id"], row["confidence"]
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity) <= 128
            or identity in identifiers
            or type(row["correct"]) is not bool
            or type(confidence) not in {int, float}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError
        identifiers.add(identity)
        if confidence >= threshold:
            selected.append(row["correct"])
    result = {
        "protocol": VERSION,
        "model": MODEL,
        "source_format": raw["source_format"],
        "threshold": threshold,
        "samples": len(selected),
        "successes": sum(selected),
        "development_sha256": hashlib.sha256(data).hexdigest(),
        "permission": raw["permission"],
    }
    # Validate before creating the final file; never overwrite a frozen profile.
    encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    # Reuse the live parser through a private sibling, cleaning it on every failure.
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch) / "profile.json"
        staged.write_text(encoded, encoding="utf-8")
        load_calibration(staged)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("development", type=Path)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        build(args.development, args.threshold, args.output)
    except (ValueError, TypeError, KeyError, OSError):
        parser.exit(2, "Calibration input or output is invalid; use a new development profile.\n")
    print("Aggregate calibration written. Freeze it before independent holdout evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
