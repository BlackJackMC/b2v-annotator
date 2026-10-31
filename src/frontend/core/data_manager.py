import json
import hashlib
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional
from PyQt6.QtCore import QThread, QEvent
from core.api_client import APIClient, OfflineError, TaskConflictError
from tasks.obb.strategy import OBBStrategy
from tasks.aabb.strategy import AABBStrategy
from tasks.svg.strategy import SVGStrategy
from tasks.classify.strategy import ClassifyStrategy
from tasks.svg.document import atomic_write


class DataManager:
    def __init__(self, api_client: APIClient):
        self.api = api_client
        self._lock = threading.RLock()
        self.task_configs = {}
        self.cached_tasks = {}
        self.status_cache: Dict[str, Dict[str, Dict]] = {}
        self.pending_sync: Dict[str, Dict[str, Dict]] = {}
        self.local_locks: Dict[str, Optional[str]] = {}
        self.active_saves: set[str] = set()
        self.claiming_tasks: set[str] = set()
        self.held_tasks: set[str] = set()
        self.committed_saves: set[str] = set()  # protect this session's acknowledged exports

        self.strategies = {
            "obb": OBBStrategy("obb"),
            "aabb": AABBStrategy("aabb"),
            "svg": SVGStrategy("svg"),
            "classify": ClassifyStrategy("classify")
        }

    def get_required_folders(self, task_type: str) -> Dict[str, str]:
        strategy = self.strategies.get(task_type)
        return strategy.get_required_folders() if strategy else {}

    def set_task_config(self, task_type: str, config_dict: dict, scan: bool = True):
        for key, path_str in config_dict.items():
            if "output" in key.lower():
                Path(path_str).mkdir(parents=True, exist_ok=True)

        with self._lock:
            self.task_configs[task_type] = config_dict
            self.status_cache.setdefault(task_type, {})
            self.load_pending_queue(task_type)
        if scan:
            self.scan_local_folder(task_type)

    def pending_queue_path(self, task_type: str) -> Optional[Path]:
        config = self.task_configs.get(task_type, {})
        output_dir = config.get("output")
        return Path(output_dir) / f".pending_sync_{task_type}.json" if output_dir else None

    def load_pending_queue(self, task_type: str):
        with self._lock:
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
        with self._lock:
            path = self.pending_queue_path(task_type)
            if not path:
                return
            queue = self.pending_sync.get(task_type, {})
            try:
                if queue:
                    atomic_write(path, json.dumps(queue, ensure_ascii=False, indent=2).encode("utf-8"))
                elif path.exists():
                    path.unlink()
            except OSError as error:
                raise OSError(f"Could not persist the sync queue. Keep this session open and retry Save: {error}") from error

    def pending_count(self, task_type: str) -> int:
        with self._lock:
            return len(self.pending_sync.get(task_type, {}))

    def scan_local_folder(self, task_type: str):
        strategy = self.strategies.get(task_type)
        config = self.task_configs.get(task_type)

        if not strategy or not config:
            with self._lock:
                self.cached_tasks[task_type] = []
            return

        scanned = strategy.scan_local_folder(config)
        # A single output path must never belong to two distinct task IDs.
        destinations = {}
        for task in scanned:
            output = task.get("output_path")
            if not output:
                continue
            path_key = os.path.normcase(str(Path(output).resolve()))
            previous = destinations.setdefault(path_key, task["task_id"])
            if previous != task["task_id"]:
                raise ValueError(
                    f"Output JSON collision: {output} is shared by {previous} "
                    f"and {task['task_id']}. Fix the dataset/output naming before saving."
                )
        with self._lock:
            self.cached_tasks[task_type] = scanned

    def local_result_for_task(self, task_type: str, task: Dict) -> Optional[Dict]:
        with self._lock:
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

    def cleanup_available_json(self, task_type: str, task: Dict) -> bool:
        """Remove a valid stale export only when the server explicitly says available.

        Never touch pending work, a current claim, or a save in flight.
        This is intentionally NOT used when the network is offline/unknown.
        """
        tid = task["task_id"]
        with self._lock:
            st = self.status_cache.get(task_type, {}).get(tid, {})
            if (st.get("status") != "available" or tid in self.active_saves
                    or tid in self.committed_saves or tid in self.claiming_tasks
                    or tid in self.held_tasks
                    or tid in self.pending_sync.get(task_type, {})
                    or self.local_locks.get(task_type) == tid):
                return False
            output = task.get("output_path")
            if not output or self.local_result_for_task(task_type, task) is None:
                return False
            # Keep the recovery snapshot if a previous Save was not confirmed.
            digest = hashlib.sha256(tid.encode("utf-8")).hexdigest()
            folder = Path(self.task_configs[task_type]["output"]).resolve()
            recovery = (folder.parent / (".board2vec_state_" + task_type + "_" + folder.name)
                        / (digest + ".json"))
            if recovery.exists():
                return False
            Path(output).unlink(missing_ok=True)
            return True

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

    def refresh_statuses(self, task_type: str, refresh=True) -> Dict[str, Dict]:
        with self._lock:
            tasks = list(self.cached_tasks.get(task_type, []))
            self.status_cache.setdefault(task_type, {})
        if not tasks:
            if refresh and not self.api.online:
                try:
                    self.api.post("/tasks", {"task_ids": []})
                except (OfflineError, TaskConflictError):
                    pass
            return {}
        task_ids = [t["task_id"] for t in tasks]
        if refresh:
            try:
                remote_list = self.api.check_status(task_ids, raise_on_error=True, include_results=False)
                # Legacy backend omits tasks that have never been claimed.
                # A successful full response makes those missing IDs available.
                new_cache = {tid: {"task_id": tid, "status": "available", "locked_by": None}
                             for tid in task_ids}
                new_cache.update({item["task_id"]: item for item in remote_list})
                with self._lock:
                    cache = self.status_cache.setdefault(task_type, {})
                    cache.clear()
                    cache.update(new_cache)
                # Only an explicit server 'available' can invalidate local exports.
                # Do not delete on offline/unknown statuses or active/pending saves.
                for task in tasks:
                    if new_cache.get(task["task_id"], {}).get("status") == "available":
                        self.cleanup_available_json(task_type, task)
            except (OfflineError, TaskConflictError):
                # A failed/partial refresh cannot authenticate status. Never
                # manufacture Done from a file or reuse a stale Available row.
                # Unknown is deliberately excluded from automatic Next.
                with self._lock:
                    cache = self.status_cache.setdefault(task_type, {})
                    for tid in task_ids:
                        previous = cache.get(tid, {})
                        cache[tid] = {"task_id": tid, "status": "unknown",
                                      "locked_by": previous.get("locked_by"),
                                      "status_unverified": True}

        with self._lock:
            cache = self.status_cache.setdefault(task_type, {})
            effective = {tid: dict(st) for tid, st in cache.items()}
            for tid, entry in self.pending_sync.get(task_type, {}).items():
                effective[tid] = {
                    "task_id": tid,
                    "status": "unknown",
                    "locked_by": None,
                    "result": entry.get("result"),
                    "status_unverified": True,
                    "pending_sync": True
                }
            locked_tid = self.local_locks.get(task_type)
            if locked_tid and (not self.api.online or locked_tid not in effective or effective[locked_tid].get("status") != "done"):
                prev = effective.get(locked_tid, {})
                if prev.get("status") not in ("done", "processing"):
                    effective[locked_tid] = {
                        "task_id": locked_tid,
                        "status": "processing",
                        "locked_by": self.api.user_id,
                        "result": prev.get("result")
                    }
            return effective

    def flush_pending_sync(self, task_type: str) -> List[Dict]:
        with self._lock:
            queue = self.pending_sync.setdefault(task_type, {})
            if not queue:
                return []
            entries = [(tid, dict(entry)) for tid, entry in queue.items()]
            task_map = {t["task_id"]: t for t in self.cached_tasks.get(task_type, [])}

        conflicts = []
        changed = False

        try:
            remote_list = self.api.check_status([tid for tid, _ in entries], raise_on_error=True)
            status_map = {item["task_id"]: item for item in remote_list}
        except OfflineError:
            return []
        except TaskConflictError:
            status_map = {}

        def mark_synced(tid_key: str, synced_result, status_obj: Optional[Dict] = None):
            nonlocal changed
            with self._lock:
                q = self.pending_sync.setdefault(task_type, {})
                current = q.get(tid_key)
                if current and current.get("result") == synced_result:
                    q.pop(tid_key, None)
                    changed = True
                self.status_cache.setdefault(task_type, {})[tid_key] = status_obj or {
                    "task_id": tid_key, "status": "done", "locked_by": None, "result": synced_result
                }

        for tid, entry in entries:
            with self._lock:
                latest_entry = self.pending_sync.get(task_type, {}).get(tid)
            if not latest_entry:
                continue
            entry = dict(latest_entry)
            local_result = entry.get("result")
            base_result = entry.get("base_result")

            st = status_map.get(tid, {})
            server_status = st.get("status")
            server_result = st.get("result")

            if server_status == "done":
                if server_result == local_result:
                    mark_synced(tid, local_result, st)
                    continue
                if base_result is not None and server_result == base_result:
                    try:
                        claimed = self.api.reclaim_task(tid, task_type, raise_on_error=True)
                        if claimed.get("existing_result") != base_result:
                            server_result = claimed.get("existing_result")
                            self.api.unclaim_task(tid, raise_on_error=True)
                            raise TaskConflictError("The server result changed while acquiring the task.")
                        self.api.submit_task(tid, local_result, raise_on_error=True)
                        mark_synced(tid, local_result)
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

            if server_result is not None and server_result != base_result and server_result != local_result:
                conflicts.append({
                    "type": "done",
                    "task_id": tid,
                    "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                    "local_result": local_result,
                    "server_result": server_result
                })
                continue

            try:
                if server_status == "processing" and st.get("locked_by") == self.api.user_id:
                    try:
                        self.api.submit_task(tid, local_result, raise_on_error=True)
                    except TaskConflictError:
                        claimed = self.api.claim_task(tid, task_type, raise_on_error=True)
                        if claimed and claimed.get("existing_result") not in (None, base_result, local_result):
                            self.api.unclaim_task(tid, raise_on_error=False)
                            raise TaskConflictError("The server result changed while acquiring the task.")
                        self.api.submit_task(tid, local_result, raise_on_error=True)
                else:
                    claimed = self.api.claim_task(tid, task_type, raise_on_error=True)
                    if claimed and claimed.get("existing_result") not in (None, base_result, local_result):
                        self.api.unclaim_task(tid, raise_on_error=False)
                        conflicts.append({
                            "type": "done",
                            "task_id": tid,
                            "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                            "local_result": local_result,
                            "server_result": claimed.get("existing_result")
                        })
                        continue
                    self.api.submit_task(tid, local_result, raise_on_error=True)
                mark_synced(tid, local_result)
            except OfflineError:
                break
            except TaskConflictError:
                try:
                    recheck = self.api.check_status([tid], raise_on_error=True)
                except (OfflineError, TaskConflictError):
                    recheck = []
                if recheck and recheck[0].get("status") == "done":
                    if recheck[0].get("result") == local_result:
                        mark_synced(tid, local_result, recheck[0])
                    else:
                        conflicts.append({
                            "type": "done",
                            "task_id": tid,
                            "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                            "local_result": local_result,
                            "server_result": recheck[0].get("result")
                        })
                elif recheck and recheck[0].get("status") == "processing" and recheck[0].get("locked_by") not in (None, self.api.user_id):
                    conflicts.append({
                        "type": "locked",
                        "task_id": tid,
                        "task": task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path")}),
                        "locked_by": recheck[0].get("locked_by"),
                        "local_result": local_result
                    })

        if changed:
            self.save_pending_queue(task_type)
        return conflicts

    def save_task_local_first(self, task_type: str, task: Dict, points_or_result) -> List[Dict]:
        result_payload = points_or_result if isinstance(points_or_result, dict) else {"points": points_or_result}
        self.write_local_output(task_type, task, result_payload)

        tid = task["task_id"]
        with self._lock:
            queue = self.pending_sync.setdefault(task_type, {})
            base_result = queue[tid].get("base_result") if tid in queue else task.get("existing_result")
            queue[tid] = {
                "task_id": tid,
                "task_type": task_type,
                "user_id": self.api.user_id,
                "result": result_payload,
                "base_result": base_result,
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
        return []

    def sync_task_state(self, task_type: str, current_task: Optional[Dict] = None) -> List[Dict]:
        conflicts = self.flush_pending_sync(task_type)
        if current_task:
            tid = current_task["task_id"]
            with self._lock:
                is_pending = tid in self.pending_sync.get(task_type, {})
            if is_pending:
                conflicts.extend(self.flush_pending_sync(task_type))
            else:
                try:
                    claim_res = self.api.claim_task(tid, task_type, raise_on_error=True)
                    server_existing = claim_res.get("existing_result") if claim_res else None
                    with self._lock:
                        self.status_cache.setdefault(task_type, {})[tid] = {
                            "task_id": tid,
                            "status": "processing",
                            "locked_by": self.api.user_id,
                            "result": server_existing
                        }
                    if server_existing is not None and server_existing != current_task.get("existing_result"):
                        conflicts.append({
                            "type": "done_active",
                            "task_id": tid,
                            "task": current_task,
                            "server_result": server_existing,
                            "local_result": current_task.get("existing_result")
                        })
                except OfflineError:
                    pass
                except TaskConflictError:
                    try:
                        statuses = self.api.check_status([tid], raise_on_error=True)
                    except (OfflineError, TaskConflictError):
                        statuses = []
                    if statuses:
                        st = statuses[0]
                        with self._lock:
                            self.status_cache.setdefault(task_type, {})[tid] = st
                            if self.local_locks.get(task_type) == tid:
                                self.local_locks[task_type] = None
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
        with self._lock:
            queue = self.pending_sync.setdefault(task_type, {})
            queued_result = queue[tid].get("result") if tid in queue else None

        if use_local:
            local_result = conflict.get("local_result")
            if local_result is None:
                local_result = queued_result
            if local_result is not None:
                try:
                    try:
                        self.api.reclaim_task(tid, task_type, raise_on_error=True)
                    except TaskConflictError:
                        self.api.claim_task(tid, task_type, raise_on_error=True)
                    self.api.submit_task(tid, local_result, raise_on_error=True)
                    with self._lock:
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
            with self._lock:
                queue.pop(tid, None)
                self.save_pending_queue(task_type)
                self.status_cache.setdefault(task_type, {})[tid] = {
                    "task_id": tid, "status": "done", "locked_by": None, "result": server_result
                }
            if server_result is not None and task.get("output_path"):
                self.write_local_output(task_type, task, server_result)
            updated = dict(task)
            updated["existing_result"] = server_result
            return updated

    def get_free_task(self, task_type: str, refresh: bool = True, claim_remote: bool = True):
        with self._lock:
            tasks = list(self.cached_tasks.get(task_type, []))
        if not tasks:
            return None

        statuses = self.refresh_statuses(task_type, refresh=refresh if claim_remote else False)
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
            if not claim_remote or not self.api.online:
                if tid not in statuses and tid not in existing_results and os.path.isfile(task.get("output_path", "")):
                    continue
                task_copy = task.copy()
                task_copy["existing_result"] = existing_results.get(tid) or self.local_result_for_task(task_type, task)
                with self._lock:
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
                    with self._lock:
                        self.local_locks[task_type] = tid
                        self.status_cache.setdefault(task_type, {})[tid] = {
                            "task_id": tid,
                            "status": "processing",
                            "locked_by": self.api.user_id,
                            "result": task_copy["existing_result"]
                        }
                    return task_copy
            except OfflineError:
                if tid not in statuses and tid not in existing_results and os.path.isfile(task.get("output_path", "")):
                    continue
                task_copy = task.copy()
                task_copy["existing_result"] = existing_results.get(tid) or self.local_result_for_task(task_type, task)
                with self._lock:
                    self.local_locks[task_type] = tid
                return task_copy
            except TaskConflictError:
                continue
        return None

    def open_task(self, task_type: str, task: Dict, reopen: bool = False) -> Optional[Dict]:
        tid = task["task_id"]
        task_copy = task.copy()
        with self._lock:
            pending = self.pending_sync.get(task_type, {}).get(tid)
            cached_status = dict(self.status_cache.get(task_type, {}).get(tid, {}))

        try:
            res = self.api.claim_task(tid, task_type, raise_on_error=True)
            if res:
                task_copy["existing_result"] = (
                    pending.get("result") if pending else res.get("existing_result")
                )
                with self._lock:
                    self.local_locks[task_type] = tid
                    self.status_cache.setdefault(task_type, {})[tid] = {
                        "task_id": tid, "status": "processing", "locked_by": self.api.user_id,
                        "has_result": res.get("existing_result") is not None,
                    }
                return task_copy
        except OfflineError:
            # Never display an editable task unless MongoDB has acknowledged its lease.
            return None
        except TaskConflictError:
            return None
        return None

    def release_task(self, task_type: str, task: Optional[Dict]):
        if not task:
            return
        tid = task["task_id"]
        with self._lock:
            if self.local_locks.get(task_type) == tid:
                self.local_locks[task_type] = None
            if tid in self.pending_sync.get(task_type, {}):
                return
        self.api.unclaim_task(tid)

    def get_done_tasks(self, task_type: str, refresh=True):
        with self._lock:
            tasks = list(self.cached_tasks.get(task_type, []))
        if not tasks:
            return []

        statuses = self.refresh_statuses(task_type, refresh=refresh)
        done_ids = {tid for tid, st in statuses.items() if st.get("status") == "done"}

        result = []
        with self._lock:
            pending_ids = set(self.pending_sync.get(task_type, {}).keys())
        for t in tasks:
            tid = t["task_id"]
            if tid in done_ids and os.path.exists(t["image_path"]):
                task_copy = t.copy()
                task_copy["pending_sync"] = tid in pending_ids
                result.append(task_copy)
        return result

    def get_processing_tasks(self, task_type: str, refresh=True):
        with self._lock:
            tasks = list(self.cached_tasks.get(task_type, []))
        if not tasks:
            return []

        statuses = self.refresh_statuses(task_type, refresh=refresh)
        my_user = self.api.user_id

        processing_tasks = []
        for t in tasks:
            tid = t["task_id"]
            if tid in statuses and os.path.exists(t["image_path"]):
                st = statuses[tid]
                if st.get("status") == "expired" or (st.get("status") == "processing" and st.get("locked_by") == my_user):
                    task_copy = t.copy()
                    task_copy["existing_result"] = st.get("result") or self.local_result_for_task(task_type, t)
                    task_copy["expired"] = st.get("status") == "expired"
                    processing_tasks.append(task_copy)

        return processing_tasks


class TaskSyncWorker(QThread):
    def __init__(self, manager, task_type, task, scan, parent):
        super().__init__(parent)
        self.manager = manager
        self.task_type = task_type
        self.task = dict(task) if task else None
        self.scan = scan
        self.conflicts = []
        self.next_task = None
        self.error = None

    def run(self):
        try:
            if self.scan:
                self.manager.scan_local_folder(self.task_type)
            self.manager.refresh_statuses(self.task_type)
            # Read-only refresh. Only a user click or explicit Next may claim.
        except Exception as error:
            self.error = str(error)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Close and self.isRunning():
            event.ignore()
            self.parent().status_label.setText("Please wait for synchronization before closing.")
            return True
        return super().eventFilter(watched, event)
