"""Local extraction inspection; private content is emitted only to an explicit output file."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from buddy_api.extraction.contracts import ExtractionError
from buddy_api.extraction.runner import extract_source
from buddy_api.persistence.errors import PersistenceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-format", choices=("pptx", "pdf"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--owner-id", type=UUID)
    parser.add_argument("--deck-id", type=UUID)
    args = parser.parse_args(argv)
    if bool(args.owner_id) != bool(args.deck_id) or (not args.output and not args.deck_id):
        parser.error("Choose --output and/or both --owner-id and --deck-id.")
    try:
        result = extract_source(
            args.source,
            source_revision=args.source_revision,
            source_format=args.source_format or args.source.suffix.lower().lstrip("."),
        )
        if args.deck_id:
            from buddy_api.persistence.database import Database
            from buddy_api.persistence.settings import DatabaseSettings

            async def save() -> None:
                database = Database(DatabaseSettings.from_environment())
                try:
                    async with database.transaction() as repos:
                        await repos.extractions.save(
                            owner_id=args.owner_id, deck_id=args.deck_id, extraction=result
                        )
                finally:
                    await database.close()

            asyncio.run(save())
        if args.output:
            # Exclusive creation avoids overwriting a deck or following an existing symlink.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(result.model_dump_json(indent=2))
        print(
            json.dumps(
                {"status": "ok", "slides": len(result.slides), "warnings": len(result.warnings)}
            )
        )
        return 0
    except ExtractionError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
    except (PersistenceError, ValueError, OSError):
        print('{"error":"output_or_persistence_unavailable"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
