import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List
from PyQt6.QtGui import QImageReader
from tasks.base.strategy import TaskStrategy


class ClassifyStrategy(TaskStrategy):
    def __init__(self, task_type="classify"):
        super().__init__(task_type)
        self.errors = []
        self.image_cache = {}

    def get_required_folders(self) -> Dict[str, str]:
        return {
            "keyframes": "Input Folder (Keyframes)",
            "aabb_jsons": "Input Folder (AABB JSONs)",
            "output": "Output Folder (Classification JSONs)"
        }

    def image_info(self, path):
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self.image_cache.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        size = QImageReader(str(path)).size()
        if not size.isValid():
            raise ValueError("Cannot read image dimensions.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        info = (size.width(), size.height(), digest.hexdigest())
        self.image_cache[str(path)] = (signature, info)
        return info

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        keyframes_dir = Path(config_dict["keyframes"])
        aabb_dir = Path(config_dict["aabb_jsons"])
        output_dir = Path(config_dict["output"])
        self.errors = []
        tasks = []
        if not keyframes_dir.is_dir() or not aabb_dir.is_dir():
            self.errors.append("The keyframe and AABB folders must exist.")
            return tasks
        if any(folder.resolve().is_relative_to(output_dir.resolve()) for folder in (aabb_dir, keyframes_dir)):
            self.errors.append("Choose an output folder separate from the input folders and their parents.")
            return tasks
        image_folders = {}
        for json_file in sorted(aabb_dir.rglob("*.json")):
            if json_file.resolve().is_relative_to(output_dir.resolve()):
                continue
            try:
                relative = json_file.relative_to(aabb_dir)
                folder = keyframes_dir / relative.parent
                if folder not in image_folders:
                    image_folders[folder] = {}
                    for path in sorted(folder.glob("*")):
                        if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                            image_folders[folder].setdefault(path.stem, []).append(path)
                images = image_folders[folder].get(json_file.stem, [])
                if len(images) != 1:
                    raise ValueError("Expected one matching PNG, JPG or JPEG keyframe.")
                image_path = images[0]
                width, height, image_hash = self.image_info(image_path)
                image_name = image_path.relative_to(keyframes_dir).as_posix()
                data = json.loads(json_file.read_bytes())
                if not isinstance(data, dict) or not isinstance(data.get("boxes"), list):
                    raise ValueError("Expected a boxes list.")
                for index, box in enumerate(data["boxes"]):
                    try:
                        values = [box[key] for key in ("x", "y", "w", "h")]
                        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                            raise ValueError("Box coordinates must be finite numbers.")
                        x, y, w, h = values
                        if w <= 0 or h <= 0:
                            raise ValueError("Box dimensions must be positive.")
                        left, top = max(0, math.floor(x)), max(0, math.floor(y))
                        right, bottom = math.ceil(min(width, x + w)), math.ceil(min(height, y + h))
                        if right <= left or bottom <= top:
                            raise ValueError("Box is outside the image.")
                        identity = [image_name, image_hash, index, [float(v) for v in values]]
                        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
                        tasks.append({
                            "task_id": f"{digest}_{self.task_type}",
                            "label": f"{relative.with_suffix('').as_posix()} / region {index + 1}",
                            "image_path": str(image_path),
                            "image": image_name,
                            "image_sha256": image_hash,
                            "aabb_path": str(json_file),
                            "region_index": index,
                            "box": values,
                            "crop": [left, top, right - left, bottom - top],
                            "output_path": str(output_dir / relative.parent / json_file.stem / f"region_{index + 1:04d}.classify.json")
                        })
                    except (KeyError, TypeError, ValueError, OverflowError) as error:
                        self.errors.append(f"{relative}, region {index + 1}: {error}")
            except (OSError, KeyError, TypeError, ValueError) as error:
                self.errors.append(f"{json_file.name}: {error}")
        return tasks
