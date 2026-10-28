"""Real-source comparison must reject stale input and expose displaced rectangles."""

import hashlib

import pytest
from backend.scripts.diagnose_pdf_region import diagnose
from backend.tests.analysis.test_grounding import GEOMETRY_PDF


def test_diagnostic_rebuilds_target_independently_of_displaced_saved_box():
    content = GEOMETRY_PDF.read_bytes()
    saved = (0.0, 700.0, 100.0, 30.0)
    report = diagnose(
        content, 1, "Buddy PDF presentation", saved, hashlib.sha256(content).hexdigest()
    )
    assert report["source_identity_verified"]
    assert report["grounded_region"] is not None
    assert report["saved_region"]["y"] == 700
    assert report["intersection_over_union"] == 0
    assert report["semantic_verified"] is False


def test_diagnostic_refuses_a_different_pdf_revision():
    with pytest.raises(ValueError, match="SHA-256 differs"):
        diagnose(GEOMETRY_PDF.read_bytes(), 1, "Buddy PDF presentation", expected_sha256="0" * 64)
