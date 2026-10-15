# Approved extraction fixtures

Authoritative sample source, designated by the requester for issue #9:
[sample slides on Google Drive](https://drive.google.com/drive/folders/1S4IdPr4_glAhRixBJ8a6hlneX49TUxhb).
The four decks were previously approved for repository fixture use in PR #81. The
Drive source is authoritative going forward; hashes below/golden.json pin the exact
tested bytes rather than silently tracking later changes to a shared folder.
All four files were downloaded from that folder and SHA-256 matched the checked-in
fixtures during implementation on 2026-10-15. No tests depend on live Drive availability.

| File | Drive file ID |
| --- | --- |
| Buddy_60_Second_Pitch.pptx | 1L9yxSARdw5SvTL5MYWWMWm1_wqp1CTEY |
| Dickinson_Sample_Slides.pptx | 1GP6EY4MZuU1MSH53HOAxWbAxwYxm6ZVG |
| SustainABLE_Orientation_presentation.pptx | 1s2dobAWHRivnILUAbdc4HXgM4my1Hjth |
| WWF_Wildlife_and_Climate_Change.pptx | 1OXHQHSz1fBya2f6f4N8reWDNFtcn2bDk |

These samples are approved for this project's tests, not asserted to be public-domain
or licensed for unrelated redistribution. Do not replace them with unapproved private
presentations. Golden records pin source SHA-256, slide/object counts and extracted
text SHA-256; tests independently compare text, slide IDs, dimensions and notes using
python-pptx. Regenerate expectations only after inspecting and reviewing a fixture change.

`Buddy_Extraction_Geometry.pptx` is a Buddy-authored, content-safe controlled test deck.
It adds portrait geometry, known rotation/flip/group transforms, chart caches, a table,
an image, speaker notes, dense text and a slide without notes. Rebuild it with
`python -m backend.tests.extraction.make_fixture`; ZIP timestamps may change its bytes,
so source digests/slide IDs are deliberately not hardcoded for this generated fixture.
No external PowerPoint application or paid provider is needed to run extraction tests.
