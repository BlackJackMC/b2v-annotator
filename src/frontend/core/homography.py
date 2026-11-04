import math
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QImage, QPainter, QPolygonF, QTransform


def board_homography(points, image_size):
    if len(points) != 4 or len(image_size) != 2:
        raise ValueError("Homography requires four board corners and an image size.")
    corners = [[float(p["x"]), float(p["y"])] if isinstance(p, dict) else list(p) for p in points]
    if any(len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in corners):
        raise ValueError("Board corners must contain finite coordinates.")
    if any(not 0 <= x <= image_size[0] or not 0 <= y <= image_size[1] for x, y in corners):
        raise ValueError("Board corners lie outside the source image.")
    center = [sum(p[axis] for p in corners) / 4 for axis in (0, 1)]
    corners.sort(key=lambda p: math.atan2(p[1] - center[1], p[0] - center[0]))
    start = min(range(4), key=lambda i: sum(corners[i]))
    corners = corners[start:] + corners[:start]
    for i in range(4):
        a, b, c = corners[i], corners[(i + 1) % 4], corners[(i + 2) % 4]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) <= 1e-6:
            raise ValueError("The board must be a convex quadrilateral with four distinct corners.")
    lengths = [math.dist(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    width, height = max(2, round(max(lengths[0], lengths[2]))), max(2, round(max(lengths[1], lengths[3])))
    if width * height > 32000000 or max(width, height) > 32768:
        raise ValueError("The rectified board is too large.")
    source = QPolygonF([QPointF(*p) for p in corners])
    target = QPolygonF([QPointF(0, 0), QPointF(width - 1, 0), QPointF(width - 1, height - 1), QPointF(0, height - 1)])
    transform = QTransform()
    if not QTransform.quadToQuad(source, target, transform) or not transform.isInvertible():
        raise ValueError("Cannot flatten this board quadrilateral.")
    return {"source_points": corners, "source_size": list(image_size), "size": [width, height],
            "matrix": [[transform.m11(), transform.m21(), transform.m31()],
                       [transform.m12(), transform.m22(), transform.m32()],
                       [transform.m13(), transform.m23(), transform.m33()]]}


def homography_transform(data):
    try:
        expected = board_homography(data["source_points"], data["source_size"])
        matrix = data["matrix"]
        if data["size"] != expected["size"] or len(matrix) != 3 or any(len(row) != 3 for row in matrix):
            raise ValueError("Invalid homography dimensions.")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or
               not math.isclose(v, expected["matrix"][r][c], rel_tol=1e-8, abs_tol=1e-8)
               for r, row in enumerate(matrix) for c, v in enumerate(row)):
            raise ValueError("Homography matrix does not match the board corners.")
        return QTransform(*(matrix[r][c] for c in range(3) for r in range(3)))
    except (KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError("Invalid homography metadata.") from error


def rectify_image(image, data):
    if data is None:
        return image
    transform = homography_transform(data)
    if [image.width(), image.height()] != data["source_size"]:
        raise ValueError("Homography source dimensions no longer match the image.")
    result = QImage(*data["size"], QImage.Format.Format_RGB32)
    if result.isNull():
        raise ValueError("Cannot allocate the rectified board image.")
    result.fill(Qt.GlobalColor.white)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(transform)
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    return result


def project_boxes(boxes, transform, bounds):
    result = []
    adjusted = 0
    for box in boxes:
        x, y, w, h = (box[key] for key in ("x", "y", "w", "h"))
        polygon = QPolygonF([QPointF(x, y), QPointF(x + w, y), QPointF(x + w, y + h), QPointF(x, y + h)])
        rect = transform.map(polygon).boundingRect()
        if any(not math.isfinite(v) for v in (rect.x(), rect.y(), rect.width(), rect.height())):
            raise ValueError("A box cannot be projected into this coordinate space.")
        clipped = rect.intersected(bounds)
        if clipped != rect:
            adjusted += 1
        if not clipped.isEmpty():
            result.append({"x": clipped.x(), "y": clipped.y(), "w": clipped.width(), "h": clipped.height()})
    return result, adjusted
