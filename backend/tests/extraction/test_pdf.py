"""Real PDFium page inspection through the production containment boundary."""

import hashlib
from pathlib import Path

import pytest

from buddy_api.extraction.contracts import ExtractionError, Limits
from buddy_api.extraction.runner import extract_pdf

FIXTURE = Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"


def test_pdf_order_rotation_crop_dimensions_and_immutable_source():
    before = FIXTURE.read_bytes()
    result = extract_pdf(FIXTURE, source_revision="pdf-v1")
    assert result.source_format == "pdf"
    assert result.source_sha256 == hashlib.sha256(before).hexdigest()
    assert [(s.width // 6350, s.height // 6350) for s in result.slides] == [
        (1440, 810),
        (1000, 600),
        (1441, 811),
    ]
    assert all(
        slide.text == "" and slide.objects == () and slide.notes is None for slide in result.slides
    )
    assert [warning.code for warning in result.warnings] == ["pdf_content_not_extracted"]
    assert FIXTURE.read_bytes() == before
    assert extract_pdf(FIXTURE, source_revision="pdf-v1").slides == result.slides
    assert extract_pdf(FIXTURE, source_revision="pdf-v2").slides[0].id != result.slides[0].id


@pytest.mark.parametrize(
    "limits",
    [Limits(slides=2), Limits(source_bytes=100)],
)
def test_pdf_resource_limits_fail_closed(limits):
    with pytest.raises(ExtractionError):
        extract_pdf(FIXTURE, source_revision="limits", limits=limits)


def test_pdf_wrong_signature_and_truncation_fail_without_private_diagnostics(tmp_path):
    for content in (b"private document text", b"%PDF-1.7\nprivate incomplete document"):
        source = tmp_path / "invalid.pdf"
        source.write_bytes(content)
        with pytest.raises(ExtractionError) as error:
            extract_pdf(source, source_revision="invalid")
        assert "private" not in str(error.value)
