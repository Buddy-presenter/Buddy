# PDF presentations (#10)

Buddy accepts **PDF and PPTX** originals. PDF is the supported in-app presentation format.
PPTX ingestion, unchanged private originals, extraction and existing analysis contracts
remain available; PPTX playback/conversion and an automatic analysis pipeline are not
implemented. Export a PPTX to PDF and upload that file to present it. The PDF is a separate
deck/revision; Buddy never overwrites or silently converts the PPTX.

## Desktop flow

Sign in, open Library, and choose **Upload PDF or PPTX…**. Files use the existing upload
reservation, checksums, quotas and private API transport. Select a saved PDF and choose
**Present PDF**. Availability to present is separate from processing/analysis status:
a PDF can be presented while its status is Awaiting processing. A saved PPTX explains
that its playback and processing pipeline are not supported yet.

Present also opens local PDFs without an account. Previous, next, go to slide, zoom to
selection, highlight and reset use the existing six-action executor and top-left page
coordinates. Existing JSON/PNG manifests remain supported through the secondary file
filter. Highlights/camera changes are transient and never modify the source PDF.

**Full screen** (F11) opens the same live surface without application chrome, on the
window's current display. Escape/F11 returns to the regular layout. Right/Space/Page Down
advance; Left/Page Up go back; G chooses a slide; drag a region then Z zooms or H highlights;
R resets. Zoom/highlight state survives entering/exiting. Closing Buddy or signing out of
a private presentation closes full screen and restores the surface safely.

The built-in engine is `pypdfium2==5.14.0`, backed by PDFium. No Office, LibreOffice,
browser account, external application or system PDF viewer is required. Windows and
macOS native libraries and their notices are collected by the desktop packager.
pypdfium2 is available under Apache-2.0/BSD-3-Clause; PDFium and bundled third-party
components have their own redistribution notices. Retain the wheel's bundled notices:
[upstream licensing](https://github.com/pypdfium2-team/pypdfium2#licensing).

## Supported content and bounds

- Static PDF page appearance, text, images and vector artwork are rendered at 144 dpi.
  Zoom currently enlarges this bounded page raster; it is not unlimited-resolution vector
  zoom. PDFium handles page crop and rotation before display. Mixed page sizes work.
- Sources are limited to 64 MiB and 500 pages (server configuration may be stricter).
  Each displayed page is at most 8192 pixels per side and 16,777,216 total pixels at
  144 dpi. Upload validation enforces the same page-size bounds as the viewer.
- Encrypted/password-protected and malformed PDFs reject with a safe message. Interactive
  forms, JavaScript, embedded files, launch actions, links, animation and multimedia are
  not executed. This is a static presenter, not an interactive PDF form application.
- Rendering runs in a separate subprocess outside the GUI thread. The parent enforces
  a 20-second deadline and samples child RSS against 512 MiB every 10 ms. Source copies,
  pixels and request files live in a disposable temporary directory and are cleaned after
  success/failure. Only the current page and source snapshot are kept in memory.
  This containment is a resource boundary, not an OS security sandbox.
- The subprocess receives an environment allowlist without AWS credentials/tokens.
  PDFs opened from Library stay in memory after transfer; signing out or changing the
  account/backend discards the private presentation and invalidates late results.

## API, S3 and database

The existing `/api/v1/uploads` contract accepts `.pdf` with `application/pdf` in addition
to PPTX. The filename, reserved MIME type, actual signature/parser, byte count and SHA-256
must agree. A retry key binds the format too. Transfer rejects a MIME mismatch before
claiming the pending reservation. No job or fabricated analysis is created by upload.

Migration `0006_pdf_sources` adds constrained `source_format` columns to `decks` and
`deck_uploads`, backfilling existing records as `pptx`. Roll out the migration before the
API. Downgrade refuses to lose format identity while PDF records exist. Do not modify
earlier migration files. Existing PPTX objects and S3 versions remain untouched.

PDF originals use `sources/<owner>/<upload>/<revision>/original.pdf` and the correct
S3 content type. PUT still uses KMS, an immutable conditional write, checksums, explicit
bucket ownership and a pinned version. Existing `sources/*` IAM permissions already cover
PDF objects; no public bucket or additional broad permission is needed.

`GET /api/v1/decks/{id}/source` returns an owner-authorized PDF with `Cache-Control:
no-store`, `nosniff`, a safe attachment name, SHA-256 and source-revision headers. It
fetches the exact persisted S3 version and verifies size, MIME and digest before returning
bytes. Missing/foreign decks return 404; PPTX presentation requests return 409. Storage
keys, bucket names, S3 version IDs and presigned URLs are not exposed. The desktop verifies
the digest again. Each response is bounded by the upload byte limit; concurrent reads
share the API's resource slot with source validation. Downloads require authentication
on every request; there is no public-link expiry because no public link is issued.

Library responses add `source_format` and `can_present`; neither field claims analysis
or server-rendered asset completion. The original PDF is stored in S3; per-page display
rasters are generated locally and are not persisted as server PNG assets. Queue workers,
provider calls, server preview caches and automatic analysis remain their own issues.

## Extraction and summaries/actions

`extract_pdf` uses the existing backend subprocess deadline/memory containment and emits
the existing extraction shape with an additive `source_format` field. `extract_and_store`
selects the parser from the owned deck's saved format. Existing summary/action storage
remains reusable; PDF support does not fabricate summaries or execute paid providers.

PDF canonical page sizes are `ceil(points * 2)` at 144 dpi. Extraction normalizes these
to EMU by multiplying by 6350, maintaining the existing extraction coordinate contract.
Text rectangles are mapped through PDFium's crop/rotation-aware page-to-device transform
into that same top-left plane. Convert EMU to display coordinates by dividing by 6350.
This is a documented page-pixel quantization, not raw PDF point geometry. Page IDs are
UUIDv5 over source digest, immutable revision, format and zero-based page index; the
desktop uses the same derivation. IDs do not imply order.

PDFs have no PowerPoint speaker notes or reliable original shape hierarchy. Extracted
objects are text rectangles, not PowerPoint shapes. Missing text emits
`text_unavailable_no_ocr`; no OCR or invented summary is substituted. Static-content and
missing-notes warnings are explicit. Same revision retries are immutable/idempotent.

```sh
python -m buddy_api.extraction.cli tests/fixtures/slides/Buddy_PDF_Geometry.pdf --source-revision pdf-v1 --output build/pdf-extraction.json
python scripts/quality.py all check
python scripts/test.py all
```

Set `BUDDY_TEST_DATABASE_URL` to a disposable `*_test` PostgreSQL database to exercise
migrations, persistence and end-to-end desktop/API upload/download tests. Automated S3
boundary tests do not require AWS credentials. A local code/build check is not evidence
of an AWS deployment: promote the reviewed image/migration through the normal release
workflow and verify a real private PDF upload/download there before claiming live support.
