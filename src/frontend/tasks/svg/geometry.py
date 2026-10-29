import math


class StrokeFilter:
    def __init__(self, point, timestamp):
        self.raw = list(point)
        self.point = list(point)
        self.timestamp = timestamp
        self.velocity = [0.0, 0.0]

    def sample(self, point, timestamp):
        elapsed = max(0.001, timestamp - self.timestamp)
        velocity_alpha = 1 / (1 + 1 / (2 * math.pi * elapsed))
        for axis in range(2):
            velocity = (point[axis] - self.raw[axis]) / elapsed
            self.velocity[axis] += velocity_alpha * (velocity - self.velocity[axis])
        cutoff = 8 + 0.04 * math.hypot(*self.velocity)
        alpha = 1 / (1 + 1 / (2 * math.pi * cutoff * elapsed))
        self.point = [previous + alpha * (current - previous)
                      for previous, current in zip(self.point, point)]
        self.raw = list(point)
        self.timestamp = timestamp
        return list(self.point)


def distance_squared(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def smooth_stroke(points):
    if len(points) < 3:
        return points
    lengths = [math.sqrt(distance_squared(a, b)) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    chord = math.sqrt(distance_squared(points[0], points[-1]))
    tolerance = min(2, max(0.75, chord * 0.015))
    if chord and total <= chord * 1.08 and all(
            segment_distance_squared(point, points[0], points[-1]) <= tolerance ** 2 for point in points):
        return [list(points[0]), list(points[-1])]
    if not total:
        return points
    spacing = max(1, total / 2048)
    samples = [list(points[0])]
    travelled, next_sample = 0.0, spacing
    for start, end, length in zip(points, points[1:], lengths):
        while length and next_sample < travelled + length:
            fraction = (next_sample - travelled) / length
            samples.append([a + (b - a) * fraction for a, b in zip(start, end)])
            next_sample += spacing
        travelled += length
    samples.append(list(points[-1]))
    closed = points[0] == points[-1]
    for _ in range(2):
        result = [] if closed else [samples[0]]
        for start, end in zip(samples, samples[1:]):
            result.extend([[0.75 * a + 0.25 * b for a, b in zip(start, end)],
                           [0.25 * a + 0.75 * b for a, b in zip(start, end)]])
        result.append(result[0] if closed else samples[-1])
        samples = result
    rounded = [[round(value, 3) for value in point] for point in samples]
    reduced = simplify_points(rounded, 0.02)
    return rounded if closed and len({tuple(point) for point in reduced}) < 3 else reduced


def point_widths(stroke):
    return stroke["widths"] if "widths" in stroke else [stroke.get("width", 1)] * len(stroke["points"])


def set_widths(stroke, widths):
    stroke["width"] = widths[0]
    stroke.pop("widths", None)
    if any(width != widths[0] for width in widths):
        stroke["widths"] = widths


def remap_widths(points, widths, target):
    if points is target or points == target:
        return list(widths)
    source_lengths = [0.0]
    target_lengths = [0.0]
    for values, lengths in ((points, source_lengths), (target, target_lengths)):
        for start, end in zip(values, values[1:]):
            lengths.append(lengths[-1] + math.sqrt(distance_squared(start, end)))
    index, result = 0, []
    for length in target_lengths:
        position = length * source_lengths[-1] / target_lengths[-1] if target_lengths[-1] else 0
        while index + 1 < len(points) - 1 and source_lengths[index + 1] < position:
            index += 1
        end = min(index + 1, len(points) - 1)
        span = source_lengths[end] - source_lengths[index]
        fraction = (position - source_lengths[index]) / span if span else 0
        result.append(round(widths[index] + (widths[end] - widths[index]) * fraction, 3))
    return result


def reshape_stroke(stroke, reshape):
    points = stroke["points"]
    if "widths" not in stroke:
        stroke["points"] = reshape(points)
        return
    widths = stroke["widths"]
    anchors = {0, len(points) - 1}
    for index in range(1, len(points)):
        if widths[index] != widths[index - 1]:
            anchors.update((index - 1, index))
    anchors = sorted(anchors)
    result, sizes = [points[0]], [widths[0]]
    for start, end in zip(anchors, anchors[1:]):
        section = points[start:end + 1]
        shaped = reshape(section)
        result.extend(shaped[1:])
        sizes.extend(remap_widths(section, widths[start:end + 1], shaped)[1:])
    stroke["points"] = result
    set_widths(stroke, sizes)


def stroke_outline(points, widths):
    samples = [(point, width / 2) for index, (point, width) in enumerate(zip(points, widths))
               if index == 0 or point != points[index - 1]]
    if len(samples) == 1:
        return [samples[0][0]], []
    normals = []
    for (start, _), (end, _) in zip(samples, samples[1:]):
        length = math.sqrt(distance_squared(start, end))
        normals.append((-(end[1] - start[1]) / length, (end[0] - start[0]) / length))
    left, right = [], []
    for index, (point, radius) in enumerate(samples):
        before = normals[max(0, index - 1)]
        after = normals[min(index, len(normals) - 1)]
        nx, ny = before[0] + after[0], before[1] + after[1]
        divisor = nx * after[0] + ny * after[1]
        offsets = [(nx / divisor, ny / divisor)] if divisor > 0.5 else [before, after]
        for x, y in offsets:
            left.append([point[0] + x * radius, point[1] + y * radius])
            right.append([point[0] - x * radius, point[1] - y * radius])
    return left, right


def segment_distance_squared(point, start, end):
    length = distance_squared(start, end)
    if length == 0:
        return distance_squared(point, start)
    t = ((point[0] - start[0]) * (end[0] - start[0]) +
         (point[1] - start[1]) * (end[1] - start[1])) / length
    t = max(0.0, min(1.0, t))
    closest = [start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1])]
    return distance_squared(point, closest)


def rounded_polyline(points, radius):
    if radius <= 0 or len(points) < 3:
        return points
    closed = points[0] == points[-1]
    anchors = points[:-1] if closed else points
    result = [] if closed else [list(anchors[0])]
    indices = range(len(anchors)) if closed else range(1, len(anchors) - 1)
    for index in indices:
        previous, point, following = anchors[index - 1], anchors[index], anchors[(index + 1) % len(anchors)]
        incoming = math.sqrt(distance_squared(previous, point))
        outgoing = math.sqrt(distance_squared(point, following))
        if incoming == 0 or outgoing == 0:
            continue
        distance = min(radius, incoming / 2, outgoing / 2)
        start = [point[axis] + (previous[axis] - point[axis]) * distance / incoming for axis in range(2)]
        end = [point[axis] + (following[axis] - point[axis]) * distance / outgoing for axis in range(2)]
        steps = max(2, min(16, math.ceil(distance * 2)))
        for step in range(steps + 1):
            t = step / steps
            sample = [round((1 - t) ** 2 * start[axis] + 2 * (1 - t) * t * point[axis] + t ** 2 * end[axis], 3)
                      for axis in range(2)]
            if not result or sample != result[-1]:
                result.append(sample)
    if not result:
        return points
    result.append(list(result[0] if closed else anchors[-1]))
    return result


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
