# Read-only presentation review (#23)

Open Review from a saved PDF in Library. Review downloads the owner's immutable PDF source
and reads the latest saved analysis revision. It checks the analysis source revision and
SHA-256 against the downloaded PDF before showing action metadata or coordinate overlays.
If analysis is absent or stale, the PDF remains browsable and the page explains why saved
analysis details are unavailable. Partial summaries and action outcomes are shown as
persisted, including rejected and needs-review results.

The desktop client uses `GET /api/v1/analyses/by-deck/{deck_id}` to discover the latest
analysis without exposing another account's deck. The endpoint resolves the authenticated
owner's deck and the highest immutable analysis version in one repeatable-read snapshot.
`GET /api/v1/analyses/{id}` remains available for direct historical reads. Action evidence
is fetched lazily through the existing owner-scoped validation evidence endpoint and is
checksum-verified before display.

PDF pages are rendered locally by the existing isolated PDFium worker at the same canonical
144-dpi plane used by analysis and validation. Only the active page image is retained;
the downloaded PDF is bounded to 64 MiB and evidence to 16 MiB per artifact. Sign-out,
account changes and backend changes clear the source bytes, page image, analysis and
evidence references. Late worker results are discarded after a view or account change.

Review is informational. It has no action editing, action execution, speech capture or
autonomous slide movement. The separate Present workspace retains its existing manual
navigation, region controls and full-screen mode.

Automated tests use deterministic typed records and local HTTP boundaries. No model,
provider credential, AWS account or real user deck is needed for those checks. For live
acceptance, sign in to an account with a saved PDF and completed or partial analysis; the
matching source PDF must still be available to that owner.
