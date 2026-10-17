# Permissioned real slide images

These four original Buddy test presentations were authored for issue #47 and its
requester-approved highlighting extension on 2026-10-17.
All text and shapes are project-authored and may be redistributed with this repository
for development and CI. They contain no external decks, photography, personal data or
third-party presentation material. The source PPTX and the **actual PowerPoint PNG
exports** are checked in. The executor reads these PNG files, not synthesized test pixels.

`make_fixtures.ps1` records the exact authoring/export procedure (Microsoft PowerPoint
on Windows, PNG at 960x540, 720x540 and 540x720). PowerPoint and COM are fixture-generation
tools only; the production executor and CI use neither. Regeneration can change PPTX
ZIP metadata and font rasterization. Inspect the exports and deliberately update
manifest digests and viewport evidence after any regeneration; do not auto-bless changes.

`make_highlight_fixture.ps1` authors/exports the fourth 960x540 slide, `content.pptx/png`,
with a word, phrase, expression and formula. `manual-manifest.json` orders all four images;
`actions-v1.1.json` exercises all six commands, including replacing each highlight and
clearing it with Reset. The original v1.0 three-slide fixtures stay unchanged.

`manifest.json` pins the raster SHA-256 bytes and defines canonical dimensions at twice
the raster dimensions to exercise nonidentity coordinate transforms. Slide IDs contain
punctuation deliberately: they are opaque references, never paths or numeric indexes.
`actions.json` contains seven purpose-bearing instances of the five catalog templates,
including title and body zooms. `mappings.json` supplies independent numerical mapping
oracles at different viewport sizes and device ratios.

See [the contract guide](../../../../docs/slide-actions.md) for schemas, runtime rules,
before/after captures and how to reproduce them. The separate extraction samples in
`tests/fixtures/slides` retain their existing restrictions and are not used here.
