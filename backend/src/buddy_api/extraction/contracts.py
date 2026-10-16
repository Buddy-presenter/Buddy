"""Internal extraction v1: top-left slide coordinates in English Metric Units."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Limits(Model):
    source_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    expanded_bytes: int = Field(default=256 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)
    part_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    entries: int = Field(default=10000, ge=1, le=10000)
    xml_nodes: int = Field(default=1000000, ge=1, le=1000000)
    xml_depth: int = Field(default=64, ge=1, le=64)
    slides: int = Field(default=500, ge=1, le=500)
    objects: int = Field(default=25000, ge=1, le=25000)
    text_characters: int = Field(default=2000000, ge=1, le=2000000)
    result_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=16 * 1024 * 1024)
    seconds: float = Field(default=30, ge=0.05, le=60)
    memory_mb: int = Field(default=512, ge=128, le=1024)


class ExtractionError(Exception):
    """A stable, sanitized code only; never attach source parser diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Point(Model):
    x: float
    y: float


class Bounds(Model):
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class Warning(Model):
    code: str
    slide_id: str | None = None
    object_id: str | None = None


type ObjectKind = Literal["shape", "picture", "chart", "table", "group", "connector", "unsupported"]


class SlideObject(Model):
    id: str
    source_id: int
    parent_id: str | None = None
    kind: ObjectKind
    text: str
    bounds: Bounds | None
    # Clockwise transformed corners; bounds is their axis-aligned envelope, not clipped.
    corners: tuple[Point, Point, Point, Point] | None


class Slide(Model):
    id: str
    source_id: int
    index: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    text: str
    notes: str | None
    objects: tuple[SlideObject, ...]


class Extraction(Model):
    schema_version: Literal["buddy.extraction.v1"] = "buddy.extraction.v1"
    extractor_version: Literal["1"] = "1"
    source_revision: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinate_units: Literal["EMU"] = "EMU"
    slides: tuple[Slide, ...]
    warnings: tuple[Warning, ...]
