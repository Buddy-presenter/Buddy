"""DrawingML affine transforms, including nested group coordinate systems."""

import math
from xml.etree.ElementTree import Element

from buddy_api.extraction.contracts import Bounds, Point
from buddy_api.extraction.package import NS, number, require

type Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def compose(a: Matrix, b: Matrix) -> Matrix:
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def translate(x: float, y: float) -> Matrix:
    return (1, 0, 0, 1, x, y)


def point(m: Matrix, x: float, y: float) -> Point:
    return Point(x=round(m[0] * x + m[2] * y + m[4], 6), y=round(m[1] * x + m[3] * y + m[5], 6))


def transform(xfrm: Element, parent: Matrix) -> tuple[Matrix, Matrix, float, float]:
    off, ext = require(xfrm.find("a:off", NS)), require(xfrm.find("a:ext", NS))
    x, y = number(off.get("x")), number(off.get("y"))
    w, h = number(ext.get("cx")), number(ext.get("cy"))
    if w < 0 or h < 0:
        from buddy_api.extraction.contracts import ExtractionError

        raise ExtractionError("invalid_geometry")
    radians = math.radians(number(xfrm.get("rot", "0")) / 60000)
    c, s = math.cos(radians), math.sin(radians)
    fx = -1 if xfrm.get("flipH") in {"1", "true"} else 1
    fy = -1 if xfrm.get("flipV") in {"1", "true"} else 1
    local = compose(
        translate(x + w / 2, y + h / 2),
        compose((c, s, -s, c, 0, 0), compose((fx, 0, 0, fy, 0, 0), translate(-w / 2, -h / 2))),
    )
    world = compose(parent, local)
    child_off, child_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
    child = world
    if child_off is not None or child_ext is not None:
        child_off, child_ext = require(child_off), require(child_ext)
        cw, ch = (
            number(child_ext.get("cx"), positive=True),
            number(child_ext.get("cy"), positive=True),
        )
        child = compose(
            world,
            compose(
                (w / cw, 0, 0, h / ch, 0, 0),
                translate(-number(child_off.get("x")), -number(child_off.get("y"))),
            ),
        )
    return world, child, w, h


def envelope(corners: tuple[Point, Point, Point, Point]) -> Bounds:
    xs, ys = [p.x for p in corners], [p.y for p in corners]
    return Bounds(x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys))
