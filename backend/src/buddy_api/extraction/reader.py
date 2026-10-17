"""Narrow PresentationML extractor; unsupported visual fidelity is explicit."""

import hashlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree.ElementTree import Element

from buddy_api.extraction.contracts import (
    Extraction,
    ExtractionError,
    Limits,
    ObjectKind,
    Slide,
    SlideObject,
    Warning,
)
from buddy_api.extraction.geometry import IDENTITY, Matrix, envelope, point, transform
from buddy_api.extraction.package import NS, REL, Package, number, require, tag


class Reader:
    def __init__(self, package: Package, revision: str, digest: str):
        self.package = package
        self.revision = revision
        self.digest = digest
        self.warnings: list[Warning] = []
        self.warning_keys: set[tuple[str, str | None, str | None]] = set()
        self.objects = 0
        self.characters = 0

    def warn(self, code: str, slide: str | None = None, obj: str | None = None) -> None:
        warning = Warning(code=code, slide_id=slide, object_id=obj)
        key = (code, slide, obj)
        if key not in self.warning_keys:
            self.warning_keys.add(key)
            self.warnings.append(warning)

    def text(self, root: Element) -> str:
        paragraphs = []
        for para in root.iter(tag("a", "p")):
            runs = []
            for item in para.iter():
                if item.tag == tag("a", "t"):
                    runs.append(item.text or "")
                elif item.tag == tag("a", "br"):
                    runs.append("\n")
            paragraphs.append("".join(runs))
        result = "\n".join(paragraphs)
        self.characters += len(result)
        if self.characters > self.package.limits.text_characters:
            raise ExtractionError("text_limit")
        return result

    def related(self, part: str, kind: str) -> list[str]:
        return [p for t, p in self.package.relationships(part).values() if t == REL + kind and p]

    def inherited(self, part: str) -> list[Element]:
        layouts = self.related(part, "slideLayout")
        if len(layouts) > 1:
            raise ExtractionError("invalid_relationship")
        roots = [self.package.xml(p) for p in layouts]
        for layout in layouts:
            masters = self.related(layout, "slideMaster")
            if len(masters) > 1:
                raise ExtractionError("invalid_relationship")
            roots.extend(self.package.xml(p) for p in masters)
        return roots

    def xfrm(self, shape: Element, inherited: list[Element]) -> Element | None:
        for query in ("p:spPr/a:xfrm", "p:grpSpPr/a:xfrm", "p:xfrm"):
            found = shape.find(query, NS)
            if found is not None:
                return found
        ph = shape.find(".//p:ph", NS)
        if ph is not None:
            for root in inherited:
                for candidate in root.findall("p:cSld/p:spTree/p:sp", NS):
                    other = candidate.find(".//p:ph", NS)
                    if other is None:
                        continue
                    inherited_type = ph.get("type", "obj")
                    if inherited_type in {
                        "obj",
                        "chart",
                        "tbl",
                        "clipArt",
                        "dgm",
                        "media",
                        "pic",
                        "subTitle",
                    }:
                        inherited_type = "body"
                    elif inherited_type == "ctrTitle":
                        inherited_type = "title"
                    match = (
                        other.get("idx", "0") == ph.get("idx", "0")
                        if root.tag == tag("p", "sldLayout")
                        else other.get("type", "obj") == inherited_type
                    )
                    if match:
                        found = candidate.find("p:spPr/a:xfrm", NS)
                        if found is not None:
                            return found
                        ph = other
                        break
        return None

    def shapes(
        self,
        tree: Element,
        slide_id: str,
        part: str,
        inherited: list[Element],
        parent: Matrix = IDENTITY,
        parent_id: str | None = None,
        seen: set[int] | None = None,
    ) -> list[SlideObject]:
        seen = set() if seen is None else seen
        records = []
        for shape in tree:
            if shape.tag in {tag("p", "nvGrpSpPr"), tag("p", "grpSpPr")}:
                continue
            if shape.tag == tag("p", "extLst"):
                self.warn("extensions_not_extracted", slide_id, parent_id)
                continue
            self.objects += 1
            if self.objects > self.package.limits.objects:
                raise ExtractionError("object_limit")
            ident = shape.find(".//p:cNvPr", NS)
            if ident is None:
                self.warn("unsupported_shape", slide_id, parent_id)
                continue
            source_id = number(ident.get("id"), positive=True)
            if source_id in seen:
                raise ExtractionError("duplicate_object_id")
            seen.add(source_id)
            obj_id = f"{slide_id}/object/{source_id}"
            kinds: dict[str, ObjectKind] = {
                tag("p", "sp"): "shape",
                tag("p", "pic"): "picture",
                tag("p", "grpSp"): "group",
                tag("p", "cxnSp"): "connector",
            }
            kind: ObjectKind = kinds.get(shape.tag, "unsupported")
            text = self.text(shape) if kind != "group" else ""
            if shape.tag == tag("p", "graphicFrame"):
                kind = "table" if shape.find(".//a:tbl", NS) is not None else "unsupported"
                chart = shape.find(".//c:chart", NS)
                if chart is not None:
                    kind = "chart"
                    rid = chart.get(tag("r", "id"), "")
                    relation = self.package.relationships(part).get(rid)
                    if relation is None or relation[0] != REL + "chart" or not relation[1]:
                        raise ExtractionError("invalid_relationship")
                    chart_root = self.package.xml(relation[1])
                    values = chart_root.findall(".//c:v", NS)
                    cached = "\n".join(v.text or "" for v in values)
                    text = "\n".join(t for t in (self.text(chart_root), cached) if t)
                    self.characters += len(cached)
                    if self.characters > self.package.limits.text_characters:
                        raise ExtractionError("text_limit")
                    self.warn("chart_cache_only", slide_id, obj_id)
            if kind == "unsupported":
                self.warn("unsupported_shape", slide_id, obj_id)
            if shape.find(".//a:custGeom", NS) is not None:
                self.warn("custom_geometry_envelope_only", slide_id, obj_id)
            if shape.find(".//a:srcRect", NS) is not None:
                self.warn("picture_crop_not_extracted", slide_id, obj_id)
            if any(
                e.tag.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/math}")
                for e in shape.iter()
            ):
                self.warn("equations_not_extracted", slide_id, obj_id)
            if ident.get("hidden") in {"1", "true"}:
                self.warn("hidden_object", slide_id, obj_id)
            xfrm = self.xfrm(shape, inherited)
            if kind == "group" and (
                xfrm is None or xfrm.find("a:chOff", NS) is None or xfrm.find("a:chExt", NS) is None
            ):
                raise ExtractionError("invalid_group_geometry")
            corners = None
            bounds = None
            child = parent
            if xfrm is not None:
                world, child, w, h = transform(xfrm, parent)
                corners = (
                    point(world, 0, 0),
                    point(world, w, 0),
                    point(world, w, h),
                    point(world, 0, h),
                )
                bounds = envelope(corners)
            else:
                self.warn("missing_geometry", slide_id, obj_id)
            records.append(
                SlideObject(
                    id=obj_id,
                    source_id=source_id,
                    parent_id=parent_id,
                    kind=kind,
                    text=text,
                    bounds=bounds,
                    corners=corners,
                )
            )
            if kind == "group":
                records.extend(self.shapes(shape, slide_id, part, [], child, obj_id, seen))
        return records

    def read(self) -> Extraction:
        office = self.related("", "officeDocument")
        if len(office) != 1:
            raise ExtractionError("invalid_presentation")
        part = office[0]
        types = self.package.xml("[Content_Types].xml")
        content_ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        if types.tag != content_ns + "Types":
            raise ExtractionError("invalid_presentation")
        main_types = [
            item.get("ContentType")
            for item in types
            if item.tag == content_ns + "Override" and item.get("PartName") == "/" + part
        ]
        if main_types != [
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
        ]:
            raise ExtractionError("unsupported_presentation")
        root = self.package.xml(part)
        if root.tag != tag("p", "presentation"):
            raise ExtractionError("unsupported_presentation")
        size = require(root.find("p:sldSz", NS))
        width, height = number(size.get("cx"), positive=True), number(size.get("cy"), positive=True)
        slide_list = require(root.find("p:sldIdLst", NS))
        if len(slide_list) > self.package.limits.slides:
            raise ExtractionError("slide_limit")
        relations = self.package.relationships(part)
        slides = []
        ids: set[int] = set()
        targets: set[str] = set()
        for index, entry in enumerate(slide_list):
            if entry.tag != tag("p", "sldId"):
                raise ExtractionError("invalid_presentation")
            native_id = number(entry.get("id"), positive=True)
            relation = relations.get(entry.get(tag("r", "id"), ""))
            if not relation or relation[0] != REL + "slide" or not relation[1]:
                raise ExtractionError("invalid_relationship")
            target = relation[1]
            if native_id in ids or target in targets:
                raise ExtractionError("duplicate_slide_id")
            ids.add(native_id)
            targets.add(target)
            slide_id = str(uuid5(NAMESPACE_URL, f"buddy:{self.digest}:{self.revision}:{native_id}"))
            slide_root = self.package.xml(target)
            if slide_root.tag != tag("p", "sld"):
                raise ExtractionError("invalid_presentation")
            inherited = self.inherited(target)
            objects = self.shapes(
                require(slide_root.find("p:cSld/p:spTree", NS)), slide_id, target, inherited
            )
            notes = None
            note_parts = self.related(target, "notesSlide")
            if len(note_parts) > 1:
                raise ExtractionError("invalid_relationship")
            if note_parts:
                note_root = self.package.xml(note_parts[0])
                bodies = []
                for shape in note_root.findall("p:cSld/p:spTree/p:sp", NS):
                    ph = shape.find(".//p:ph", NS)
                    if ph is not None and ph.get("type") == "body":
                        bodies.append(self.text(shape))
                notes = "\n".join(bodies) if bodies else None
            if notes is None:
                self.warn("notes_unavailable", slide_id)
            if inherited:
                self.warn("inherited_artwork_not_extracted", slide_id)
            if slide_root.get("show") in {"0", "false"}:
                self.warn("hidden_slide", slide_id)
            if slide_root.find("p:timing", NS) is not None:
                self.warn("animations_not_extracted", slide_id)
            slides.append(
                Slide(
                    id=slide_id,
                    source_id=native_id,
                    index=index,
                    width=width,
                    height=height,
                    text="\n".join(o.text for o in objects if o.text),
                    notes=notes,
                    objects=tuple(objects),
                )
            )
        if self.package.external_links:
            self.warn("external_links_not_followed")
        if self.package.embedded_material:
            self.warn("embedded_material_not_executed")
        self.warn("visual_fidelity_not_rendered")
        return Extraction(
            source_revision=self.revision,
            source_sha256=self.digest,
            slides=tuple(slides),
            warnings=tuple(self.warnings),
        )


def read_package(path: str, revision: str, limits: Limits) -> Extraction:
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with Package(path, limits) as package:
        return Reader(package, revision, digest).read()
