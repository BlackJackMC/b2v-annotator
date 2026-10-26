import hashlib
import json
from pathlib import Path
from typing import Dict, List
from PyQt6.QtGui import QImageReader
from tasks.base.strategy import TaskStrategy
from tasks.svg.geometry import crop_bounds


class SVGStrategy(TaskStrategy):
    def __init__(self, task_type="svg"):
        super().__init__(task_type)
        self.errors = []
        self.image_cache = {}

    def get_required_folders(self) -> Dict[str, str]:
        return {
            "keyframes": "Input Folder (Keyframes)",
            "aabb_jsons": "AABB Ground Truth (JSONs)",
            "output": "Output Folder (SVG Traces)"
        }

    def image_info(self, path):
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self.image_cache.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        reader = QImageReader(str(path))
        size = reader.size()
        if not size.isValid():
            raise ValueError("Cannot read image dimensions.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        info = {"width": size.width(), "height": size.height(), "sha256": digest.hexdigest()}
        self.image_cache[str(path)] = (signature, info)
        return info

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        keyframes_dir = Path(config_dict["keyframes"])
        aabb_dir = Path(config_dict["aabb_jsons"])
        output_dir = Path(config_dict["output"])
        self.errors = []
        tasks = []
        if not keyframes_dir.is_dir() or not aabb_dir.is_dir():
            self.errors.append("The keyframe and AABB input folders must exist.")
            return tasks
        if aabb_dir.resolve().is_relative_to(output_dir.resolve()):
            self.errors.append("Choose an output folder separate from the AABB input folder and its parents.")
            return tasks
        image_folders = {}
        for json_file in sorted(aabb_dir.rglob("*.json")):
            if json_file.resolve().is_relative_to(output_dir.resolve()):
                continue
            try:
                relative = json_file.relative_to(aabb_dir)
                frame = json_file.stem
                folder = keyframes_dir / relative.parent
                if folder not in image_folders:
                    image_folders[folder] = {}
                    for path in sorted(folder.glob("*")):
                        if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                            image_folders[folder].setdefault(path.stem, []).append(path)
                images = image_folders[folder].get(frame, [])
                if len(images) != 1:
                    raise ValueError("Expected exactly one matching PNG, JPG or JPEG image.")
                image_path = images[0]
                info = self.image_info(image_path)
                content = json_file.read_bytes()
                data = json.loads(content)
                if not isinstance(data, dict) or not isinstance(data.get("boxes"), list):
                    raise ValueError("Expected an AABB JSON containing a boxes list.")
                for index, box in enumerate(data["boxes"]):
                    try:
                        crop = crop_bounds(box, info["width"], info["height"])
                        source = {
                            "image": image_path.relative_to(keyframes_dir).as_posix(),
                            "image_sha256": info["sha256"],
                            "image_size": [info["width"], info["height"]],
                            "aabb": relative.as_posix(),
                            "aabb_sha256": hashlib.sha256(content).hexdigest(),
                            "region_index": index,
                            "region_xywh": [float(box[k]) for k in ("x", "y", "w", "h")],
                            "crop_xywh": crop
                        }
                        identity = [source[k] for k in ("image", "image_sha256", "region_index", "region_xywh")]
                        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
                        name = f"region_{index + 1:04d}"
                        tasks.append({
                            "task_id": f"{digest}_svg",
                            "label": f"{relative.with_suffix('').as_posix()} / region {index + 1}",
                            "image_path": str(image_path),
                            "aabb_path": str(json_file),
                            "output_path": str(output_dir / relative.parent / frame / f"{name}.json"),
                            "source": source
                        })
                    except (ValueError, KeyError, TypeError) as error:
                        self.errors.append(f"{relative}, region {index + 1}: {error}")
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.errors.append(f"{json_file.name}: {error}")
        return tasks
