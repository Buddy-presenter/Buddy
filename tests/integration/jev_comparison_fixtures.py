"""Public synthetic inputs, authored before running the paired provider comparison.

These are evaluation fixtures, not analysis-model output or claimed visual QA results.
Native validation and provider decisions must be collected separately by the harness.
"""

from pathlib import Path
from uuid import UUID

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.util import Pt

from buddy_api.analysis.contracts import (
    ActionGrounding,
    AnalysisInput,
    ContentAction,
    GroundingTrace,
    Manifest,
    PowerPointAnalysisInput,
    PowerPointContentAction,
    PowerPointParameters,
    PowerPointSlideOutput,
    Region,
    SlideOutput,
    Summary,
    digest,
)
from buddy_api.analysis.powerpoint import powerpoint_manifest
from buddy_api.extraction.contracts import Extraction

SLIDES = (
    (
        "Financial results",
        (
            "Revenue $48 million",
            "Operating margin 22 percent",
            "Cash reserves $12 million",
            "Customer count 1,200",
        ),
    ),
    (
        "Product roadmap",
        (
            "Atlas launches in March",
            "Borealis launches in June",
            "Mobile release in September",
            "Migration ends in December",
        ),
    ),
    (
        "Customer performance",
        (
            "Customer retention 94 percent",
            "Customer churn 6 percent",
            "Support response 2 hours",
            "Customer satisfaction 88 percent",
        ),
    ),
    (
        "Security and privacy",
        (
            "Encryption protects stored records",
            "MFA protects account access",
            "Keys rotate every 30 days",
            "Audit records last 90 days",
        ),
    ),
    (
        "Hiring and staffing",
        (
            "Engineering hires 8 people",
            "Support hires 4 people",
            "Sales hires 3 people",
            "Training budget $60 thousand",
        ),
    ),
    (
        "Regional revenue",
        (
            "North revenue $20 million",
            "South revenue $16 million",
            "East revenue $8 million",
            "West revenue $4 million",
        ),
    ),
    (
        "System performance",
        (
            "Median latency 180 milliseconds",
            "P95 latency 420 milliseconds",
            "Availability 99.9 percent",
            "Throughput 800 requests per second",
        ),
    ),
    (
        "Next steps and risks",
        (
            "Expand the pilot in January",
            "Review supplier risk monthly",
            "Publish the rollout checklist",
            "Confirm the budget with finance",
        ),
    ),
)
RECTS = ((50, 150, 400, 110), (510, 150, 400, 110), (50, 320, 400, 110), (510, 320, 400, 110))


def sources(folder: Path) -> dict[str, Path]:
    from reportlab.pdfgen.canvas import Canvas

    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder / "comparison.pdf"
    canvas = Canvas(str(pdf), pagesize=(960, 540))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(960), Pt(540)
    for title, cards in SLIDES:
        canvas.setFont("Helvetica-Bold", 28)
        canvas.drawString(50, 475, title)
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        heading = slide.shapes.add_textbox(Pt(50), Pt(30), Pt(850), Pt(60))
        heading.text_frame.text = title
        heading.text_frame.paragraphs[0].font.size = Pt(28)
        for (x, y, w, h), text in zip(RECTS, cards, strict=True):
            canvas.setFillColorRGB(0.94, 0.94, 0.94)
            canvas.roundRect(x, 540 - y - h, w, h, 10, fill=1)
            canvas.setFillColorRGB(0, 0, 0)
            canvas.setFont("Helvetica", 18)
            canvas.drawString(x + 16, 540 - y - h / 2, text)
            box = slide.shapes.add_shape(
                MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE, Pt(x), Pt(y), Pt(w), Pt(h)
            )
            box.fill.solid()
            box.fill.fore_color.rgb = RGBColor(240, 240, 240)
            box.text_frame.text = text
            p = box.text_frame.paragraphs[0]
            p.font.size, p.font.color.rgb = Pt(18), RGBColor(0, 0, 0)
        canvas.showPage()
    canvas.save()
    pptx = folder / "comparison.pptx"
    deck.save(pptx)
    return {"pdf": pdf, "pptx": pptx}


def document(extraction: Extraction, deck: UUID, job: UUID, extraction_id: UUID):
    """Build canonical immutable fixtures against actual extracted slide IDs and dimensions."""
    native = extraction.source_format == "pptx"
    render_id = ("pptx-" if native else "pdfium-5.14.0-144dpi:") + extraction.source_sha256
    manifest = (
        powerpoint_manifest(extraction, str(deck))
        if native
        else Manifest.model_validate(
            dict(
                version="1.0",
                deck_id=str(deck),
                render_id=render_id,
                slides=[
                    dict(
                        slide_id=s.id,
                        source=dict(
                            width=s.width // 6350,
                            height=s.height // 6350,
                            units="source_pixels",
                            origin="top_left",
                        ),
                        # The action path uses original PDF bytes, not this legacy raster asset.
                        raster=dict(
                            path=f"slide-{i}.png",
                            width=s.width // 6350,
                            height=s.height // 6350,
                            transform="scale",
                            sha256=extraction.source_sha256,
                        ),
                    )
                    for i, s in enumerate(extraction.slides)
                ],
            )
        )
    )
    slides, grounding = [], []
    for i, (s, (title, cards)) in enumerate(zip(extraction.slides, SLIDES, strict=True)):
        actions = []
        if i in {0, 2}:
            for index, ((x, y, w, h), text) in enumerate(zip(RECTS, cards, strict=True)):
                for kind in ("zoom_region", "highlight_region"):
                    identifier = f"s{i}-card{index}-{kind}"
                    scale = 1 if native else 2
                    region = Region(x=x * scale, y=y * scale, width=w * scale, height=h * scale)
                    common = dict(
                        instance_id=identifier,
                        purpose=("Enlarge " if kind == "zoom_region" else "Emphasize ") + text,
                        deck_id=str(deck),
                        render_id=render_id,
                        slide_id=s.id,
                        type=kind,
                    )
                    if native:
                        obj = next(o for o in s.objects if o.text.strip() == text)
                        action = PowerPointContentAction(
                            **common,
                            params=PowerPointParameters(
                                region=region,
                                source_shape_id=obj.source_id,
                                target_text=text if kind == "highlight_region" else "",
                            ),
                        )
                    else:
                        action = ContentAction(
                            version="1.1", **common, params={"region": region.model_dump()}
                        )
                        grounding.append(
                            ActionGrounding(
                                instance_id=identifier,
                                action_sha256=digest(action),
                                trace=GroundingTrace(
                                    method="pdf_words",
                                    target_text=text,
                                    model_region=region,
                                    resolved_region=region,
                                ),
                            )
                        )
                    actions.append(action)
        summary = Summary(status="available", text=title + ". " + "; ".join(cards))
        slides.append(
            (PowerPointSlideOutput if native else SlideOutput)(
                slide_id=s.id, summary=summary, actions=tuple(actions)
            )
        )
    common = dict(
        extraction_id=extraction_id,
        job_id=job,
        source_revision=extraction.source_revision,
        source_sha256=extraction.source_sha256,
        manifest=manifest,
        presentation_summary=Summary(
            status="available",
            text=(
                "Synthetic evaluation of financial results, customers, product roadmap, "
                "security, hiring, regional revenue, performance and next steps."
            ),
        ),
        slides=tuple(slides),
    )
    if native:
        return PowerPointAnalysisInput(office_safe=True, **common)
    return AnalysisInput(
        renderer_version="pdfium-5.14.0-144dpi",
        display_profile="wide-2x",
        validator_version="desktop-preflight-1",
        grounding=tuple(grounding),
        **common,
    )


def cases() -> list[dict]:
    """Expected target labels are frozen before looking at either provider's predictions."""
    result = []

    def add(
        text, expected, *, current=0, earlier=(), focus="slide", provisional=False, category="focus"
    ):
        result.append(
            dict(
                id=f"case-{len(result) + 1:03}",
                text=text,
                expected=expected,
                current=current,
                earlier=list(earlier),
                focus=focus,
                provisional=provisional,
                category=category,
            )
        )

    for current in (0, 2):
        for card, text in enumerate(SLIDES[current][1]):
            add(
                "Please zoom into " + text + ".",
                {"tool": "zoom_region", "action_id": f"s{current}-card{card}-zoom_region"},
                current=current,
            )
            add(
                "Please highlight " + text + ".",
                {
                    "tool": "highlight_region",
                    "action_id": f"s{current}-card{card}-highlight_region",
                },
                current=current,
            )
    for destination in (1, 3, 4, 5, 6, 7):
        add(
            "Let's now discuss " + SLIDES[destination][0] + ".",
            {"tool": "go_to_slide", "slide": destination},
            category="topic_navigation",
        )
    for text, destination in (
        ("Next slide.", 1),
        ("Go to slide seven.", 6),
        ("Previous slide.", 1),
    ):
        add(
            text,
            {"tool": "go_to_slide", "slide": destination},
            current=2 if "Previous" in text else 0,
            category="explicit_navigation",
        )
    for text in (
        "Hello everyone, thanks for joining.",
        "Don't highlight the revenue.",
        "We will discuss security later.",
        "As I said earlier, cash reserves were twelve million.",
        "Imagine if customer churn were six percent.",
        "Does anyone have a question about revenue?",
        "Never mind, don't change anything.",
        "My colleague said, quote, zoom into the cash reserves, end quote.",
        "We are checking the microphone, one two three.",
        "The next slide has more detail, but stay here for now.",
    ):
        add(text, {"tool": "no_action"}, category="abstention")
    add(
        "Actually, highlight cash reserves, not revenue.",
        {"tool": "highlight_region", "action_id": "s0-card2-highlight_region"},
        earlier=("We are looking at revenue.",),
        category="correction",
    )
    add(
        "No, zoom into operating margin instead.",
        {"tool": "zoom_region", "action_id": "s0-card1-zoom_region"},
        earlier=("Highlight the customer count.",),
        category="correction",
    )
    add(
        "Please highlight that figure.",
        {"tool": "highlight_region", "action_id": "s0-card0-highlight_region"},
        earlier=("Revenue is forty eight million.",),
        category="pronoun",
    )
    add(
        "Let's enlarge that number.",
        {"tool": "zoom_region", "action_id": "s2-card1-zoom_region"},
        current=2,
        earlier=("Customer churn is six percent.",),
        category="pronoun",
    )
    add("Please zoom into", {"tool": "no_action"}, provisional=True, category="unfinished")
    add("We're going to", {"tool": "no_action"}, provisional=True, category="unfinished")
    add(
        "Please highlight revenue",
        {"tool": "highlight_region", "action_id": "s0-card0-highlight_region"},
        provisional=True,
        category="unfinished",
    )
    add("Please reset the view.", {"tool": "reset_view"}, focus="zoom_region", category="reset")
    return result
