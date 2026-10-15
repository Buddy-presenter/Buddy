"""Export/check the standalone internal extraction schema without changing HTTP contracts."""

import argparse
import json
from pathlib import Path

from buddy_api.extraction.contracts import Extraction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = json.dumps(Extraction.model_json_schema(), indent=2, sort_keys=True) + "\n"
    if args.check:
        return int(args.output.read_text(encoding="utf-8") != schema)
    args.output.write_text(schema, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
