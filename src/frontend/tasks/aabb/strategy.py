from pathlib import Path
import json
from typing import List, Dict
from tasks.base.strategy import TaskStrategy

class AABBStrategy(TaskStrategy):
    def get_required_folders(self) -> Dict[str, str]:
        return {
            "keyframes": "Input Folder (Keyframes)",
            "obb_jsons": "OBB Ground Truth (JSONs)",
            "output": "Output Folder (AABB JSONs)"
        }

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        keyframes_dir = Path(config_dict["keyframes"])
        obb_dir = Path(config_dict["obb_jsons"])
        output_dir = Path(config_dict["output"])
        local_tasks = []

        if not keyframes_dir.exists() or not obb_dir.exists():
            return local_tasks

        for json_file in obb_dir.rglob("*.json"):
            video_name = json_file.parent.name
            frame = json_file.stem
            
            img_path = keyframes_dir / video_name / f"{frame}.png"
            if not img_path.exists():
                continue
                
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)
                    
                obb_coords = data.get("board_obb")
                if not obb_coords or len(obb_coords) != 4:
                    continue

                task_id = f"{video_name}_{frame}_{self.task_type}"
                
                local_tasks.append({
                    "task_id": task_id,
                    "video_name": video_name,
                    "frame": frame,
                    "image_path": str(img_path),
                    "crop_coords": obb_coords,
                    "output_path": str(output_dir / video_name / f"{frame}.json")
                })
            except Exception:
                pass
                
        return local_tasks