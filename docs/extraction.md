# Bounded presentation extraction (#9, #10)

PDF extraction is also available; see [PDF presentations](pdf-presentations.md) for format
selection, page IDs, static-content limitations and the explicit PDF-to-EMU mapping.
The PPTX-specific behavior below remains unchanged.

This backend-only stage consumes a validated local PPTX path or binary stream plus an
immutable source revision. It does not fetch storage objects, run a queue, render slides,
create provider analyses, or change library processing state. The original PPTX remains
unchanged and is still the provider input; these records are internal extraction data.

## Invocation and persistence

Install `./shared` and `./backend[api,dev]` using the development guide. From the repo:

```sh
python -m buddy_api.extraction.cli tests/fixtures/slides/Buddy_Extraction_Geometry.pptx --source-revision sample-v1 --output build/extraction.json
```

The installed equivalent is `buddy-extract`. Output files are created exclusively, with
owner-only POSIX permissions, and never overwrite existing files. The JSON contains
private deck content: keep it in an access-controlled workspace (Windows ACLs apply),
not logs or telemetry. Stdout contains only counts; errors contain stable sanitized codes.
The caller owns deliberate output-file retention. Temporary source copies are cleaned
after success, parser failure and timeout. Source streams are consumed from their current
position, remain open, and must supply local bounded reads, not blocking network callbacks.

`extract_pptx(Path(...), source_revision="...")` is the synchronous production function;
offload it from GUI/event-loop threads. `extract_and_store(database, source, owner_id=...,
deck_id=..., source_revision=...)` offloads extraction and persists a `deck_extractions`
record. It verifies deck ownership before parsing and again under a row lock when saving.
Apply migration `0003_extractions` explicitly. No public HTTP extraction endpoint is added.

For an existing owned deck, the CLI can also save with `--owner-id UUID --deck-id UUID`
and `BUDDY_DATABASE_URL`/`BUDDY_DATABASE_SCHEMA` as in the database guide. These are trusted
internal operator arguments, not a client authorization mechanism. The owner/deck foreign
key is enforced by PostgreSQL. Same deck/revision/extractor retries are idempotent,
including concurrent retries; changed bytes or different results conflict rather than
overwriting a revision. Parser failures produce no extraction row. A new revision gets
a new record. Extraction is separate from a completed job or provider analysis.

## Versioned contract and coordinates

`buddy.extraction.v1` / extractor `1` includes source revision, SHA-256, ordered slides,
dimensions, text, optional notes, flat ordered objects with parent IDs, and warnings.
The standalone machine-readable contract is [extraction.schema.json](extraction.schema.json).
Regenerate/check it with `python -m buddy_api.extraction.schema --output
docs/extraction.schema.json` (add `--check` to verify without changing it).
The source content digest and revision together scope opaque UUIDv5 slide IDs, derived
from the native persistent slide ID (not the slide position). Object IDs append their
native shape ID. IDs repeat for the same bytes/revision; never infer ordering from IDs.
Revisions and digests are mandatory when relating extraction to later rendering/actions.

Coordinates use English Metric Units (914400 EMU/inch), top-left origin, x right, y down.
Each object's four corners are transformed into canonical slide space, in local
top-left/top-right/bottom-right/bottom-left order; flips can reverse winding. `bounds`
is their axis-aligned envelope, not a clipped visible-pixel box. Nested group offsets,
child-space scales, flips and clockwise rotations compose through all parent transforms.
Negative/off-slide coordinates are preserved, not silently clipped; later zoom validation
must intersect/check targets against slide bounds. Geometry can be absent only with a
`missing_geometry` warning. Invalid group transforms fail instead of fabricating child
coordinates. Placeholder geometry inherits by layout index then master semantic type.

Text follows shape/z-order and paragraph order, not a guessed human reading order.
Groups are containers with separate child records; tables contain row-major cell text;
charts expose cached labels/values and rich text, not a live Excel interpretation. Available
speaker notes include body placeholders only (not page numbers/header/footer placeholders).
`None` notes means unavailable, distinct from an existing empty notes body.

## Supported limits and fidelity

Default ceilings (callers can lower them using `Limits`, not exceed its maxima):

| Resource | Ceiling |
| --- | --- |
| Compressed source | 64 MiB |
| Declared total expanded archive | 256 MiB |
| ZIP entries | 10000 |
| Individual XML/rels part | 8 MiB |
| XML nodes parsed / nesting depth | 1000000 / 64 |
| Slides / objects across deck | 500 / 25000 |
| Extracted text characters | 2000000 |
| Serialized result | 16 MiB |
| Wall time including local copy and process startup | 30 seconds |
| Parser process memory | 512 MiB |

Parsing runs in a separate process, with Windows Job Object process-memory containment
and kill-on-close, Linux address/data limits, and POSIX CPU limits. macOS rejects the
address/data limit configuration, so its parent samples RSS every 10ms using psutil and
kills on excess or monitoring failure. This sampled guard can briefly overshoot between
checks; it is not an instantaneous allocation ceiling. Parser structure/count limits still
apply on every platform. Parent timeout kills/reaps
the parser. Failure to establish limits fails closed; caller cancellation is not a queue
cancellation API. The stage accepts only stored/deflated unencrypted ZIP members, rejects
duplicate/traversal/absolute/symlink entries, parses XML with DTD/entities/external entities
forbidden, and checks unused XML as well, including mixed-case `.xml`/`.rels` suffixes.
Embedded/active material is reported through `embedded_material_not_executed`, using
both package names and content-type/relationship metadata (including package
relationships outside the conventional embeddings folder). Upload applies its rejection
policy to that finding; extraction still never executes the content.
Node accounting includes repeat parses. No
archive member is extracted to disk; media/workbooks are never decoded/executed, external
relationships are never fetched, and bounded part reads prevent XML expansion bombs.
The parser inherits only basic OS/runtime environment entries, not database/provider
credentials. This bounded parser process is not a general-purpose OS sandbox for running
untrusted executables; it never launches embedded code or a document converter.

Supported input is transitional PresentationML, including non-16:9 slides, grouped and
rotated shapes, images, tables and cached charts. This is not a pixel-fidelity renderer:
inherited master/layout artwork, image crop semantics, custom shape outlines, SmartArt,
equations, animation, embedded objects and font metrics are not reconstructed. Relevant
warnings explicitly identify missing geometry, unsupported shapes, ignored extensions,
inherited artwork, cropped images, hidden content, cached charts and ignored external
links. A general `visual_fidelity_not_rendered` warning accompanies every result. Strict
OOXML, encrypted/legacy PowerPoint and malformed or unsupported package structures fail
with sanitized structured errors. Never treat a warning-bearing result as proof of exact
rendering or silently substitute extracted text/images for the original provider PPTX.

## Verification

```sh
python -m pytest backend/tests/extraction
python scripts/test.py api
python scripts/test.py database
python scripts/quality.py all check
```

Database checks require the disposable PostgreSQL settings in the database guide. Fixture
provenance is in `tests/fixtures/slides/README.md`; no cloud/provider calls occur in tests.
CI uses only the checked-in Buddy-authored geometry fixture for deterministic coverage;
the four full external decks remain in Drive and are not required or downloaded by CI.
For optional local golden/independent-reader checks, download them into ignored storage
and set `BUDDY_PPTX_FIXTURE_DIR` as documented in the fixture README. Configured downloads
must match pinned SHA-256 before parsing; missing or changed files fail, not skip. Without
explicit opt-in, only those four external-corpus cases skip. Exact-value tests always
cover controlled transforms/content. Adversarial tests exercise archive/XML/path/part
failures, budgets, stable IDs, source integrity, exclusive output, cleanup, owner isolation,
revision conflicts, concurrent retries and durable reads after a database restart.
