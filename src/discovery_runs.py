from collections.abc import Callable
from copy import deepcopy
import os
import threading
import uuid


class DiscoveryBusyError(RuntimeError):
    pass


class DiscoveryPartialSuccess(RuntimeError):
    """Raised when a search saved results but some sub-steps failed.

    Reporting this as an outright failure would hide work that genuinely
    succeeded: a single unreachable contact page should not make a run that
    saved dozens of leads look like it did nothing.
    """

    def __init__(self, messages, saved: int = 0):
        self.messages = list(messages)
        self.saved = saved
        super().__init__(" | ".join(self.messages))


DEFAULT_WORKER_LIMIT = 4
MAX_WORKER_LIMIT = 32
_worker_slots = None
_worker_slots_lock = threading.Lock()


def worker_slots():
    """Caps simultaneous discovery threads across every account.

    Without a cap, each signed-in account could start its own worker, so
    thread count and third-party API spend would grow with the user count.
    Resolved lazily because .env is loaded after this module is imported.
    """
    global _worker_slots
    with _worker_slots_lock:
        if _worker_slots is None:
            raw = (os.getenv("MAX_DISCOVERY_WORKERS") or "").strip()
            try:
                limit = int(raw) if raw else DEFAULT_WORKER_LIMIT
            except ValueError:
                limit = DEFAULT_WORKER_LIMIT
            limit = max(1, min(limit, MAX_WORKER_LIMIT))
            _worker_slots = threading.BoundedSemaphore(limit)
            _worker_slots.limit = limit
        return _worker_slots


def reset_worker_slots(limit: int | None = None):
    """Rebuilds the pool. Intended for tests and for configuration reloads."""
    global _worker_slots
    with _worker_slots_lock:
        if limit is None:
            _worker_slots = None
            return None
        _worker_slots = threading.BoundedSemaphore(limit)
        _worker_slots.limit = limit
        return _worker_slots


class LocalDiscoveryRuns:
    """Tracks background discovery runs, isolated per signed-in account.

    Runs are owned: the busy check, status lookups, and logs are all scoped to
    the owner so one account can neither block nor observe another's search.
    """

    # Bounded history. Sized for several accounts running searches concurrently.
    MAX_TRACKED_RUNS = 100

    def __init__(self, label: str = "local"):
        self._label = label
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

    def snapshot(self, run_id: str, owner: object = None) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["owner"] != owner:
                return None
            return deepcopy({key: value for key, value in run.items() if key != "owner"})

    def start(
        self,
        tasks: list[tuple[str, str, Callable[[], None]]],
        log: Callable[[str], None],
        owner: object = None,
        on_thread_start: Callable[[], None] | None = None,
        run_id: str | None = None,
        on_progress=None,
        on_finish=None,
    ) -> str:
        run_id = run_id or uuid.uuid4().hex
        slots = worker_slots()
        with self._lock:
            # The owner check comes first so a user who already has a search
            # running gets that specific message, and never consumes a slot.
            if any(
                run["state"] == "running" and run["owner"] == owner
                for run in self._runs.values()
            ):
                raise DiscoveryBusyError(
                    f"A {self._label} discovery is already running. "
                    "Wait for it to finish before starting another."
                )
            # Non-blocking, so holding the lock cannot stall other callers.
            if not slots.acquire(blocking=False):
                raise DiscoveryBusyError(
                    f"All {slots.limit} search workers are busy right now. "
                    "Wait for a running search to finish, then try again."
                )
            while len(self._runs) >= self.MAX_TRACKED_RUNS:
                # Never evict a run that is still working — with several
                # accounts active, the oldest entry may belong to a live search.
                stale = next(
                    (key for key, run in self._runs.items() if run["state"] != "running"),
                    None,
                )
                if stale is None:
                    break
                del self._runs[stale]
            self._runs[run_id] = {
                "id": run_id, "state": "running", "total": len(tasks),
                "completed": 0, "errors": [], "warnings": [], "saved": 0,
                "logs": [], "owner": owner,
                "successful_tasks": 0,
            }

        try:
            thread = threading.Thread(
                target=self._execute,
                args=(run_id, tasks, log, on_thread_start, slots, on_progress, on_finish),
                daemon=True,
            )
            thread.start()
        except Exception:
            slots.release()
            with self._lock:
                self._runs[run_id]["state"] = "failed"
                self._runs[run_id]["errors"].append("The discovery worker could not be started.")
            raise
        return run_id

    def _execute(
        self,
        run_id: str,
        tasks: list[tuple[str, str, Callable[[], None]]],
        log: Callable[[str], None],
        on_thread_start: Callable[[], None] | None = None,
        slots=None,
        on_progress=None,
        on_finish=None,
    ) -> None:
        self._context.run_id = run_id
        try:
            if on_thread_start is not None:
                on_thread_start()
            for source, industry, task in tasks:
                try:
                    result = task()
                except DiscoveryPartialSuccess as partial:
                    message = f"{source} / {industry}: {partial}"
                    with self._lock:
                        run = self._runs[run_id]
                        run["warnings"].append(message)
                        run["saved"] += partial.saved
                        run["successful_tasks"] += 1
                    log(f"[DISCOVERY WARNING] {message}")
                except Exception as error:
                    message = f"{source} / {industry}: {error}"
                    with self._lock:
                        self._runs[run_id]["errors"].append(message)
                    log(f"[DISCOVERY ERROR] {message}")
                else:
                    with self._lock:
                        self._runs[run_id]["successful_tasks"] += 1
                    if isinstance(result, int) and not isinstance(result, bool):
                        with self._lock:
                            self._runs[run_id]["saved"] += result
                finally:
                    with self._lock:
                        self._runs[run_id]["completed"] += 1
                    if on_progress:
                        on_progress(self.snapshot(run_id, self._runs[run_id]["owner"]))
        except Exception as error:
            log(f"[DISCOVERY CONTROL ERROR] Search stopped: {error}")
            with self._lock:
                self._runs[run_id]["errors"].append("Search control failed; check the saved results and credit settlement.")
        finally:
            with self._lock:
                run = self._runs[run_id]
                # Warnings describe partial work that was still saved, so they
                # must not turn a productive run into a failure. Likewise, if
                # one source failed but others saved leads, the operator should
                # get their results with the failures surfaced alongside.
                if run["errors"] and run["saved"] > 0:
                    run["warnings"].extend(run["errors"])
                    run["errors"] = []
                run["state"] = "failed" if run["errors"] else "succeeded"
            try:
                if on_finish:
                    on_finish(self.snapshot(run_id, self._runs[run_id]["owner"]))
            except Exception as error:
                log(f"[BILLING ERROR] Search settlement failed; reservation retained for reconciliation: {error}")
                with self._lock:
                    self._runs[run_id]["state"] = "failed"
                    self._runs[run_id]["errors"].append("Credit settlement needs operator reconciliation.")
            finally:
                del self._context.run_id
                if slots is not None:
                    slots.release()
