from pathlib import Path
from typing import List, Dict
from tasks.base.strategy import TaskStrategy

class OBBStrategy(TaskStrategy):
    def get_required_folders(self) -> Dict[str, str]:
        return {
            "input": "Input Folder (Keyframes)",
            "output": "Output Folder (OBB JSONs)"
        }

    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        input_dir = Path(config_dict["input"])
        output_dir = Path(config_dict["output"])
        local_tasks = []

        if not input_dir.exists():
            return local_tasks

        for video_dir in input_dir.iterdir():
            if not video_dir.is_dir():
                continue
            
            video_name = video_dir.name
            for img_file in video_dir.glob("*"):
                if img_file.suffix.lower() not in ['.png', '.jpg', '.jpeg']:
                    continue
                
                frame = img_file.stem
                task_id = f"{video_name}_{frame}_{self.task_type}"
                
                local_tasks.append({
                    "task_id": task_id,
                    "video_name": video_name,
                    "frame": frame,
                    "image_path": str(img_file),
                    "output_path": str(output_dir / video_name / f"{frame}.json")
                })
        return local_tasks