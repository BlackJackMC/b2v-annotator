import copy
import json
import math
import os
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from tasks.svg.geometry import stroke_outline


def stroke_data(stroke, clone=False):
    result = {"tool": "freehand" if stroke["tool"] == "draw" else stroke["tool"],
              "points": copy.deepcopy(stroke["points"]) if clone else stroke["points"]}
    if stroke.get("width", 1) != 1:
        result["width"] = stroke["width"]
    if "widths" in stroke:
        result["widths"] = list(stroke["widths"]) if clone else stroke["widths"]
    return result


def new_annotation(task, user_id):
    return {
        "version": 2,
        "task_id": task["task_id"],
        "image": task["source"]["image"],
        "image_sha256": task["source"]["image_sha256"],
        "region": task["source"]["region_index"] + 1,
        "crop": list(task["source"]["crop_xywh"]),
        "annotator": user_id,
        "strokes": []
    }


def compact_annotation(data, task, user_id):
    result = new_annotation(task, user_id)
    if data.get("image_sha256", result["image_sha256"]) != result["image_sha256"]:
        raise ValueError("The saved tracing uses an older source image. Use a new image name for a revised source.")
    if data.get("schema_version") == "board2vec.trace.v1":
        if data.get("task_id") != task["task_id"] or data["source"]["crop_xywh"] != result["crop"]:
            raise ValueError("This tracing belongs to a different image or region.")
    else:
        if data.get("version") != 2:
            raise ValueError("Unsupported tracing JSON version.")
        if any(data.get(key) != result[key] for key in ("task_id", "image", "region", "crop")):
            raise ValueError("This tracing belongs to a different image or region.")
    if not isinstance(data.get("strokes"), list):
        raise ValueError("Expected a strokes list.")
    result["strokes"] = [stroke_data(stroke, clone=True) for stroke in data["strokes"]]
    return result


def canvas_strokes(data):
    strokes = [stroke_data(stroke, clone=True) for stroke in data["strokes"]]
    for index, stroke in enumerate(strokes):
        stroke.update(id=f"stroke_{index + 1}", tool="draw" if stroke["tool"] == "freehand" else stroke["tool"])
    return strokes


def validate_annotation(data, task=None, complete=False):
    if data.get("version") != 2:
        raise ValueError("Unsupported tracing JSON version.")
    for key in ("task_id", "image", "annotator"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Annotation {key} is missing.")
    crop = data.get("crop")
    if not isinstance(crop, list) or len(crop) != 4 or any(type(v) is not int for v in crop):
        raise ValueError("Crop must contain integer x, y, width and height.")
    if crop[0] < 0 or crop[1] < 0 or crop[2] <= 0 or crop[3] <= 0:
        raise ValueError("Invalid crop bounds.")
    if type(data.get("region")) is not int or data["region"] < 1:
        raise ValueError("Region numbers start at one.")
    if task and (data["task_id"] != task["task_id"] or crop != task["source"]["crop_xywh"] or
                 data["image"] != task["source"]["image"] or data["region"] != task["source"]["region_index"] + 1):
        raise ValueError("This tracing belongs to a different image or region.")
    if not isinstance(data.get("strokes"), list):
        raise ValueError("Expected a strokes list.")
    for stroke in data["strokes"]:
        if stroke.get("tool") not in ("freehand", "polyline"):
            raise ValueError("Unknown tracing tool.")
        width = stroke.get("width", 1)
        if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or not 0 < width <= 50:
            raise ValueError("Stroke width must be greater than zero and at most 50 pixels.")
        if not isinstance(stroke.get("points"), list) or not stroke["points"]:
            raise ValueError("Each stroke needs at least one point.")
        if "widths" in stroke:
            widths = stroke["widths"]
            if not isinstance(widths, list) or len(widths) != len(stroke["points"]) or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or not 0 < value <= 50 for value in widths):
                raise ValueError("Variable widths must contain one valid width per point.")
        for point in stroke["points"]:
            if not isinstance(point, list) or len(point) != 2 or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
                raise ValueError("Stroke coordinates must be finite x/y pairs.")
            if not (0 <= point[0] <= crop[2] and 0 <= point[1] <= crop[3]):
                raise ValueError("A stroke is outside the crop.")
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("This annotation exceeds 8 MB. Use smaller AABB regions.")
    return encoded


def svg_text(data, validated=False):
    if not validated:
        validate_annotation(data)
    width, height = data["crop"][2:]
    root = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "width": str(width), "height": str(height),
        "viewBox": f"0 0 {width} {height}", "overflow": "hidden"
    })
    group = ET.SubElement(root, "g", {
        "fill": "none", "stroke": "black", "stroke-width": "1",
        "stroke-linecap": "round", "stroke-linejoin": "round"
    })
    for index, stroke in enumerate(data["strokes"]):
        points = stroke["points"]
        stroke_width = stroke.get("width", 1)
        identifier = f"stroke_{index + 1}"
        if "widths" in stroke:
            left, right = stroke_outline(points, stroke["widths"])
            if right:
                path = svg_path(left, closed=False)
                for side, (width, end) in enumerate(((stroke["widths"][-1], right[-1]), (stroke["widths"][0], left[0]))):
                    radius = svg_numbers([width / 2])
                    path += f"A{radius} {radius} 0 0 0 {svg_numbers(end)}"
                    if side == 0 and len(right) > 1:
                        path += "L" + svg_numbers(value for point in reversed(right[:-1]) for value in point)
                ET.SubElement(group, "path", {"id": identifier, "d": path + "Z", "fill": "black", "stroke": "none"})
            else:
                ET.SubElement(group, "circle", {"id": identifier, "cx": str(points[0][0]), "cy": str(points[0][1]),
                                               "r": str(stroke["widths"][0] / 2), "fill": "black", "stroke": "none"})
        elif len(points) == 1:
            ET.SubElement(group, "circle", {
                "id": identifier, "cx": str(points[0][0]), "cy": str(points[0][1]),
                "r": str(stroke_width / 2), "fill": "black", "stroke": "none"
            })
        else:
            attributes = {"id": identifier, "d": svg_path(points)}
            if stroke_width != 1:
                attributes["stroke-width"] = str(stroke_width)
            ET.SubElement(group, "path", attributes)
    return ET.tostring(root, encoding="unicode")


def svg_path(points, closed=None):
    if closed is None:
        closed = len(points) > 2 and points[0] == points[-1]
    if closed and points[0] == points[-1]:
        points = points[:-1]
    return "M" + svg_numbers(value for point in points for value in point) + ("Z" if closed else "")


def svg_numbers(values):
    parts = [format(round(value, 3), ".3f").rstrip("0").rstrip(".") for value in values]
    return " ".join("0" if part == "-0" else part for part in parts)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_export(task, data):
    encoded = validate_annotation(data, task, complete=True)
    output = Path(task["output_path"])
    if output.exists():
        existing = json.loads(output.read_bytes())
        if not isinstance(existing, dict) or existing.get("task_id") != task["task_id"]:
            raise ValueError("This output file belongs to another task. Choose a separate SVG output folder.")
    return encoded, svg_text(data, validated=True).encode("utf-8")


def write_export(task, data, contents=None):
    encoded, svg = contents if contents is not None else prepare_export(task, data)
    output = Path(task["output_path"])
    atomic_write(output.with_suffix(".svg"), svg)
    atomic_write(output, encoded)
