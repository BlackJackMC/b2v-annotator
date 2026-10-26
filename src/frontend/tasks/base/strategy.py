from abc import ABC, abstractmethod
from typing import List, Dict

class TaskStrategy(ABC):
    def __init__(self, task_type: str):
        self.task_type = task_type

    @abstractmethod
    def get_required_folders(self) -> Dict[str, str]:
        pass

    @abstractmethod
    def scan_local_folder(self, config_dict: Dict) -> List[Dict]:
        pass