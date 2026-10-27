import math


def distance_squared(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def segment_distance_squared(point, start, end):
    length = distance_squared(start, end)
    if length == 0:
        return distance_squared(point, start)
    t = ((point[0] - start[0]) * (end[0] - start[0]) +
         (point[1] - start[1]) * (end[1] - start[1])) / length
    t = max(0.0, min(1.0, t))
    closest = [start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1])]
    return distance_squared(point, closest)


def simplify_points(points, tolerance=0.35):
    if len(points) < 3 or tolerance <= 0:
        return [list(p) for p in points]
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    remaining = 250000
    while pending:
        first, last = pending.pop()
        remaining -= last - first - 1
        if remaining < 0:
            return [list(p) for p in points]
        maximum = tolerance * tolerance
        furthest = None
        for index in range(first + 1, last):
            distance = segment_distance_squared(points[index], points[first], points[last])
            if distance > maximum:
                maximum = distance
                furthest = index
        if furthest is not None:
            keep.add(furthest)
            pending.extend([(first, furthest), (furthest, last)])
    return [list(points[index]) for index in sorted(keep)]


def crop_bounds(box, width, height):
    values = [box[key] for key in ("x", "y", "w", "h")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("Region coordinates must be finite numbers.")
    x, y, w, h = values
    if w <= 0 or h <= 0:
        raise ValueError("Region width and height must be positive.")
    left = max(0, math.floor(x))
    top = max(0, math.floor(y))
    right = min(width, math.ceil(x + w))
    bottom = min(height, math.ceil(y + h))
    if right <= left or bottom <= top:
        raise ValueError("Region is outside the image.")
    return [left, top, right - left, bottom - top]
