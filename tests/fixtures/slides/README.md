# Approved extraction fixtures

Authoritative sample source, designated by the requester for issue #9:
[sample slides on Google Drive](https://drive.google.com/drive/folders/1S4IdPr4_glAhRixBJ8a6hlneX49TUxhb).
The four full sample decks stay in Drive, not in the repository. Their proposed addition
in PR #81 was closed; PR #82's review confirmed Drive-only storage. `golden.json` pins
the exact local test bytes, counts and text hashes without distributing the binaries.
All four files were downloaded from that folder and verified during implementation on
2026-10-15. CI never downloads from Drive and needs no Drive connection or credentials.

| File | Drive file ID |
| --- | --- |
| Buddy_60_Second_Pitch.pptx | 1L9yxSARdw5SvTL5MYWWMWm1_wqp1CTEY |
| Dickinson_Sample_Slides.pptx | 1GP6EY4MZuU1MSH53HOAxWbAxwYxm6ZVG |
| SustainABLE_Orientation_presentation.pptx | 1s2dobAWHRivnILUAbdc4HXgM4my1Hjth |
| WWF_Wildlife_and_Climate_Change.pptx | 1OXHQHSz1fBya2f6f4N8reWDNFtcn2bDk |

These samples can be used locally for this project's tests, but are not asserted to be
public-domain or licensed for redistribution. Do not commit them or unapproved private
presentations. Golden records pin source SHA-256, slide/object counts and extracted
text SHA-256; tests independently compare text, slide IDs, dimensions and notes using
python-pptx. Regenerate expectations only after inspecting and reviewing a fixture change.

## Optional Local Corpus

Download the original PPTX bytes from the folder into an ignored directory such as
`build/external-slides/`. Preserve the filenames in the table. Do not export PDF/images
or use a changed presentation. Then explicitly opt in:

```powershell
$env:BUDDY_PPTX_FIXTURE_DIR = (Resolve-Path build/external-slides).Path
python -m pytest backend/tests/extraction
Remove-Item Env:BUDDY_PPTX_FIXTURE_DIR
```

```sh
BUDDY_PPTX_FIXTURE_DIR="$PWD/build/external-slides" python -m pytest backend/tests/extraction
```

Without this variable the four optional corpus cases skip, even if local copies exist;
all deterministic controlled/adversarial tests still run. With the variable set, missing
files or mismatched pinned SHA-256 fail before extraction instead of silently skipping.
`golden.json` is the manifest for manual download verification. Never commit credentials,
downloaded decks or extracted private content. The source folder is not an approved CI
artifact service; introducing one requires a separate review and permission decision.

`Buddy_Extraction_Geometry.pptx` is the checked-in Buddy-authored, content-safe controlled deck.
It adds portrait geometry, known rotation/flip/group transforms, chart caches, a table,
an image, speaker notes, dense text and a slide without notes. Rebuild it with
`python -m backend.tests.extraction.make_fixture`; ZIP timestamps may change its bytes,
so source digests/slide IDs are deliberately not hardcoded for this generated fixture.
No external PowerPoint application or paid provider is needed to run extraction tests.
