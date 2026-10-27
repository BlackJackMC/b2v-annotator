import copy
import json
import math
import os
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


def new_annotation(task, user_id):
    return {
        "version": 2,
        "task_id": task["task_id"],
        "image": task["source"]["image"],
        "region": task["source"]["region_index"] + 1,
        "crop": list(task["source"]["crop_xywh"]),
        "annotator": user_id,
        "strokes": []
    }


def compact_annotation(data, task, user_id):
    result = new_annotation(task, user_id)
    if data.get("schema_version") == "board2vec.trace.v1":
        if data.get("task_id") != task["task_id"] or data["source"]["crop_xywh"] != result["crop"]:
            raise ValueError("This tracing belongs to a different image or region.")
    else:
        validate_annotation(data, task)
    for stroke in data["strokes"]:
        tool = stroke["tool"]
        result["strokes"].append({
            "tool": "freehand" if tool == "draw" else tool,
            "points": copy.deepcopy(stroke["points"])
        })
    validate_annotation(result, task)
    return result


def canvas_strokes(data):
    return [{"id": f"stroke_{index + 1}",
             "tool": "draw" if stroke["tool"] == "freehand" else stroke["tool"],
             "points": copy.deepcopy(stroke["points"])}
            for index, stroke in enumerate(data["strokes"])]


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
        if not isinstance(stroke.get("points"), list) or not stroke["points"]:
            raise ValueError("Each stroke needs at least one point.")
        for point in stroke["points"]:
            if not isinstance(point, list) or len(point) != 2 or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
                raise ValueError("Stroke coordinates must be finite x/y pairs.")
            if not (0 <= point[0] <= crop[2] and 0 <= point[1] <= crop[3]):
                raise ValueError("A stroke is outside the crop.")
    if len(json.dumps(data, allow_nan=False).encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("This annotation exceeds 8 MB. Use smaller AABB regions.")


def svg_text(data):
    validate_annotation(data)
    width, height = data["crop"][2:]
    root = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "width": str(width), "height": str(height),
        "viewBox": f"0 0 {width} {height}"
    })
    group = ET.SubElement(root, "g", {
        "fill": "none", "stroke": "black", "stroke-width": "1",
        "stroke-linecap": "round", "stroke-linejoin": "round"
    })
    for index, stroke in enumerate(data["strokes"]):
        points = stroke["points"]
        identifier = f"stroke_{index + 1}"
        if len(points) == 1:
            ET.SubElement(group, "circle", {
                "id": identifier, "cx": str(points[0][0]), "cy": str(points[0][1]),
                "r": "0.5", "fill": "black", "stroke": "none"
            })
        else:
            path = "M " + " L ".join(f"{x} {y}" for x, y in points)
            ET.SubElement(group, "path", {"id": identifier, "d": path})
    return ET.tostring(root, encoding="unicode")


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


def write_export(task, data, png_bytes):
    validate_annotation(data, task, complete=True)
    output = Path(task["output_path"])
    atomic_write(output.with_suffix(".svg"), svg_text(data).encode("utf-8"))
    atomic_write(output.with_suffix(".png"), png_bytes)
    atomic_write(output, json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
