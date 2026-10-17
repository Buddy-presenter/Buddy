"""Generate a deterministic contract and conservatively reject breaking API drift."""

import argparse
import json
import subprocess
import sys
from difflib import unified_diff
from itertools import islice
from pathlib import Path
from typing import Any

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings

SCHEMA_PATH = Path("docs/api/openapi.json")
DOCUMENTATION_KEYS = {"description", "title", "summary", "examples", "example", "tags"}


def generate_schema() -> dict[str, Any]:
    # Exporting contracts does not read production secrets or start external services.
    return create_app(Settings(Environment.TEST)).openapi()


def breaking_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Allow additions to named maps, but conservatively reject existing contract changes."""
    failures: list[str] = []

    def referenced_schemas(document: dict[str, Any], key: str) -> set[str]:
        schemas = document.get("components", {}).get("schemas", {})
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                ref = value.get("$ref", "")
                if ref.startswith("#/components/schemas/"):
                    name = ref.rsplit("/", 1)[1]
                    if name not in found:
                        found.add(name)
                        walk(schemas.get(name, {}))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for operations in document.get("paths", {}).values():
            for operation in operations.values():
                if isinstance(operation, dict):
                    walk(operation.get(key, {}))
        return found

    # Widening accepted request values is compatible, but never widen response enums
    # (old clients may exhaustively match them), or schemas shared with responses.
    input_only = (
        referenced_schemas(before, "requestBody") & referenced_schemas(after, "requestBody")
    ) - (referenced_schemas(before, "responses") | referenced_schemas(after, "responses"))
    map_entries = {
        "schema_map": "schema",
        "response_map": "response",
        "media_map": "media_type",
        "header_map": "header",
    }
    metadata_contexts = {
        "schema",
        "path_item",
        "operation",
        "response",
        "media_type",
        "parameter",
        "request_body",
        "header",
    }

    def child_context(context: str, key: str) -> str:
        # Named-map keys are identifiers, even when they match annotation keywords.
        if context in map_entries:
            return map_entries[context]
        if context == "schema":
            if key in {
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            }:
                return "schema_map"
            if key in {
                "items",
                "additionalProperties",
                "contains",
                "not",
                "if",
                "then",
                "else",
                "propertyNames",
                "unevaluatedProperties",
                "unevaluatedItems",
            }:
                return "schema"
            if key in {"allOf", "anyOf", "oneOf", "prefixItems"}:
                return "schema_list"
        if context == "path_item" and key in {
            "get",
            "put",
            "post",
            "delete",
            "options",
            "head",
            "patch",
            "trace",
        }:
            return "operation"
        if context in {"path_item", "operation"} and key == "parameters":
            return "parameter_list"
        if context == "operation":
            if key == "responses":
                return "response_map"
            if key == "requestBody":
                return "request_body"
        if context in {"response", "request_body", "parameter", "header"} and key == "content":
            return "media_map"
        if context == "response" and key == "headers":
            return "header_map"
        if context in {"media_type", "parameter", "header"} and key == "schema":
            return "schema"
        # Literal default/enum values and unknown structures are compared conservatively.
        return "value"

    def compare(
        old: Any, new: Any, path: str, *, context: str, allow_additions: bool = False
    ) -> None:
        def annotation(key: str) -> bool:
            return context in metadata_contexts and (
                key in DOCUMENTATION_KEYS or key.startswith("x-")
            )

        parts = path.split("/")
        request_value = (
            len(parts) >= 4 and parts[1:3] == ["components", "schemas"] and parts[3] in input_only
        )
        if isinstance(old, dict) and isinstance(new, dict):
            if context == "schema" and request_value:
                if (
                    type(old.get("minLength")) is int
                    and type(new.get("minLength")) is int
                    and 0 <= new["minLength"] <= old["minLength"]
                ):
                    old = {**old, "minLength": new["minLength"]}
                if "const" in old and "enum" in new and old["const"] in new["enum"]:
                    old = {**old, "enum": new["enum"]}
                    del old["const"]
                elif (
                    "enum" in old
                    and "enum" in new
                    and all(value in new["enum"] for value in old["enum"])
                ):
                    old = {**old, "enum": new["enum"]}
            for key, value in old.items():
                if annotation(key):
                    continue
                if key not in new:
                    failures.append(f"Removed {path}/{key}")
                else:
                    child = child_context(context, key)
                    compare(
                        value,
                        new[key],
                        f"{path}/{key}",
                        context=child,
                        allow_additions=child in map_entries,
                    )
            if not allow_additions:
                for key in new.keys() - old.keys():
                    if not annotation(key):
                        failures.append(f"Changed contract at {path}/{key}")
        elif (
            isinstance(old, list)
            and isinstance(new, list)
            and context in {"schema_list", "parameter_list"}
            and len(old) == len(new)
        ):
            for index, (value, current) in enumerate(zip(old, new, strict=True)):
                compare(value, current, f"{path}/{index}", context=context.removesuffix("_list"))
        elif old != new:
            failures.append(f"Changed contract at {path}")

    for path, operations in before.get("paths", {}).items():
        current = after.get("paths", {}).get(path)
        if current is None:
            failures.append(f"Removed endpoint {path}")
        else:
            compare(
                operations, current, f"/paths/{path}", context="path_item", allow_additions=True
            )
    compare(
        before.get("components", {}).get("schemas", {}),
        after.get("components", {}).get("schemas", {}),
        "/components/schemas",
        context="schema_map",
        allow_additions=True,
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--compare-ref", help="Compare with a Git ref, e.g. origin/main, in CI.")
    args = parser.parse_args()
    rendered = json.dumps(generate_schema(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(
                "OpenAPI snapshot is stale. Regenerate it and review the contract change.",
                file=sys.stderr,
            )
            if args.output.is_file():
                diff = unified_diff(
                    args.output.read_text(encoding="utf-8").splitlines(),
                    rendered.splitlines(),
                    fromfile="committed contract",
                    tofile="generated contract",
                    lineterm="",
                )
                print("\n".join(islice(diff, 60)), file=sys.stderr)
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.compare_ref:
        if args.compare_ref.startswith("-"):
            parser.error("The baseline must be a Git ref, not a command option.")
        ref = subprocess.run(
            ["git", "rev-parse", "--verify", args.compare_ref], capture_output=True, text=True
        )
        if ref.returncode:
            print("Cannot resolve the baseline Git ref.", file=sys.stderr)
            return 2
        baseline = subprocess.run(
            ["git", "show", f"{ref.stdout.strip()}:{SCHEMA_PATH.as_posix()}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if baseline.returncode:
            print("Baseline has no API snapshot; this PR introduces the first contract.")
            return 0
        try:
            changes = breaking_changes(json.loads(baseline.stdout), json.loads(rendered))
        except (ValueError, TypeError):
            print("The baseline API schema is invalid.", file=sys.stderr)
            return 2
        if changes:
            print("Potentially breaking API changes require a new API version:", file=sys.stderr)
            for change in changes:
                print(f"- {change}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
