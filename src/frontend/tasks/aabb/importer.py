
import json
import math
from pathlib import Path, PurePosixPath

from PyQt6.QtGui import QTransform

from core.homography import homography_transform, project_boxes


MAX_JSON_SIZE = 8 * 1024 * 1024


def image_key(name):
    path = PurePosixPath(str(name).replace("\\", "/"))

    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "Image names must be relative to the keyframe folder."
        )

    return path.with_suffix("").as_posix().casefold()


def read_detection_file(path):
    path = Path(path)

    if path.stat().st_size > MAX_JSON_SIZE:
        raise ValueError(
            f"AABB JSON exceeds 8 MB: {path.name}"
        )

    data = json.loads(path.read_text(encoding="utf-8-sig"))

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected a JSON object: {path.name}"
        )

    return data


def index_detection_files(folder):
    root = Path(folder)
    result = {}

    for path in sorted(root.rglob("*.json")):
        data = read_detection_file(path)

        # Input must match the AABB output schema.
        if "boxes" not in data:
            continue

        if not isinstance(data["boxes"], list):
            raise ValueError(
                f"Expected a boxes list: {path.name}"
            )

        key = image_key(
            path.relative_to(root).as_posix()
        )

        if key in result:
            raise ValueError(
                f"Multiple AABB JSONs match {key}."
            )

        result[key] = path

    if not result:
        raise ValueError(
            "No AABB JSON files containing 'boxes' were found."
        )

    return result


def import_boxes(path, image_name, image_size, homography, bounds):
    """
    Import JSON using the same schema as AABB output:

    {
        "boxes": [
            {"x": 463.0, "y": 274.0,
             "w": 172.0, "h": 297.0}
        ]
    }

    Coordinates are in the original image space.
    """

    data = read_detection_file(path)
    boxes = data.get("boxes")

    if not isinstance(boxes, list):
        raise ValueError(
            "AABB JSON must contain a 'boxes' list."
        )

    width, height = image_size

    for i, box in enumerate(boxes):
        if not isinstance(box, dict):
            raise ValueError(
                f"Box {i} must be a JSON object."
            )

        for key in ("x", "y", "w", "h"):
            value = box.get(key)

            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise ValueError(
                    f"Box {i} has invalid '{key}'."
                )

        x = box["x"]
        y = box["y"]
        w = box["w"]
        h = box["h"]

        if (
            w <= 0
            or h <= 0
            or x < 0
            or y < 0
            or x + w > width
            or y + h > height
        ):
            raise ValueError(
                f"Box {i} is outside the original image "
                "or has invalid dimensions."
            )

    transform = (
        homography_transform(homography)
        if homography is not None
        else QTransform()
    )

    return project_boxes(boxes, transform, bounds)
