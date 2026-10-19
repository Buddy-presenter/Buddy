# PDF presentations (#10)

Buddy accepts **PDF and PPTX** originals. PDF remains Buddy's in-app rendered format.
On Windows, Present can also launch a local `.pptx` in installed desktop PowerPoint and
track its slideshow. This is a separate presentation path: Buddy does not convert the
PPTX or render it in its own slide surface. The original remains unchanged. Slide
navigation from Buddy is added by #109; drawing/erase by #110; region zoom remains under
investigation in #111. Export to PDF when using Buddy's cross-platform rendered viewer.

## Desktop flow

Sign in, open Library, and choose **Upload PDF or PPTX…**. Files use the existing upload
reservation, checksums, quotas and private API transport. Select a saved PDF and choose
**Present PDF**. Availability to present is separate from processing/analysis status:
a PDF can be presented while its status is Awaiting processing. A saved PPTX explains
that its playback and processing pipeline are not supported yet.

Interrupted upload retries keep their reservation key until completion is confirmed.
Selecting the same file again after a successful upload creates a separate deck.
Routine successful checks of the same account preserve the active presentation and
full-screen state; loss of access or a change of account/session/backend clears it.

Present also opens local PDFs without an account. Previous, next, go to slide, zoom to
selection, highlight and reset use the existing six-action executor and top-left page
coordinates. Existing JSON/PNG manifests remain supported through the secondary file
filter. Highlights/camera changes are transient and never modify the source PDF.

**Full screen** (F11) opens the same live surface without application chrome, on the
window's current display. Escape/F11 returns to the regular layout. Right/Space/Page Down
advance; Left/Page Up go back; G chooses a slide; drag a region then Z zooms or H highlights;
R resets. Zoom/highlight state survives entering/exiting. Closing Buddy or signing out of
a private presentation closes full screen and restores the surface safely.

The built-in PDF engine is `pypdfium2==5.14.0`, backed by PDFium. PDF presentation does
not require Office, LibreOffice, a browser account, an external application or a system
PDF viewer. Windows and
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

## Page metadata and action coordinates

PDF inspection reads page count, order, stable IDs and dimensions for validation and
rendering. It does not extract text, text positions, shapes or speaker notes. Existing
metadata records use empty text/objects and an explicit `pdf_content_not_extracted`
warning. PPTX extraction remains unchanged.

Canonical page sizes are `ceil(points * 2)` at 144 dpi. Metadata stores these dimensions
in the existing EMU plane by multiplying by 6350; divide by 6350 for display coordinates.
Page IDs are UUIDv5 over source digest, immutable revision, format and zero-based page
index, matching the desktop. Zoom/highlight actions use this defined page coordinate
system; they do not require extracted text rectangles. Sending the original PDF to a
PDF-capable model and generating summaries/actions remains future pipeline work.

```sh
python scripts/quality.py all check
python scripts/test.py all
```

Set `BUDDY_TEST_DATABASE_URL` to a disposable `*_test` PostgreSQL database to exercise
migrations, persistence and end-to-end desktop/API upload/download tests. Automated S3
boundary tests do not require AWS credentials. A local code/build check is not evidence
of an AWS deployment: promote the reviewed image/migration through the normal release
workflow and verify a real private PDF upload/download there before claiming live support.
