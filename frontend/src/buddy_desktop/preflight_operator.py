"""Trusted host IPC for isolated QA; this child receives no provider credentials."""

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from buddy_desktop.correction_qa import inspect_with_corrections
from buddy_desktop.preflight import PreflightRunner


def run(folder: Path) -> None:
    raw = (folder / "snapshot.json").read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Snapshot limit")
    snapshot = json.loads(raw)
    source = (folder / ("source." + snapshot["source_format"])).read_bytes()
    if (
        len(source) > 64 * 1024 * 1024
        or hashlib.sha256(source).hexdigest() != snapshot["source_sha256"]
    ):
        raise ValueError("Source mismatch")
    sequence = 0

    def judge(target: dict[str, Any], before: bytes, after: bytes) -> object:
        nonlocal sequence
        sequence += 1
        (folder / "judge-before.png").write_bytes(before)
        (folder / "judge-after.png").write_bytes(after)
        request = dict(
            sequence=sequence,
            target=target,
            before=hashlib.sha256(before).hexdigest(),
            after=hashlib.sha256(after).hexdigest(),
        )
        pending = folder / "judge-request.tmp"
        pending.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
        pending.replace(folder / "judge-request.json")
        deadline = time.monotonic() + 70
        response = folder / "judge-response.json"
        while time.monotonic() < deadline:
            if response.exists():
                content = response.read_bytes()
                if len(content) > 8192:
                    raise ValueError("Verdict limit")
                value = json.loads(content)
                if value.get("sequence") == sequence:
                    response.unlink()
                    return value["verdict"]
            time.sleep(0.05)
        raise TimeoutError("Trusted verdict unavailable")

    with PreflightRunner(snapshot["owner"]) as runner:
        inspect_with_corrections(
            runner,
            snapshot["owner"],
            source=source,
            source_format=snapshot["source_format"],
            deck_id=snapshot["deck_id"],
            revision=snapshot["revision"],
            slides=snapshot["slides"],
            action=snapshot["action"],
            model_id=snapshot["model_id"],
            judge=judge,
            output=folder / "result",
            profile=snapshot["profile"],
            viewport=tuple(snapshot["viewport"]),
            max_corrections=2 if snapshot["correct"] else 0,
        )


if __name__ == "__main__":
    if sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(1)
    run(Path(sys.argv[1]).resolve())
