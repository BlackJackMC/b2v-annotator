import math
from tasks.aabb.strategy import AABBStrategy


def valid_polygon(points):
    if not isinstance(points, (list, tuple)) or len(points) != 4 or any(not isinstance(p, dict) for p in points):
        return False
    if any(type(p.get(k)) not in (int, float) or not math.isfinite(p[k]) for p in points for k in ("x", "y")):
        return False
    turns = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        turns.append((b["x"] - a["x"]) * (c["y"] - b["y"]) - (b["y"] - a["y"]) * (c["x"] - b["x"]))
    return all(t > 1e-6 for t in turns) or all(t < -1e-6 for t in turns)


def encode_polygons(polygons):
    lines = []
    for points in polygons:
        if not valid_polygon(points):
            raise ValueError("Each polygon must have four distinct corners in perimeter order.")
        lines.append(", ".join(str(float(p[k])) for p in points for k in ("x", "y")) + ",###")
    return "\n".join(lines) + ("\n" if lines else "")


def decode_polygons(text):
    polygons = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 9 or fields[-1] != "###":
            raise ValueError("Invalid polygon text format.")
        points = [{"x": float(fields[i]), "y": float(fields[i + 1])} for i in range(0, 8, 2)]
        if not valid_polygon(points):
            raise ValueError("Invalid polygon corners.")
        polygons.append(points)
    return polygons


class PolygonStrategy(AABBStrategy):
    def get_required_folders(self):
        return {"keyframes": "Input Folder (Keyframes)", "obb_jsons": "OBB Ground Truth (JSONs)",
                "output": "Output Folder (Polygon TXT files)"}

    def scan_local_folder(self, config_dict):
        tasks = super().scan_local_folder(config_dict)
        from pathlib import Path
        for task in tasks:
            task["output_path"] = str(Path(task["output_path"]).with_suffix(".txt"))
        return tasks
