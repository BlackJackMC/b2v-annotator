import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from core.api_client import APIClient, OfflineError, TaskConflictError
from tasks.obb.strategy import OBBStrategy
from tasks.aabb.strategy import AABBStrategy
from tasks.svg.strategy import SVGStrategy
from tasks.classify.strategy import ClassifyStrategy
from tasks.svg.document import atomic_write


class DataManager:
    def __init__(self, api_client: APIClient):
        self.api = api_client
        self.task_configs = {}
        self.cached_tasks = {}
        self.status_cache: Dict[str, Dict[str, Dict]] = {}
        self.pending_sync: Dict[str, Dict[str, Dict]] = {}
        self.local_locks: Dict[str, Optional[str]] = {}

        self.strategies = {
            "obb": OBBStrategy("obb"),
            "aabb": AABBStrategy("aabb"),
            "svg": SVGStrategy("svg"),
            "classify": ClassifyStrategy("classify")
        }

    def get_required_folders(self, task_type: str) -> Dict[str, str]:
        strategy = self.strategies.get(task_type)
        return strategy.get_required_folders() if strategy else {}

    def set_task_config(self, task_type: str, config_dict: dict):
        for key, path_str in config_dict.items():
            if "output" in key.lower():
                Path(path_str).mkdir(parents=True, exist_ok=True)

        self.task_configs[task_type] = config_dict
        self.status_cache.setdefault(task_type, {})
        self.load_pending_queue(task_type)
        self.scan_local_folder(task_type)

    def pending_queue_path(self, task_type: str) -> Optional[Path]:
        config = self.task_configs.get(task_type, {})
        output_dir = config.get("output")
        return Path(output_dir) / f".pending_sync_{task_type}.json" if output_dir else None

    def load_pending_queue(self, task_type: str):
        self.pending_sync.setdefault(task_type, {})
        path = self.pending_queue_path(task_type)
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_bytes())
            if isinstance(data, dict):
                self.pending_sync[task_type] = data
        except (OSError, ValueError):
            self.pending_sync[task_type] = {}

    def save_pending_queue(self, task_type: str):
        path = self.pending_queue_path(task_type)
        if not path:
            return
        queue = self.pending_sync.get(task_type, {})
        try:
            if queue:
                atomic_write(path, json.dumps(queue, ensure_ascii=False, indent=2).encode("utf-8"))
            elif path.exists():
                path.unlink()
        except OSError:
            pass

    def pending_count(self, task_type: str) -> int:
        return len(self.pending_sync.get(task_type, {}))

    def scan_local_folder(self, task_type: str):
        strategy = self.strategies.get(task_type)
        config = self.task_configs.get(task_type)

        if not strategy or not config:
            self.cached_tasks[task_type] = []
            return

        self.cached_tasks[task_type] = strategy.scan_local_folder(config)

    def local_result_for_task(self, task_type: str, task: Dict) -> Optional[Dict]:
        pending = self.pending_sync.get(task_type, {}).get(task["task_id"])
        if pending and "result" in pending:
            return pending["result"]
        output_path = task.get("output_path")
        if not output_path or not os.path.isfile(output_path):
            return None
        try:
            data = json.loads(Path(output_path).read_bytes())
            if not isinstance(data, dict):
                return None
            if task_type == "obb" and isinstance(data.get("board_obb"), list):
                return {"points": data["board_obb"]}
            if task_type == "aabb" and isinstance(data.get("boxes"), list):
                return {"points": data["boxes"]}
            if task_type == "classify" and data.get("label") in ("handwriting", "sketch"):
                return {"points": [{
                    "label": data["label"],
                    "image_sha256": data.get("image_sha256", task.get("image_sha256")),
                    "crop": data.get("crop", task.get("crop"))
                }]}
        except (OSError, ValueError):
            return None
        return None

    def write_local_output(self, task_type: str, task: Dict, result_payload: Dict):
        output_path = Path(task["output_path"])
        points = result_payload.get("points", []) if isinstance(result_payload, dict) else result_payload
        if task_type == "obb":
            payload = {"board_obb": points}
        elif task_type == "aabb":
            payload = {"boxes": points}
        elif task_type == "classify":
            saved = points[0] if isinstance(points, list) and points else (result_payload if isinstance(result_payload, dict) else {})
            payload = {
                "version": 1,
                "task_id": task["task_id"],
                "image": task["image"],
                "image_sha256": task["image_sha256"],
                "region": task["region_index"] + 1,
                "crop": task["crop"],
                "annotator": self.api.user_id,
                "label": saved.get("label")
            }
        else:
            return
        atomic_write(output_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def refresh_statuses(self, task_type: str) -> Dict[str, Dict]:
        tasks = self.cached_tasks.get(task_type, [])
        cache = self.status_cache.setdefault(task_type, {})
        if not tasks:
            return {}
        task_ids = [t["task_id"] for t in tasks]
        try:
            remote_list = self.api.check_status(task_ids, raise_on_error=True)
            new_cache = {}
            for item in remote_list:
                new_cache[item["task_id"]] = item
            cache.clear()
            cache.update(new_cache)
        except (OfflineError, TaskConflictError):
            for t in tasks:
                tid = t["task_id"]
                if tid not in cache:
                    local_res = self.local_result_for_task(task_type, t)
                    if local_res is not None:
                        cache[tid] = {"task_id": tid, "status": "done", "locked_by": None, "result": local_res}

        effective = {tid: dict(st) for tid, st in cache.items()}
        for tid, entry in self.pending_sync.get(task_type, {}).items():
            effective[tid] = {
                "task_id": tid,
                "status": "done",
                "locked_by": None,
                "result": entry.get("result"),
                "pending_sync": True
            }
        locked_tid = self.local_locks.get(task_type)
        if locked_tid and not self.api.online:
            prev = effective.get(locked_tid, {})
            effective[locked_tid] = {
                "task_id": locked_tid,
                "status": "processing",
                "locked_by": self.api.user_id,
                "result": prev.get("result")
            }
        return effective

    def flush_pending_sync(self, task_type: str) -> List[Dict]:
        queue = self.pending_sync.setdefault(task_type, {})
        if not queue:
            return []

        task_map = {t["task_id"]: t for t in self.cached_tasks.get(task_type, [])}
        conflicts = []
        changed = False

        for tid in list(queue.keys()):
            entry = queue[tid]
            local_result = entry.get("result")
            base_result = entry.get("base_result")
            try:
                statuses = self.api.check_status([tid], raise_on_error=True)
            except OfflineError:
                break
            except TaskConflictError:
                continue

            st = statuses[0] if statuses else {}
            server_status = st.get("status")
            server_result = st.get("result")

            if server_status == "done":
                if server_result == local_result:
                    queue.pop(tid, None)
                    self.status_cache.setdefault(task_type, {})[tid] = st
                    changed = True
                    continue
                if base_result is not None and server_result == base_result:
                    try:
                        self.api.reclaim_task(tid, task_type, raise_on_error=True)
                        self.api.submit_task(tid, local_result, raise_on_error=True)
                        queue.pop(tid, None)
                        self.status_cache.setdefault(task_type, {})[tid] = {
                            "task_id": tid, "status": "done", "locked_by": None, "result": local_result
                        }
                        changed = True
                        continue
                    except OfflineError:
                        break
                    except TaskConflictError:
                        pass
                conflicts.append({
                    "type": "done",
                    "task_id": tid,
                    "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                    "local_result": local_result,
                    "server_result": server_result
                })
                continue

            if server_status == "processing" and st.get("locked_by") not in (None, self.api.user_id):
                conflicts.append({
                    "type": "locked",
                    "task_id": tid,
                    "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                    "locked_by": st.get("locked_by"),
                    "local_result": local_result
                })
                continue

            try:
                self.api.claim_task(tid, task_type, raise_on_error=True)
                self.api.submit_task(tid, local_result, raise_on_error=True)
                queue.pop(tid, None)
                self.status_cache.setdefault(task_type, {})[tid] = {
                    "task_id": tid, "status": "done", "locked_by": None, "result": local_result
                }
                changed = True
            except OfflineError:
                break
            except TaskConflictError:
                try:
                    recheck = self.api.check_status([tid], raise_on_error=True)
                except (OfflineError, TaskConflictError):
                    recheck = []
                if recheck and recheck[0].get("status") == "done":
                    if recheck[0].get("result") == local_result:
                        queue.pop(tid, None)
                        self.status_cache.setdefault(task_type, {})[tid] = recheck[0]
                        changed = True
                    else:
                        conflicts.append({
                            "type": "done",
                            "task_id": tid,
                            "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                            "local_result": local_result,
                            "server_result": recheck[0].get("result")
                        })

        if changed:
            self.save_pending_queue(task_type)
        return conflicts

    def save_task_local_first(self, task_type: str, task: Dict, points_or_result) -> List[Dict]:
        result_payload = points_or_result if isinstance(points_or_result, dict) else {"points": points_or_result}
        self.write_local_output(task_type, task, result_payload)

        tid = task["task_id"]
        queue = self.pending_sync.setdefault(task_type, {})
        queue[tid] = {
            "task_id": tid,
            "task_type": task_type,
            "user_id": self.api.user_id,
            "result": result_payload,
            "base_result": task.get("existing_result"),
            "output_path": task.get("output_path")
        }
        self.save_pending_queue(task_type)
        if self.local_locks.get(task_type) == tid:
            self.local_locks[task_type] = None

        self.status_cache.setdefault(task_type, {})[tid] = {
            "task_id": tid,
            "status": "done",
            "locked_by": None,
            "result": result_payload
        }
        return self.flush_pending_sync(task_type)

    def sync_task_state(self, task_type: str, current_task: Optional[Dict] = None) -> List[Dict]:
        conflicts = self.flush_pending_sync(task_type)
        if current_task:
            tid = current_task["task_id"]
            if tid not in self.pending_sync.get(task_type, {}):
                try:
                    self.api.claim_task(tid, task_type, raise_on_error=True)
                except OfflineError:
                    pass
                except TaskConflictError:
                    try:
                        statuses = self.api.check_status([tid], raise_on_error=True)
                    except (OfflineError, TaskConflictError):
                        statuses = []
                    if statuses:
                        st = statuses[0]
                        if st.get("status") == "done" and st.get("result") != current_task.get("existing_result"):
                            conflicts.append({
                                "type": "done_active",
                                "task_id": tid,
                                "task": current_task,
                                "server_result": st.get("result"),
                                "local_result": current_task.get("existing_result")
                            })
                        elif st.get("status") == "processing" and st.get("locked_by") not in (None, self.api.user_id):
                            conflicts.append({
                                "type": "locked_active",
                                "task_id": tid,
                                "task": current_task,
                                "locked_by": st.get("locked_by")
                            })
        return conflicts

    def resolve_conflict(self, task_type: str, conflict: Dict, use_local: bool) -> Optional[Dict]:
        tid = conflict["task_id"]
        task = conflict.get("task") or {"task_id": tid}
        queue = self.pending_sync.setdefault(task_type, {})

        if use_local:
            local_result = conflict.get("local_result")
            if local_result is None and tid in queue:
                local_result = queue[tid].get("result")
            if local_result is not None:
                try:
                    try:
                        self.api.reclaim_task(tid, task_type, raise_on_error=True)
                    except TaskConflictError:
                        self.api.claim_task(tid, task_type, raise_on_error=True)
                    self.api.submit_task(tid, local_result, raise_on_error=True)
                    queue.pop(tid, None)
                    self.save_pending_queue(task_type)
                    self.status_cache.setdefault(task_type, {})[tid] = {
                        "task_id": tid, "status": "done", "locked_by": None, "result": local_result
                    }
                except (OfflineError, TaskConflictError):
                    pass
            return None
        else:
            server_result = conflict.get("server_result")
            queue.pop(tid, None)
            self.save_pending_queue(task_type)
            if server_result is not None and task.get("output_path"):
                self.write_local_output(task_type, task, server_result)
            self.status_cache.setdefault(task_type, {})[tid] = {
                "task_id": tid, "status": "done", "locked_by": None, "result": server_result
            }
            updated = dict(task)
            updated["existing_result"] = server_result
            return updated

    def get_free_task(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return None

        statuses = self.refresh_statuses(task_type)
        my_user = self.api.user_id
        unavailable_ids = set()
        existing_results = {}

        for tid, st in statuses.items():
            if st.get("status") == "done":
                unavailable_ids.add(tid)
            elif st.get("status") == "processing" and st.get("locked_by") != my_user:
                unavailable_ids.add(tid)
            elif st.get("status") == "processing" and st.get("locked_by") == my_user:
                existing_results[tid] = st.get("result")

        free_tasks = [t for t in tasks if t["task_id"] not in unavailable_ids and os.path.exists(t["image_path"])]

        for task in free_tasks:
            tid = task["task_id"]
            if not self.api.online:
                if tid not in existing_results and os.path.isfile(task.get("output_path", "")):
                    continue
                task_copy = task.copy()
                task_copy["existing_result"] = existing_results.get(tid) or self.local_result_for_task(task_type, task)
                self.local_locks[task_type] = tid
                return task_copy

            try:
                claim_res = self.api.claim_task(tid, task_type, raise_on_error=True)
                if claim_res:
                    task_copy = task.copy()
                    if tid in existing_results and existing_results[tid]:
                        task_copy["existing_result"] = existing_results[tid]
                    elif claim_res.get("existing_result"):
                        task_copy["existing_result"] = claim_res.get("existing_result")
                    else:
                        task_copy["existing_result"] = self.local_result_for_task(task_type, task)
                    self.local_locks[task_type] = tid
                    return task_copy
            except OfflineError:
                if tid not in existing_results and os.path.isfile(task.get("output_path", "")):
                    continue
                task_copy = task.copy()
                task_copy["existing_result"] = existing_results.get(tid) or self.local_result_for_task(task_type, task)
                self.local_locks[task_type] = tid
                return task_copy
            except TaskConflictError:
                continue
        return None

    def open_task(self, task_type: str, task: Dict, reopen: bool = False) -> Optional[Dict]:
        tid = task["task_id"]
        task_copy = task.copy()
        pending = self.pending_sync.get(task_type, {}).get(tid)

        try:
            if reopen and not pending:
                res = self.api.reclaim_task(tid, task_type, raise_on_error=True)
            else:
                try:
                    res = self.api.claim_task(tid, task_type, raise_on_error=True)
                except TaskConflictError:
                    res = self.api.reclaim_task(tid, task_type, raise_on_error=True)
            if res:
                task_copy["existing_result"] = (
                    pending.get("result") if pending else res.get("existing_result") or self.local_result_for_task(task_type, task)
                )
                self.local_locks[task_type] = tid
                return task_copy
        except OfflineError:
            cached_st = self.status_cache.get(task_type, {}).get(tid, {})
            task_copy["existing_result"] = (
                pending.get("result") if pending else cached_st.get("result") or self.local_result_for_task(task_type, task)
            )
            self.local_locks[task_type] = tid
            return task_copy
        except TaskConflictError:
            return None
        return None

    def release_task(self, task_type: str, task: Optional[Dict]):
        if not task:
            return
        tid = task["task_id"]
        if self.local_locks.get(task_type) == tid:
            self.local_locks[task_type] = None
        if tid in self.pending_sync.get(task_type, {}):
            return
        self.api.unclaim_task(tid)

    def get_done_tasks(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return []

        statuses = self.refresh_statuses(task_type)
        done_ids = {tid for tid, st in statuses.items() if st.get("status") == "done"}

        result = []
        for t in tasks:
            tid = t["task_id"]
            if tid in done_ids and os.path.exists(t["image_path"]):
                task_copy = t.copy()
                task_copy["pending_sync"] = tid in self.pending_sync.get(task_type, {})
                result.append(task_copy)
        return result

    def get_processing_tasks(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return []

        statuses = self.refresh_statuses(task_type)
        my_user = self.api.user_id

        processing_tasks = []
        for t in tasks:
            tid = t["task_id"]
            if tid in statuses and os.path.exists(t["image_path"]):
                st = statuses[tid]
                if st.get("status") == "processing" and st.get("locked_by") == my_user:
                    task_copy = t.copy()
                    task_copy["existing_result"] = st.get("result") or self.local_result_for_task(task_type, t)
                    processing_tasks.append(task_copy)

        return processing_tasks
