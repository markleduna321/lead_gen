from collections.abc import Callable
from copy import deepcopy
import threading
import uuid


class DiscoveryBusyError(RuntimeError):
    pass


class LocalDiscoveryRuns:
    def __init__(self):
        self._runs = {}
        self._lock = threading.Lock()
        self._context = threading.local()

    def record_log(self, message: str) -> None:
        run_id = getattr(self._context, "run_id", None)
        with self._lock:
            if run_id in self._runs:
                logs = self._runs[run_id]["logs"]
                logs.append(message)
                del logs[:-50]

    def snapshot(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run else None

    def start(self, tasks: list[tuple[str, str, Callable[[], None]]], log: Callable[[str], None]) -> str:
        run_id = uuid.uuid4().hex
        with self._lock:
            if any(run["state"] == "running" for run in self._runs.values()):
                raise DiscoveryBusyError("A local discovery is already running. Wait for it to finish before starting another.")
            while len(self._runs) >= 20:
                del self._runs[next(iter(self._runs))]
            self._runs[run_id] = {
                "id": run_id, "state": "running", "total": len(tasks),
                "completed": 0, "errors": [], "logs": [],
            }
        try:
            thread = threading.Thread(target=self._execute, args=(run_id, tasks, log), daemon=True)
            thread.start()
        except Exception:
            with self._lock:
                self._runs[run_id]["state"] = "failed"
                self._runs[run_id]["errors"].append("The discovery worker could not be started.")
            raise
        return run_id

    def _execute(self, run_id: str, tasks: list[tuple[str, str, Callable[[], None]]], log: Callable[[str], None]) -> None:
        self._context.run_id = run_id
        try:
            for source, industry, task in tasks:
                try:
                    task()
                except Exception as error:
                    message = f"{source} / {industry}: {error}"
                    with self._lock:
                        self._runs[run_id]["errors"].append(message)
                    log(f"[DISCOVERY ERROR] {message}")
                finally:
                    with self._lock:
                        self._runs[run_id]["completed"] += 1
        finally:
            with self._lock:
                run = self._runs[run_id]
                run["state"] = "failed" if run["errors"] else "succeeded"
            del self._context.run_id
