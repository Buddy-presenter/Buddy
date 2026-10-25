"""Source-grounding regressions: model boxes are hints, never stored geometry."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from buddy_api.analysis.grounding import (
    OcrWord,
    Rect,
    _ground_object_target,
    _match_ocr,
    _matches_normalized_text,
    _matches_text_words,
    _snap_object,
    _tagged_content_ids,
    ground_actions,
)
from buddy_api.analysis.provider import ModelAction, ModelOutput, ModelSlide, ModelSummary
from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.runner import extract_pdf
from buddy_api.processing.stage import _configure_child_resource_limits, execute_grounding

ROOT = Path(__file__).resolve().parents[3]
GEOMETRY_PDF = ROOT / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
SCANNED_PDF = Path(__file__).resolve().parents[1] / "fixtures/pdf/scanned-source-anchor.pdf"


def _output(extraction, actions_by_page):
    return ModelOutput(
        presentation=ModelSummary(text="A source-grounding test.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index + 1,
                text=f"Page {index + 1}.",
                reason=None,
                actions=actions_by_page.get(index, ()),
            )
            for index in range(len(extraction.slides))
        ),
    )


def _action(kind="highlight_region", quote="Buddy PDF presentation", *, hint=(0, 0, 1440, 810)):
    return ModelAction(
        type=kind,
        purpose="Focus on the quoted source text.",
        target_text=quote if kind == "highlight_region" else quote,
        x=hint[0],
        y=hint[1],
        width=hint[2],
        height=hint[3],
    )


def _load(path: Path):
    content = path.read_bytes()
    extraction = extract_pdf(BytesIO(content), source_revision="grounding-test")
    manifest = render_manifest(content, extraction, str(uuid4()))
    return content, extraction, manifest


def _two_column_pdf() -> bytes:
    """Build a deterministic WinAnsi source PDF with repeated text in two columns."""
    content = (
        b"BT /F1 24 Tf 90 700 Td (caf\xe9 results) Tj ET "
        b"BT /F1 24 Tf 390 700 Td (caf\xe9 results) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return bytes(document)


def _load_bytes(content: bytes):
    extraction = extract_pdf(BytesIO(content), source_revision="grounding-test")
    manifest = render_manifest(content, extraction, str(uuid4()))
    return extraction, manifest


def test_highlight_is_rebuilt_from_pdfium_glyph_positions_not_model_box():
    content, extraction, manifest = _load(GEOMETRY_PDF)
    original = _action()
    generated = _output(extraction, {0: (original,)})

    result = ground_actions(content, extraction, manifest, generated)
    action = result.slides[0].actions[0]
    page = manifest.slides[0].source

    assert action.x != original.x or action.y != original.y
    assert action.width < page.width / 2
    assert action.height < page.height / 8
    assert action.x + action.width <= page.width
    assert action.y + action.height <= page.height


def test_rotated_and_cropped_page_text_maps_into_viewer_coordinate_plane():
    content, extraction, manifest = _load(GEOMETRY_PDF)
    assert manifest.slides[1].source.width == 1000
    assert manifest.slides[1].source.height == 600
    action = _action(quote="Rotated and cropped page", hint=(0, 0, 1000, 600))

    result = ground_actions(content, extraction, manifest, _output(extraction, {1: (action,)}))
    grounded = result.slides[1].actions[0]

    # The source phrase is vertically typeset on this fixture, so its accurate box
    # is tall and narrow rather than a landscape-shaped line rectangle.
    assert grounded.width < 100
    assert grounded.height > 400
    assert grounded.x + grounded.width <= 1000
    assert grounded.y + grounded.height <= 600


def test_repeated_unicode_quote_in_columns_uses_hint_only_to_disambiguate_source_matches():
    content = _two_column_pdf()
    extraction, manifest = _load_bytes(content)
    page_width = manifest.slides[0].source.width
    action = _action(
        quote="cafe\u0301 results", hint=(page_width * 0.55, 0, page_width * 0.45, 100)
    )

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    grounded = result.slides[0].actions[0]
    assert grounded.x > page_width * 0.55
    assert grounded.width < page_width * 0.25
    assert grounded.height < manifest.slides[0].source.height * 0.05


def test_repeated_quote_with_equally_plausible_hint_is_declined():
    content = _two_column_pdf()
    extraction, manifest = _load_bytes(content)
    action = _action(
        quote="café results",
        hint=(manifest.slides[0].source.width * 0.45, 0, 100, 100),
    )

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert result.slides[0].actions == ()


def test_ligature_quote_maps_back_to_the_original_source_glyph():
    class TextPage:
        source = "o\ufb03ce"

        def count_chars(self):
            return len(self.source)

        def get_text_range(self, index, _count):
            return self.source[index]

        def get_charbox(self, index):
            return (index * 10, 0, (index + 1) * 10, 10)

    class Converter:
        @staticmethod
        def to_bitmap(x, y):
            return x, y

    matches = _matches_normalized_text(TextPage(), TextPage.source, "office", 100, 100, Converter())

    assert matches == [Rect(0, 0, 40, 10)]


def test_missing_text_target_is_dropped_instead_of_using_model_rectangle():
    content, extraction, manifest = _load(GEOMETRY_PDF)
    action = _action(quote="A phrase which is not present in this PDF")

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert result.slides[0].actions == ()


def test_highlight_quote_spanning_adjacent_lines_is_not_bounded_as_one_box():
    content, extraction, manifest = _load(GEOMETRY_PDF)
    action = _action(
        quote="Buddy PDF presentation Navigate",
        hint=(0, 0, manifest.slides[0].source.width, manifest.slides[0].source.height),
    )

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert result.slides[0].actions == ()


def test_scanned_page_without_ocr_confident_match_is_dropped(monkeypatch):
    content, extraction, manifest = _load(SCANNED_PDF)
    assert extraction.slides[0].text == ""
    action = _action(quote="Revenue increased 15 percent", hint=(0, 0, 1224, 1584))
    monkeypatch.setattr(
        "buddy_api.analysis.grounding._ocr_words",
        lambda _page, _width, _height: [],
    )

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert result.slides[0].actions == ()


def test_scanned_ocr_anchor_uses_word_boxes_and_not_page_hint(monkeypatch):
    content, extraction, manifest = _load(SCANNED_PDF)
    words = [
        OcrWord("Revenue", 97, Rect(140, 710, 200, 46)),
        OcrWord("increased", 96, Rect(351, 710, 220, 46)),
        OcrWord("15", 95, Rect(580, 710, 45, 46)),
        OcrWord("percent", 94, Rect(636, 710, 160, 46)),
    ]
    monkeypatch.setattr("buddy_api.analysis.grounding._ocr_words", lambda *_args: words)
    action = _action(quote="Revenue increased 15 percent", hint=(0, 0, 1224, 1584))

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))
    grounded = result.slides[0].actions[0]

    assert grounded.x == pytest.approx(138.5)
    assert grounded.y == pytest.approx(708.5)
    assert grounded.width == pytest.approx(659.0)
    assert grounded.height == pytest.approx(49.0)


def test_ocr_requires_exact_token_sequence_and_confident_words():
    words = [
        OcrWord("Revenue", 97, Rect(10, 20, 60, 18)),
        OcrWord("increased", 64, Rect(74, 20, 90, 18)),
        OcrWord("15", 96, Rect(170, 20, 18, 18)),
        OcrWord("percent", 95, Rect(194, 20, 70, 18)),
    ]

    assert _match_ocr(words, "Revenue increased 15 percent") == []
    assert _match_ocr(words, "Revenue rose 15 percent") == []


def test_ocr_declines_same_baseline_words_separated_by_a_column_gap():
    words = [
        OcrWord("Revenue", 97, Rect(100, 200, 60, 20)),
        # Same baseline and correct token order, but this is a separate column.
        OcrWord("increased", 96, Rect(420, 200, 90, 20)),
    ]

    assert _match_ocr(words, "Revenue increased") == []


def test_split_pdf_text_blocks_join_only_words_on_the_same_visual_line(monkeypatch):
    word_boxes = {
        "revenue": [Rect(100, 200, 90, 24)],
        "increased": [Rect(198, 201, 108, 23)],
        "15": [Rect(315, 201, 20, 22)],
        "percent": [Rect(342, 200, 70, 24)],
    }
    monkeypatch.setattr(
        "buddy_api.analysis.grounding._matches_text",
        lambda _page, _textpage, token, *_args: word_boxes.get(token, []),
    )

    result = _matches_text_words(
        object(), object(), "Revenue increased 15 percent", 1000, 1000, object()
    )
    assert result == [Rect(100, 200, 312, 24)]

    word_boxes["15"] = [Rect(315, 250, 20, 22)]
    word_boxes["percent"] = [Rect(342, 250, 70, 24)]
    wrapped = _matches_text_words(
        object(), object(), "Revenue increased 15 percent", 1000, 1000, object()
    )
    assert wrapped == []


def test_image_snap_prefers_a_unique_pdfium_image_and_rejects_ambiguity():
    first, second = Rect(100, 200, 400, 300), Rect(600, 200, 400, 300)
    objects = [(3, first), (3, second)]

    assert _snap_object(Rect(120, 220, 280, 220), objects, 1200, 1600) == first
    assert _snap_object(Rect(350, 220, 400, 220), objects, 1200, 1600) is None


def test_subregion_inside_scanned_flattened_page_is_preserved_only_for_visual_qa():
    content, extraction, manifest = _load(SCANNED_PDF)
    action = _action("zoom_region", quote=None, hint=(200, 400, 400, 200))

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert len(result.slides[0].actions) == 1
    candidate = result.slides[0].actions[0]
    assert candidate.visual_qa_pending
    assert (candidate.x, candidate.y, candidate.width, candidate.height) == (200, 400, 400, 200)


def test_whole_page_flattened_image_is_not_promoted_as_a_useful_zoom():
    content, extraction, manifest = _load(SCANNED_PDF)
    source = manifest.slides[0].source
    action = _action("zoom_region", quote=None, hint=(0, 0, source.width, source.height))

    result = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))

    assert result.slides[0].actions == ()


def test_object_region_classifies_only_partial_image_crop_as_qa_candidate():
    image = Rect(100, 200, 400, 300)
    whole, pending = _ground_object_target(Rect(105, 205, 390, 290), [(3, image)], 1200, 1600)
    crop, crop_pending = _ground_object_target(Rect(150, 250, 100, 80), [(3, image)], 1200, 1600)

    assert whole == image and not pending
    assert crop == Rect(150, 250, 100, 80) and crop_pending


def test_valid_tagged_object_geometry_is_considered_before_fallback_geometry():
    first, tagged = Rect(100, 200, 400, 300), Rect(600, 200, 400, 300)
    hint = Rect(350, 220, 400, 220)

    assert _snap_object(hint, [(3, first), (3, tagged)], 1200, 1600, [(3, tagged)]) == tagged


def test_tag_tree_mcids_are_read_with_bounds_and_closed(monkeypatch):
    tree, element = object(), object()
    closed = []
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructTree_GetForPage", lambda _page: tree
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructTree_CountChildren", lambda _tree: 1
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructTree_GetChildAtIndex",
        lambda _tree, _index: element,
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructElement_GetMarkedContentIdCount",
        lambda _element: 1,
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructElement_GetMarkedContentIdAtIndex",
        lambda _element, _index: 7,
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructElement_CountChildren", lambda _element: 0
    )
    monkeypatch.setattr(
        "buddy_api.analysis.grounding.raw.FPDF_StructTree_Close", lambda value: closed.append(value)
    )

    assert _tagged_content_ids(object()) == {7}
    assert closed == [tree]


def test_model_highlight_must_supply_a_nonempty_exact_text_anchor():
    with pytest.raises(ValueError):
        ModelAction(
            type="highlight_region",
            purpose="Highlight some text.",
            target_text=" ",
            x=1,
            y=1,
            width=10,
            height=10,
        )


def test_grounding_runs_inside_the_bounded_child_process():
    content, extraction, manifest = _load(GEOMETRY_PDF)
    action = _action(hint=(0, 0, manifest.slides[0].source.width, manifest.slides[0].source.height))
    generated = _output(extraction, {0: (action,)})

    result = execute_grounding(content, extraction, manifest, generated, seconds=30)

    assert len(result.slides[0].actions) == 1
    assert result.slides[0].actions[0].height < manifest.slides[0].source.height / 8


def test_unix_child_does_not_use_data_limit_when_address_space_limit_is_unavailable(monkeypatch):
    from buddy_api.processing import stage

    monkeypatch.setattr(stage.sys, "platform", "linux")
    calls = []
    resource = SimpleNamespace(
        RLIMIT_DATA=2,
        RLIMIT_CPU=1,
        RLIM_INFINITY=-1,
        getrlimit=lambda _limit: (30, -1),
        setrlimit=lambda limit, values: calls.append((limit, values)),
    )

    _configure_child_resource_limits(resource)

    assert calls == [(1, (120, -1))]


def test_darwin_child_leaves_resource_limits_to_parent_memory_and_deadline_guards(monkeypatch):
    from buddy_api.processing import stage

    monkeypatch.setattr(stage.sys, "platform", "darwin")
    _configure_child_resource_limits(SimpleNamespace())
