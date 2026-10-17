"""Exercise the production executor and compare its real pixels with PowerPoint exports."""

import copy
import hashlib
import json
import shutil
from pathlib import Path
from threading import Event, Thread

import pytest
from jsonschema import Draft202012Validator
from PySide6.QtCore import QPointF, QSize, QTimer
from PySide6.QtGui import QColor, QImage

from buddy_desktop.slide_contract import (
    ContractError,
    Region,
    parse_action,
    parse_manifest,
    read_json,
)
from buddy_desktop.slide_executor import SlideExecutor
from buddy_desktop.slide_surface import SlideSurface, viewport_mapping

FIXTURES = Path(__file__).parent / "fixtures" / "slide_actions"
SCHEMAS = Path(__file__).parents[1] / "src" / "buddy_desktop" / "slide_contracts"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
EXAMPLES = json.loads((FIXTURES / "actions.json").read_text())


def pixels(image):
    # Color-space and DPI metadata are not viewport pixel values.
    converted = image.convertToFormat(QImage.Format.Format_RGB32)
    return converted.size(), bytes(converted.constBits())


def capture(surface, dpr=1):
    # Render the real widget onto an explicit device, independent of host display scaling.
    image = QImage(
        QSize(round(surface.width() * dpr), round(surface.height() * dpr)),
        QImage.Format.Format_RGB32,
    )
    image.setDevicePixelRatio(dpr)
    surface.render(image)
    return image


def action(kind, current=0, **params):
    return {
        "version": "1.0",
        "instance_id": "test:instance",
        "purpose": "Exercise the specified slide operation.",
        "deck_id": MANIFEST["deck_id"],
        "render_id": MANIFEST["render_id"],
        "slide_id": MANIFEST["slides"][current]["slide_id"],
        "type": kind,
        "params": params,
    }


@pytest.fixture
def player(qtbot):
    surface = SlideSurface()
    qtbot.addWidget(surface)
    surface.resize(960, 540)
    surface.show()
    executor = SlideExecutor(surface)
    results = []
    executor.completed.connect(results.append)

    def wait(request):
        qtbot.waitUntil(lambda: any(item.request_id == request for item in results), timeout=5000)
        return next(item for item in results if item.request_id == request)

    assert wait(executor.load_manifest(FIXTURES / "manifest.json")).status == "completed"
    yield surface, executor, wait, results
    executor.close()
    qtbot.waitUntil(lambda: not executor.busy, timeout=5000)
    executor.wait_for_shutdown()


def test_all_published_examples_and_actual_pixels(player):
    surface, executor, wait, results = player
    initial = capture(surface).convertToFormat(QImage.Format.Format_RGB32)
    exported = QImage(str(FIXTURES / "wide.png")).convertToFormat(QImage.Format.Format_RGB32)
    assert pixels(initial) == pixels(exported)
    for example in EXAMPLES:
        assert wait(executor.dispatch(example)).status == "completed"
    assert executor.current_index == 2
    assert surface.region == surface.slide.full
    assert len(results) == 1 + len(EXAMPLES)


@pytest.mark.parametrize(
    "name,index,size",
    [("wide", 0, (960, 540)), ("standard", 1, (720, 540)), ("portrait", 2, (540, 720))],
)
def test_navigation_preserves_each_real_aspect_ratio(player, name, index, size):
    surface, executor, wait, _ = player
    # A portrait test canvas can exceed the physical screen's available work area.
    surface.hide()
    surface.resize(*size)
    result = wait(
        executor.dispatch(
            action("go_to_slide", target_slide_id=MANIFEST["slides"][index]["slide_id"])
        )
    )
    assert result.status == "completed"
    assert result.slide_id == MANIFEST["slides"][index]["slide_id"]
    assert pixels(capture(surface)) == pixels(QImage(str(FIXTURES / f"{name}.png")))


@pytest.mark.parametrize(
    "region,size,crop",
    [
        ({"x": 80, "y": 64, "width": 1760, "height": 200}, (880, 100), (40, 32, 880, 100)),
        ({"x": 80, "y": 320, "width": 1760, "height": 360}, (880, 180), (40, 160, 880, 180)),
    ],
)
def test_title_body_zoom_matches_real_export_crop_and_reset(player, region, size, crop):
    surface, executor, wait, _ = player
    surface.resize(*size)
    for _ in range(3):
        assert wait(executor.dispatch(action("zoom_region", region=region))).status == "completed"
        expected = QImage(str(FIXTURES / "wide.png")).copy(*crop)
        assert pixels(capture(surface)) == pixels(expected)
    assert wait(executor.dispatch(action("reset_view"))).status == "completed"
    surface.resize(960, 540)
    assert pixels(capture(surface)) == pixels(QImage(str(FIXTURES / "wide.png")))


def test_letterboxing_and_resize(player):
    surface, executor, wait, _ = player
    for width, height in [(800, 600), (300, 600), (1200, 300)]:
        surface.resize(width, height)
        image = capture(surface)
        assert image.pixelColor(0, 0) == QColor("#06090D")
        assert image.pixelColor(width // 2, height // 2) == QColor("#101820")
        assert surface.region == surface.slide.full
    assert wait(executor.dispatch(action("next_slide"))).status == "completed"


def test_boundaries_valid_jumps_and_navigation_resets_zoom(player):
    surface, executor, wait, _ = player
    for _ in range(2):
        assert wait(executor.dispatch(action("previous_slide"))).status == "completed"
        assert executor.current_index == 0
    assert (
        wait(
            executor.dispatch(
                action("go_to_slide", target_slide_id=MANIFEST["slides"][2]["slide_id"])
            )
        ).status
        == "completed"
    )
    for _ in range(2):
        assert wait(executor.dispatch(action("next_slide", 2))).status == "completed"
        assert executor.current_index == 2
    assert wait(executor.dispatch(action("previous_slide", 2))).status == "completed"
    assert executor.current_index == 1
    assert surface.region == surface.slide.full


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "run_shell"),
        ("type", "pan"),
        ("version", "2.0"),
        ("instance_id", ""),
        ("purpose", " "),
        ("deck_id", "stale"),
        ("render_id", "stale"),
        ("slide_id", "stale"),
        ("slide_id", -1),
        ("slide_id", True),
        ("extra", 1),
        ("params", {"extra": 1}),
    ],
)
def test_invalid_actions_do_not_mutate_surface(player, field, value):
    surface, executor, wait, _ = player
    data = action("reset_view")
    data[field] = value
    before = capture(surface)
    assert wait(executor.dispatch(data)).status == "rejected"
    assert capture(surface) == before
    assert executor.current_index == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("x", -1),
        ("y", -1),
        ("width", 0),
        ("height", 0),
        ("width", -5),
        ("height", -5),
        ("x", float("nan")),
        ("y", float("inf")),
        ("width", float("-inf")),
        ("x", 1920),
        ("y", 1080),
        ("width", 1921),
        ("height", 1081),
        ("width", 1),
        ("height", 1),
        ("width", True),
        ("extra", 0),
    ],
)
def test_invalid_zoom_regions_rejected_before_execution(player, field, value):
    surface, executor, wait, _ = player
    region = {"x": 80, "y": 64, "width": 1760, "height": 200}
    region[field] = value
    assert wait(executor.dispatch(action("zoom_region", region=region))).status == "rejected"
    assert surface.region == surface.slide.full


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"target_slide_id": "unknown"},
        {"target_slide_id": -1},
        {"slide_index": 0},
        {"target_slide_id": 0},
        {"target_slide_id": True},
        {"target_slide_id": ""},
        {"target_slide_id": "slide:standard/opaque", "index": 1},
    ],
)
def test_invalid_jumps(player, params):
    _, executor, wait, _ = player
    assert wait(executor.dispatch(action("go_to_slide", **params))).status == "rejected"
    assert executor.current_index == 0


@pytest.mark.parametrize("field", list(action("reset_view")))
def test_every_instance_field_is_required(field):
    data = action("reset_view")
    del data[field]
    with pytest.raises(ContractError):
        parse_action(data, parse_manifest(MANIFEST, FIXTURES), 0)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "duplicate",
        "aspect",
        "pixels",
        "path",
        "version",
        "extra",
        "source_extra",
        "raster_extra",
        "dimension",
        "nan",
    ],
)
def test_invalid_manifests(change):
    data = copy.deepcopy(MANIFEST)
    if change == "empty":
        data["slides"] = []
    elif change == "duplicate":
        data["slides"][1]["slide_id"] = data["slides"][0]["slide_id"]
    elif change == "aspect":
        data["slides"][0]["source"]["width"] = 1000
    elif change == "pixels":
        data["slides"][0]["raster"].update(width=7680, height=4320)
    elif change == "path":
        data["slides"][0]["raster"]["path"] = "../secret.png"
    elif change == "version":
        data["version"] = "2.0"
    elif change == "extra":
        data["extra"] = 1
    elif change == "source_extra":
        data["slides"][0]["source"]["rotation"] = 90
    elif change == "raster_extra":
        data["slides"][0]["raster"]["crop"] = 0
    elif change == "dimension":
        data["slides"][0]["source"]["height"] = -1
    elif change == "nan":
        data["slides"][0]["source"]["width"] = float("nan")
    with pytest.raises(ContractError):
        parse_manifest(data, FIXTURES)


def test_golden_coordinate_mappings():
    for case in json.loads((FIXTURES / "mappings.json").read_text()):
        mapping = viewport_mapping(Region(*case["region"]), *case["viewport"], case["dpr"])
        assert mapping.scale == pytest.approx(case["scale"])
        assert (mapping.offset_x, mapping.offset_y) == pytest.approx(case["offset"])
        assert mapping.point(*case["point"]) == QPointF(*case["logical"])
        assert mapping.point(*case["point"], device=True) == QPointF(*case["device"])


@pytest.mark.parametrize("kind", ["manifest", "action"])
def test_standalone_schemas_and_fixtures(kind):
    schema = json.loads((SCHEMAS / f"{kind}-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for value in [MANIFEST] if kind == "manifest" else EXAMPLES:
        validator.validate(value)


@pytest.mark.parametrize(
    "content", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"{", b"[]" * 100]
)
def test_strict_bounded_json(tmp_path, content):
    path = tmp_path / "input.json"
    path.write_bytes(content)
    with pytest.raises(ContractError):
        read_json(path, 128)


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "decode", "dimensions", "digest", "escape"]
)
def test_real_asset_failure_preserves_previous_deck_and_reset(player, tmp_path, failure):
    surface, executor, wait, _ = player
    shutil.copytree(FIXTURES, tmp_path / "deck")
    path = tmp_path / "deck" / "manifest.json"
    data = copy.deepcopy(MANIFEST)
    image = path.parent / "wide.png"
    if failure == "missing":
        image.unlink()
    elif failure == "corrupt":
        image.write_bytes(b"not a PNG")
        data["slides"][0]["raster"]["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    elif failure == "decode":
        image.write_bytes(image.read_bytes()[:60])
        data["slides"][0]["raster"]["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    elif failure == "dimensions":
        data["slides"][0]["raster"].update(width=1920, height=1080)
    elif failure == "digest":
        data["slides"][0]["raster"]["sha256"] = "0" * 64
    elif failure == "escape":
        data["slides"][0]["raster"]["path"] = "../wide.png"
    path.write_text(json.dumps(data))
    assert wait(executor.dispatch(EXAMPLES[0])).status == "completed"
    assert wait(executor.load_manifest(path)).status == "failed"
    assert executor.manifest.deck_id == MANIFEST["deck_id"]
    assert surface.region == surface.slide.full
    assert wait(executor.dispatch(action("reset_view"))).status == "completed"


def test_navigation_asset_failure_and_retry(player, tmp_path):
    surface, executor, wait, _ = player
    shutil.copytree(FIXTURES, tmp_path / "deck")
    assert wait(executor.load_manifest(tmp_path / "deck" / "manifest.json")).status == "completed"
    (tmp_path / "deck" / "standard.png").unlink()
    assert wait(executor.dispatch(EXAMPLES[0])).status == "completed"
    assert wait(executor.dispatch(action("next_slide"))).status == "failed"
    assert executor.current_index == 0
    assert surface.region == surface.slide.full
    shutil.copyfile(FIXTURES / "standard.png", tmp_path / "deck" / "standard.png")
    assert wait(executor.dispatch(action("next_slide"))).status == "completed"
    assert executor.current_index == 1


def test_busy_cancel_reset_and_closed_executor_are_responsive(player, qtbot, monkeypatch):
    import buddy_desktop.slide_executor as module

    surface, executor, wait, results = player
    entered, release = Event(), Event()
    real_load = module.load_image

    def delayed(manifest, slide):
        entered.set()
        assert release.wait(4)
        return real_load(manifest, slide)

    monkeypatch.setattr(module, "load_image", delayed)
    marked = {"x": 80, "y": 64, "width": 100, "height": 30}
    assert wait(executor.dispatch(highlight(marked))).status == "completed"
    request = executor.dispatch(action("next_slide"))
    qtbot.waitUntil(entered.is_set)
    timer_events = []
    QTimer.singleShot(0, lambda: timer_events.append(True))
    qtbot.waitUntil(lambda: bool(timer_events))
    try:
        assert wait(executor.dispatch(action("next_slide"))).status == "rejected"
        assert wait(executor.dispatch(highlight(marked))).status == "rejected"
        assert surface.highlight == Region(**marked)
        assert wait(executor.load_manifest(FIXTURES / "manifest.json")).status == "rejected"
        assert wait(executor.dispatch(action("reset_view"))).status == "completed"
        assert wait(request).status == "cancelled"
        assert surface.region == surface.slide.full
        assert surface.highlight is None
        executor.close()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not executor.busy)
    assert executor.current_index == 0
    assert sum(item.request_id == request for item in results) == 1
    assert wait(executor.dispatch(action("next_slide"))).status == "rejected"


def test_wrong_thread_cannot_mutate_qt(player):
    _, executor, _, _ = player
    errors = []

    def run():
        try:
            executor.dispatch(action("next_slide"))
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = Thread(target=run)
    thread.start()
    thread.join(timeout=1)
    assert errors == ["Slide state must only be accessed on its Qt thread."]


@pytest.mark.parametrize("dpr", [1, 1.5, 2])
def test_real_widget_rendering_at_multiple_device_ratios(player, dpr):
    surface, executor, wait, _ = player
    surface.resize(880, 100)
    assert (
        wait(
            executor.dispatch(
                action("zoom_region", region={"x": 80, "y": 64, "width": 1760, "height": 200})
            )
        ).status
        == "completed"
    )
    image = capture(surface, dpr)
    assert image.width() == round(880 * dpr)
    assert image.height() == round(100 * dpr)
    # Compare complete title raster to the independently cropped export, scaled to this device.
    from PySide6.QtCore import Qt

    reference = (
        QImage(str(FIXTURES / "wide.png"))
        .copy(40, 32, 880, 100)
        .scaled(
            image.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        .convertToFormat(QImage.Format.Format_RGB32)
    )
    image.setDevicePixelRatio(1)
    # QPainter and QImage use different rounding for fractional interpolation. Bound channel error.
    actual_bytes = bytes(image.constBits())
    expected_bytes = bytes(reference.constBits())
    errors = [abs(a - b) for a, b in zip(actual_bytes, expected_bytes, strict=True)]
    assert sum(errors) / len(errors) < 0.75
    assert sum(error > 8 for error in errors) / len(errors) < 0.01


def test_native_device_grab_has_expected_slide_and_letterbox(player):
    surface, _, _, _ = player
    surface.resize(800, 600)
    image = surface.grab().toImage()
    ratio = surface.devicePixelRatioF()
    assert image.width() == round(800 * ratio)
    assert image.height() == round(600 * ratio)
    assert image.pixelColor(0, 0) == QColor("#06090D")
    assert image.pixelColor(round(400 * ratio), round(300 * ratio)) == QColor("#101820")


@pytest.mark.parametrize("index", [-1, 3, 1000, True, 0.0])
def test_invalid_current_indices_are_not_python_negative_indexing(index):
    with pytest.raises(ContractError):
        parse_action(action("reset_view"), parse_manifest(MANIFEST, FIXTURES), index)


def test_fractional_edge_and_maximum_zoom_are_valid(player):
    surface, executor, wait, _ = player
    for region in [
        {"x": 0, "y": 0, "width": 1920, "height": 1080},
        {"x": 1860, "y": 1046.25, "width": 60, "height": 33.75},
        {"x": 80.5, "y": 64.25, "width": 1000.5, "height": 200.25},
    ]:
        assert wait(executor.dispatch(action("zoom_region", region=region))).status == "completed"
        assert surface.region == Region(**region)


def test_cyclic_and_non_json_inputs_are_bounded(player):
    _, executor, wait, _ = player
    cyclic = {}
    cyclic["loop"] = cyclic
    for value in [cyclic, [0] * 1001, {str(i): i for i in range(17)}, {"purpose": object()}]:
        assert wait(executor.dispatch(value)).status == "rejected"


def test_unloaded_executor_can_recover_after_missing_manifest(qtbot, tmp_path):
    surface = SlideSurface()
    qtbot.addWidget(surface)
    executor = SlideExecutor(surface)
    results = []
    executor.completed.connect(results.append)
    request = executor.load_manifest(tmp_path / "missing.json")
    qtbot.waitUntil(lambda: bool(results))
    assert results[-1].request_id == request and results[-1].status == "failed"
    assert executor.manifest is None and surface.region is None
    executor.load_manifest(FIXTURES / "manifest.json")
    qtbot.waitUntil(lambda: len(results) == 2)
    assert results[-1].status == "completed"
    executor.close()
    executor.wait_for_shutdown()


def test_image_symlink_cannot_escape_manifest_directory(player, tmp_path):
    _, executor, wait, _ = player
    manifest = copy.deepcopy(MANIFEST)
    try:
        (tmp_path / "wide.png").symlink_to(FIXTURES / "wide.png")
    except OSError:
        pytest.skip("This OS account cannot create symlinks.")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert wait(executor.load_manifest(tmp_path / "manifest.json")).status == "failed"
    assert executor.current_index == 0


def test_immutable_evidence_states_match_each_published_action(player):
    surface, executor, wait, _ = player
    root = Path(__file__).parents[2] / "docs" / "evidence" / "slide-actions-v1"
    evidence = json.loads((root / "evidence.json").read_text())
    for index, case in enumerate(evidence):
        if index:
            assert case["action"] == EXAMPLES[index - 1]
            assert wait(executor.dispatch(case["action"])).status == "completed"
        assert surface.slide.slide_id == case["slide_id"]
        assert surface.region == Region(**case["region"])
        assert pixels(capture(surface)) == pixels(QImage(str(root / case["capture"])))


def highlight(region, current=0, **changes):
    value = action("highlight_region", current, region=region)
    value["version"] = "1.1"
    value.update(changes)
    return value


def test_highlight_real_pixels_replace_repeat_and_reset(player):
    surface, executor, wait, _ = player
    first = {"x": 400, "y": 600, "width": 200, "height": 100}
    second = {"x": 1000, "y": 600, "width": 200, "height": 100}
    original = capture(surface)
    assert wait(executor.dispatch(highlight(first))).status == "completed"
    marked = capture(surface)
    before, after = original.pixelColor(250, 325), marked.pixelColor(250, 325)
    for old, yellow, actual in zip(
        before.getRgb()[:3], (255, 235, 59), after.getRgb()[:3], strict=True
    ):
        assert abs(actual - round(old * 165 / 255 + yellow * 90 / 255)) <= 1
    assert marked.pixelColor(10, 10) == original.pixelColor(10, 10)
    assert surface.region == surface.slide.full
    assert surface.highlight == Region(**first)
    assert wait(executor.dispatch(highlight(first))).status == "completed"
    assert pixels(capture(surface)) == pixels(marked)  # No accumulated darkening.
    assert wait(executor.dispatch(highlight(second))).status == "completed"
    assert capture(surface).pixelColor(250, 325) == before
    assert surface.highlight == Region(**second)
    assert wait(executor.dispatch(action("reset_view"))).status == "completed"
    assert surface.highlight is None
    assert pixels(capture(surface)) == pixels(original)


def test_highlight_preserves_camera_and_tracks_zoom_and_resize(player):
    surface, executor, wait, _ = player
    zoom = {"x": 400, "y": 400, "width": 1000, "height": 400}
    assert wait(executor.dispatch(action("zoom_region", region=zoom))).status == "completed"
    region = {"x": 500, "y": 500, "width": 100, "height": 40}
    assert wait(executor.dispatch(highlight(region))).status == "completed"
    assert surface.region == Region(**zoom)
    for size in [(800, 600), (400, 300), (960, 540)]:
        surface.resize(*size)
        assert surface.highlight == Region(**region)
        mapping = surface.mapping()
        center = mapping.point(550, 520)
        assert capture(surface).pixelColor(round(center.x()), round(center.y())) != QColor(
            "#101820"
        )
    assert (
        wait(
            executor.dispatch(
                action("zoom_region", region={"x": 450, "y": 450, "width": 600, "height": 400})
            )
        ).status
        == "completed"
    )
    assert surface.highlight == Region(**region)


@pytest.mark.parametrize("kind", ["next_slide", "previous_slide", "go_to_slide"])
def test_changing_slides_clears_highlight_but_rejected_jump_preserves_it(player, kind):
    surface, executor, wait, _ = player
    if kind == "previous_slide":
        assert wait(executor.dispatch(action("next_slide"))).status == "completed"
    current = executor.current_index
    region = {"x": 80, "y": 64, "width": 100, "height": 30}
    assert wait(executor.dispatch(highlight(region, current))).status == "completed"
    assert (
        wait(executor.dispatch(action("go_to_slide", current, target_slide_id="unknown"))).status
        == "rejected"
    )
    assert surface.highlight == Region(**region)
    params = {"target_slide_id": MANIFEST["slides"][2]["slide_id"]} if kind == "go_to_slide" else {}
    assert wait(executor.dispatch(action(kind, current, **params))).status == "completed"
    assert surface.highlight is None


def test_highlight_accepts_small_fractional_regions_and_preserves_boundary_highlight(player):
    surface, executor, wait, _ = player
    region = {"x": 1919.5, "y": 1079.5, "width": 0.5, "height": 0.5}
    assert wait(executor.dispatch(highlight(region))).status == "completed"
    assert surface.highlight == Region(**region)
    assert wait(executor.dispatch(action("previous_slide"))).status == "completed"
    assert surface.highlight == Region(**region)
    assert (
        wait(
            executor.dispatch(
                action("go_to_slide", target_slide_id=MANIFEST["slides"][0]["slide_id"])
            )
        ).status
        == "completed"
    )
    assert surface.highlight == Region(**region)


@pytest.mark.parametrize(
    "field,value",
    [
        ("x", -1),
        ("y", -1),
        ("width", 0),
        ("height", 0),
        ("width", -1),
        ("x", float("nan")),
        ("y", float("inf")),
        ("width", float("-inf")),
        ("x", 1920),
        ("y", 1080),
        ("width", 1921),
        ("height", 1081),
        ("width", True),
        ("extra", 0),
    ],
)
def test_invalid_highlights_preserve_image_camera_and_prior_highlight(player, field, value):
    surface, executor, wait, _ = player
    region = {"x": 80, "y": 64, "width": 100, "height": 30}
    assert wait(executor.dispatch(highlight(region))).status == "completed"
    before = pixels(capture(surface))
    invalid = region | {field: value}
    assert wait(executor.dispatch(highlight(invalid))).status == "rejected"
    assert pixels(capture(surface)) == before
    assert surface.highlight == Region(**region)


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.0"},
        {"version": "2.0"},
        {"render_id": "old"},
        {"deck_id": "other"},
        {"slide_id": "unknown"},
        {"extra": 1},
        {"params": {"text": "Buddy"}},
    ],
)
def test_highlight_contract_rejects_stale_refs_unknown_fields_and_unsupported_versions(
    player, change
):
    surface, executor, wait, _ = player
    value = highlight({"x": 80, "y": 64, "width": 100, "height": 30}) | change
    assert wait(executor.dispatch(value)).status == "rejected"
    assert surface.highlight is None


def test_v11_schema_is_standalone_and_accepts_all_six_catalog_types():
    schema = json.loads((SCHEMAS / "action-v1.1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for value in EXAMPLES:
        validator.validate(value | {"version": "1.1"})
    validator.validate(highlight({"x": 80, "y": 64, "width": 100, "height": 30}))


@pytest.mark.parametrize("dpr", [1, 1.5, 2])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_highlight_pixels_follow_device_ratio_and_all_slide_aspects(player, dpr, index):
    surface, executor, wait, _ = player
    assert (
        wait(
            executor.dispatch(
                action("go_to_slide", target_slide_id=MANIFEST["slides"][index]["slide_id"])
            )
        ).status
        == "completed"
    )
    before = capture(surface, dpr)
    region = {"x": 80, "y": 80, "width": 100, "height": 40}
    assert wait(executor.dispatch(highlight(region, index))).status == "completed"
    after = capture(surface, dpr)
    point = surface.mapping().point(130, 100)
    x, y = round(point.x() * dpr), round(point.y() * dpr)
    assert before.pixelColor(x, y) != after.pixelColor(x, y)
    assert surface.region == surface.slide.full


def test_v11_word_phrase_expression_formula_evidence_and_clear(player):
    surface, executor, wait, _ = player
    assert wait(executor.load_manifest(FIXTURES / "manual-manifest.json")).status == "completed"
    root = Path(__file__).parents[2] / "docs" / "evidence" / "slide-actions-v1.1"
    cases = json.loads((root / "evidence.json").read_text())
    for index, case in enumerate(cases):
        if index:
            assert wait(executor.dispatch(case["action"])).status == "completed"
        assert surface.region == Region(**case["region"])
        expected_highlight = Region(**case["highlight"]) if case["highlight"] else None
        assert surface.highlight == expected_highlight
        assert pixels(capture(surface)) == pixels(QImage(str(root / case["capture"])))
