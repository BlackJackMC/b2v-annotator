from pathlib import Path
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QTransform
from tasks.aabb.importer import read_detection_file, image_key
from tasks.polygon.strategy import valid_polygon
from core.homography import homography_transform


def project_polygons(polygons, transform, bounds):
    result = []
    for polygon in polygons:
        if not valid_polygon(polygon):
            raise ValueError("Expected four distinct polygon corners in perimeter order.")
        points = [transform.map(QPointF(p["x"], p["y"])) for p in polygon]
        if any(not bounds.adjusted(-1e-6, -1e-6, 1e-6, 1e-6).contains(p) for p in points):
            raise ValueError("A polygon lies outside the target image/board. No changes were applied.")
        projected = [{"x": max(bounds.left(), min(p.x(), bounds.right())),
                      "y": max(bounds.top(), min(p.y(), bounds.bottom()))} for p in points]
        if not valid_polygon(projected):
            raise ValueError("The projected polygon is invalid.")
        result.append(projected)
    return result


def import_polygons(path, image_name, image_size, homography, bounds):
    data = read_detection_file(Path(path))
    if "image" in data and image_key(data["image"]) != image_key(image_name):
        raise ValueError("This JSON belongs to another image.")
    if data.get("homography") is not None:
        raise ValueError("Import expects original image coordinates.")
    if ("width" in data or "height" in data) and [data.get("width"), data.get("height")] != image_size:
        raise ValueError("JSON dimensions do not match the image.")
    detections = data.get("detections", data.get("boxes"))
    if not isinstance(detections, list):
        raise ValueError("Expected a detections or boxes list.")
    polygons = []
    for detection in detections:
        if not isinstance(detection, dict):
            raise ValueError("Invalid detection.")
        raw = detection.get("polygon")
        if raw is not None:
            if not isinstance(raw, list) or len(raw) != 4 or any(not isinstance(p, list) or len(p) != 2 for p in raw):
                raise ValueError("Each imported polygon must have exactly four corners.")
            polygon = [{"x": p[0], "y": p[1]} for p in raw]
        else:
            box = detection.get("bbox_xyxy")
            if box is None:
                try:
                    box = [detection["x"], detection["y"], detection["x"] + detection["w"], detection["y"] + detection["h"]]
                except (KeyError, TypeError):
                    raise ValueError("Expected polygon, bbox_xyxy, or x/y/w/h.")
            if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) for v in box):
                raise ValueError("Invalid bounding box.")
            x1, y1, x2, y2 = box
            if x2 <= x1 or y2 <= y1:
                raise ValueError("Bounding boxes must have positive dimensions.")
            polygon = [{"x": x1, "y": y1}, {"x": x2, "y": y1}, {"x": x2, "y": y2}, {"x": x1, "y": y2}]
        polygons.append(polygon)
    polygons = project_polygons(polygons, QTransform(), QRectF(0, 0, *image_size))
    transform = homography_transform(homography) if homography else QTransform()
    return project_polygons(polygons, transform, bounds)
