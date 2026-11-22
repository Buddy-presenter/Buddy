"""Bind a live Office slideshow to an authenticated, immutable saved analysis."""

import hashlib
from dataclasses import dataclass

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo, same_slide_size
from buddy_desktop.review_service import ReviewAnalysis, ReviewSlide


@dataclass(frozen=True)
class PowerPointSessionBinding:
    deck_id: str
    analysis_id: str
    source_revision: str
    source_sha256: str
    path: str
    window_id: int
    slides: tuple[ReviewSlide, ...]
    render_id: str = ""

    @classmethod
    def create(
        cls, analysis: ReviewAnalysis, content: bytes, info: PresentationInfo
    ) -> "PowerPointSessionBinding":
        if (
            analysis.source_format != "pptx"
            or not analysis.office_safe
            or not analysis.source_revision
            or not analysis.source_sha256
            or len(content) > 64 * 1024 * 1024
            or hashlib.sha256(content).hexdigest() != analysis.source_sha256
            or not analysis.slides
            or len({slide.slide_id for slide in analysis.slides}) != len(analysis.slides)
            or len({slide.native_slide_id for slide in analysis.slides}) != len(analysis.slides)
            or any(slide.native_slide_id <= 0 for slide in analysis.slides)
            or info.window_id <= 0
        ):
            raise PowerPointError("A matching saved PowerPoint analysis is required.")
        binding = cls(
            str(analysis.deck_id),
            str(analysis.id),
            analysis.source_revision,
            analysis.source_sha256,
            info.identity,
            info.window_id,
            analysis.slides,
            analysis.render_id,
        )
        binding.slide_id(info)
        return binding

    def slide_id(self, info: PresentationInfo) -> str:
        if (
            info.identity != self.path
            or info.window_id != self.window_id
            or info.slide_count != len(self.slides)
            or not 1 <= info.slide_index <= len(self.slides)
        ):
            raise PowerPointError("The PowerPoint slideshow no longer matches the saved session.")
        slide = self.slides[info.slide_index - 1]
        if info.slide_id != slide.native_slide_id or not same_slide_size(
            info.slide_width, info.slide_height, slide.width, slide.height
        ):
            raise PowerPointError("The live PowerPoint slide differs from the saved analysis.")
        return slide.slide_id
