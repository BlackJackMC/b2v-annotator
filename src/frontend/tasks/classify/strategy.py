from typing import Dict, List
from tasks.base.strategy import TaskStrategy
from tasks.svg.strategy import read_image_info, scan_regions


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
        info = read_image_info(path, self.image_cache)
        return info["width"], info["height"], info["sha256"]

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        tasks = scan_regions(config_dict, self.image_cache, self.errors, self.task_type)
        for task in tasks:
            source = task.pop("source")
            task.update(image=source["image"], image_sha256=source["image_sha256"],
                        region_index=source["region_index"], box=source["region_xywh"], crop=source["crop_xywh"])
        return tasks
