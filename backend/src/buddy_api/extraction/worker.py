"""Internal subprocess entry point; stdout is bounded JSON, never parser tracebacks."""

import json
import sys

from buddy_api.extraction.contracts import ExtractionError, Limits


def set_posix_limits(limits: Limits) -> None:
    if sys.platform != "win32":
        import resource

        resources = [(resource.RLIMIT_CPU, max(1, int(limits.seconds) + 1))]
        # Darwin rejects these memory rlimits; the parent enforces RSS there instead.
        if sys.platform != "darwin":
            memory = limits.memory_mb * 1024 * 1024
            resources += [(resource.RLIMIT_AS, memory), (resource.RLIMIT_DATA, memory)]
        try:
            for kind, ceiling in resources:
                _, hard = resource.getrlimit(kind)
                bound = ceiling if hard == resource.RLIM_INFINITY else min(ceiling, hard)
                resource.setrlimit(kind, (bound, hard))
        except (OSError, ValueError):
            raise ExtractionError("isolation_unavailable") from None


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(4096))
        limits = Limits.model_validate(request["limits"])
        set_posix_limits(limits)
        if request.get("source_format", "pptx") == "pdf":
            from buddy_api.extraction.pdf import read_pdf

            result = read_pdf(sys.argv[1], request["revision"], limits)
        else:
            from buddy_api.extraction.reader import read_package

            result = read_package(sys.argv[1], request["revision"], limits)
        payload = result.model_dump_json().encode()
        if len(payload) > limits.result_bytes:
            raise ExtractionError("result_limit")
        sys.stdout.buffer.write(payload)
        return 0
    except ExtractionError as exc:
        sys.stdout.write(json.dumps({"error": exc.code}))
    except MemoryError:
        sys.stdout.write('{"error":"memory_limit"}')
    except Exception:
        # Library messages can contain ZIP paths, XML text, or private slide content.
        sys.stdout.write('{"error":"invalid_package"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
