"""Read bounded OPC parts in memory without resolving external relationships."""

import io
import posixpath
import stat
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from defusedxml.ElementTree import iterparse  # type: ignore[import-untyped]

from buddy_api.extraction.contracts import ExtractionError, Limits

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}
REL = NS["r"] + "/"


def tag(prefix: str, name: str) -> str:
    return "{" + NS[prefix] + "}" + name


def require(element: Element | None) -> Element:
    if element is None:
        raise ExtractionError("invalid_structure")
    return element


def number(value: str | None, *, positive: bool = False) -> int:
    try:
        if value is None or len(value) > 16:
            raise ValueError
        result = int(value)
        if abs(result) > 10**12 or (positive and result <= 0):
            raise ValueError
        return result
    except ValueError:
        raise ExtractionError("invalid_geometry") from None


class Package:
    def __init__(self, path: str, limits: Limits):
        self.zip = ZipFile(path)
        self.limits = limits
        self.nodes = 0
        self.cache: dict[str, Element] = {}
        self.names = set(self.zip.namelist())
        self.rels_cache: dict[str, dict[str, tuple[str, str | None]]] = {}
        self.external_links = False
        self.embedded_material = False

    def __enter__(self) -> "Package":
        try:
            entries = self.zip.infolist()
            if len(entries) > self.limits.entries:
                raise ExtractionError("archive_limit")
            seen: set[str] = set()
            expanded = 0
            for part in entries:
                name = part.filename
                lower_name = name.lower()
                original = part.orig_filename
                clean = name.rstrip("/")
                if (
                    not clean
                    or name != original
                    or "\\" in original
                    or "\x00" in original
                    or name.startswith("/")
                    or "\\" in name
                    or ":" in name
                    or "\x00" in name
                    or any(p in {".", "..", ""} for p in clean.split("/"))
                    or name in seen
                    or stat.S_ISLNK(part.external_attr >> 16)
                    or part.flag_bits & 1
                    or part.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
                ):
                    raise ExtractionError("unsafe_archive")
                seen.add(name)
                expanded += part.file_size
                if expanded > self.limits.expanded_bytes:
                    raise ExtractionError("archive_limit")
                if (
                    lower_name.endswith((".xml", ".rels"))
                    and part.file_size > self.limits.part_bytes
                ):
                    raise ExtractionError("xml_limit")
                if (
                    "vba" in lower_name
                    or "/embeddings/" in lower_name
                    or "/activex/" in lower_name
                    or lower_name.endswith((".exe", ".dll", ".com", ".bat", ".js", ".vbs"))
                ):
                    self.embedded_material = True
            # Check every XML/rels part, including unused parts, for hostile markup.
            for part in entries:
                name = part.filename.lower()
                if name.endswith((".xml", ".rels")):
                    root = self.xml(part.filename, cache=False)
                    if name == "[content_types].xml" or name.endswith(".rels"):
                        self.inspect_embedded_material(root)
                    if name.endswith(".rels"):
                        for relation in root:
                            if relation.get("TargetMode") == "External":
                                self.external_links = True
            return self
        except BaseException:
            self.zip.close()
            raise

    def __exit__(self, *args: object) -> None:
        self.zip.close()

    def inspect_embedded_material(self, root: Element) -> None:
        # Report package facts inside the isolated parser. Upload owns the rejection
        # policy; extraction may still expose cached content without executing it.
        package_relationships = {
            (REL + "package").lower(),
            "http://purl.oclc.org/ooxml/officedocument/relationships/package",
        }
        for element in root.iter():
            for attribute in ("ContentType", "Type"):
                value = element.get(attribute, "").lower()
                if value in package_relationships or any(
                    word in value for word in ("macroenabled", "vba", "activex", "oleobject")
                ):
                    self.embedded_material = True

    def xml(self, part: str, *, cache: bool = True) -> Element:
        if part in self.cache:
            return self.cache[part]
        info = self.zip.getinfo(part)
        if info.file_size > self.limits.part_bytes:
            raise ExtractionError("xml_limit")
        with self.zip.open(info) as stream:
            data = stream.read(self.limits.part_bytes + 1)
        if len(data) > self.limits.part_bytes:
            raise ExtractionError("xml_limit")
        depth = 0
        root = None
        for event, element in iterparse(
            io.BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                self.nodes += 1
                if depth > self.limits.xml_depth or self.nodes > self.limits.xml_nodes:
                    raise ExtractionError("xml_limit")
            else:
                depth -= 1
        result = require(root)
        if cache:
            self.cache[part] = result
        return result

    def relationships(self, part: str) -> dict[str, tuple[str, str | None]]:
        if part in self.rels_cache:
            return self.rels_cache[part]
        path = PurePosixPath(part)
        rels = str(path.parent / "_rels" / (path.name + ".rels")) if part else "_rels/.rels"
        if rels not in self.names:
            return {}
        result: dict[str, tuple[str, str | None]] = {}
        for item in self.xml(rels):
            if item.tag != tag("rel", "Relationship"):
                raise ExtractionError("invalid_relationship")
            rid, kind, target = item.get("Id"), item.get("Type"), item.get("Target")
            if not rid or rid in result or not kind or not target:
                raise ExtractionError("invalid_relationship")
            if item.get("TargetMode") == "External":
                result[rid] = (kind, None)
                continue
            if item.get("TargetMode") not in {None, "Internal"}:
                raise ExtractionError("invalid_relationship")
            parsed = urlsplit(target)
            decoded = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or parsed.query or "\\" in decoded or ":" in decoded:
                raise ExtractionError("unsafe_relationship")
            resolved = posixpath.normpath(
                decoded.lstrip("/")
                if decoded.startswith("/")
                else posixpath.join(str(path.parent) if part else "", decoded)
            )
            if resolved == ".." or resolved.startswith("../") or resolved not in self.names:
                raise ExtractionError("missing_part")
            result[rid] = (kind, resolved)
        self.rels_cache[part] = result
        return result
