"""Internal subprocess entry point; stdout is bounded JSON, never parser tracebacks."""

import json
import sys

from buddy_api.extraction.contracts import ExtractionError, Limits


def set_posix_limits(limits: Limits) -> None:
    if sys.platform != "win32":
        import resource

        memory = limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        cpu = max(1, int(limits.seconds) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(4096))
        limits = Limits.model_validate(request["limits"])
        set_posix_limits(limits)
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
