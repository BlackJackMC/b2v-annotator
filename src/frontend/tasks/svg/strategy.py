import hashlib
import json
from pathlib import Path
from typing import Dict, List
from PyQt6.QtGui import QImageReader
from tasks.base.strategy import TaskStrategy
from tasks.svg.geometry import crop_bounds


def read_image_info(path, cache):
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = cache.get(str(path))
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
    cache[str(path)] = (signature, info)
    return info


def scan_regions(config_dict, image_cache, errors, task_type):
    keyframes_dir = Path(config_dict["keyframes"])
    aabb_dir = Path(config_dict["aabb_jsons"])
    output_dir = Path(config_dict["output"])
    dataset = keyframes_dir.parent.name if keyframes_dir.name.lower() in ("frames", "keyframes", "images") else keyframes_dir.name
    errors.clear()
    tasks = []
    if any(character.isspace() for character in str(output_dir.resolve())):
        errors.append("Choose an output folder without spaces; use underscores instead.")
        return tasks
    if not keyframes_dir.is_dir() or not aabb_dir.is_dir():
        errors.append("The keyframe and AABB input folders must exist.")
        return tasks
    if any(folder.resolve().is_relative_to(output_dir.resolve()) for folder in (aabb_dir, keyframes_dir)):
        errors.append("Choose an output folder separate from the input folders and their parents.")
        return tasks
    image_folders = {}
    for json_file in sorted(aabb_dir.rglob("*.json")):
        if json_file.resolve().is_relative_to(output_dir.resolve()):
            continue
        try:
            relative = json_file.relative_to(aabb_dir)
            output_parts = []
            for part in (*relative.parent.parts, json_file.stem):
                clean = "_".join(part.split())
                if clean != part:
                    clean += "_" + hashlib.sha256(part.encode()).hexdigest()[:8]
                output_parts.append(clean)
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
            info = read_image_info(image_path, image_cache)
            content = json_file.read_bytes()
            data = json.loads(content)
            if not isinstance(data, dict) or not isinstance(data.get("boxes"), list):
                raise ValueError("Expected an AABB JSON containing a boxes list.")
            aabb_hash = hashlib.sha256(content).hexdigest()
            name = "".join(character if character.isalnum() or character == "_" else
                           "--" if character == "/" else "-s" if character == " " else
                           "-h" if character == "-" else "-x" + character.encode().hex() + "-"
                           for character in f"{dataset}/{relative.with_suffix('').as_posix()}")
            for index, box in enumerate(data["boxes"]):
                try:
                    crop = crop_bounds(box, info["width"], info["height"])
                    source = {
                        "image": image_path.relative_to(keyframes_dir).as_posix(),
                        "image_sha256": info["sha256"],
                        "image_size": [info["width"], info["height"]],
                        "aabb": relative.as_posix(),
                        "aabb_sha256": aabb_hash,
                        "region_index": index,
                        "region_xywh": [float(box[k]) for k in ("x", "y", "w", "h")],
                        "crop_xywh": crop
                    }
                    tasks.append({
                        "task_id": f"{name}--region_{index + 1:04d}--{task_type}",
                        "label": f"{relative.with_suffix('').as_posix()} / region {index + 1}",
                        "image_path": str(image_path),
                        "aabb_path": str(json_file),
                        "output_path": str(output_dir.joinpath(*output_parts) / f"region_{index + 1:04d}.json"),
                        "source": source
                    })
                except (ValueError, KeyError, TypeError, OverflowError) as error:
                    errors.append(f"{relative}, region {index + 1}: {error}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{json_file.name}: {error}")
    return tasks


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
        return read_image_info(path, self.image_cache)

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        return scan_regions(config_dict, self.image_cache, self.errors, self.task_type)
