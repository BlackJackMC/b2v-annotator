import json
import math
from pathlib import Path, PurePosixPath
from PyQt6.QtGui import QTransform
from core.homography import homography_transform, project_boxes


def image_key(name):
    path = PurePosixPath(str(name).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Detection image names must be relative to the keyframe folder.")
    return path.with_suffix("").as_posix().casefold()


def read_detection_file(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError(f"Detection JSON exceeds 8 MB: {path.name}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return data


def index_detection_files(folder):
    root = Path(folder)
    result = {}
    for path in sorted(root.rglob("*.json")):
        data = read_detection_file(path)
        if "detections" not in data and "boxes" not in data:
            continue
        key = image_key(data.get("image", path.relative_to(root).as_posix()))
        if key in result:
            raise ValueError(f"Multiple detection JSONs match {key}. Keep one file per image.")
        result[key] = path
    if not result:
        raise ValueError("No detection or bounding-box JSONs were found in this folder.")
    return result


def import_boxes(path, image_name, image_size, homography, bounds):
    data = read_detection_file(Path(path))
    if "image" in data and image_key(data["image"]) != image_key(image_name):
        raise ValueError("This detection JSON belongs to a different image.")
    if data.get("homography") is not None:
        raise ValueError("Import expects boxes in original image coordinates.")
    if "width" in data or "height" in data:
        if [data.get("width"), data.get("height")] != list(image_size):
            raise ValueError("Detection dimensions do not match the original image. No boxes were imported.")
    detections = data.get("detections")
    boxes = data.get("boxes") if detections is None else []
    if detections is not None:
        if not isinstance(detections, list):
            raise ValueError("Expected a detections list.")
        for detection in detections:
            coords = detection.get("bbox_xyxy") if isinstance(detection, dict) else None
            if not isinstance(coords, list) or len(coords) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in coords):
                raise ValueError("Each detection must contain four finite bbox_xyxy coordinates.")
            x1, y1, x2, y2 = coords
            boxes.append({"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1})
    if not isinstance(boxes, list):
        raise ValueError("Expected boxes or detections in the JSON.")
    for box in boxes:
        if not isinstance(box, dict) or any(type(box.get(k)) not in (int, float) or not math.isfinite(box[k]) for k in ("x", "y", "w", "h")):
            raise ValueError("Boxes must contain finite x, y, w, h values.")
        if box["w"] <= 0 or box["h"] <= 0 or box["x"] < 0 or box["y"] < 0 or box["x"] + box["w"] > image_size[0] or box["y"] + box["h"] > image_size[1]:
            raise ValueError("A detection box is outside the original image or has invalid dimensions.")
    transform = homography_transform(homography) if homography is not None else QTransform()
    return project_boxes(boxes, transform, bounds)
