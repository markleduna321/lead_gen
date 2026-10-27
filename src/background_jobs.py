"""Per-user guard for the dashboard's maintenance jobs.

Each (user, job) pair runs at most once at a time in this process, matching the
in-process worker model used by discovery. Jobs with a cooldown cannot restart
until it ends, which protects metered upstream quotas.
"""

import threading
import time


class JobUnavailable(Exception):
    def __init__(self, message, code, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _wait_text(seconds):
    minutes = max(1, -(-int(seconds) // 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class JobGuard:
    def __init__(self, jobs, clock=time.monotonic):
        """jobs maps a job id to (label, cooldown_seconds)."""
        self.jobs = dict(jobs)
        self._clock = clock
        self._lock = threading.Lock()
        self._running = set()
        self._ready_at = {}

    def start(self, user_id, job, target, cooldown_if=lambda result: True):
        """Runs target() on a daemon thread or raises JobUnavailable."""
        label, cooldown = self.jobs[job]
        key = (user_id, job)
        with self._lock:
            if key in self._running:
                raise JobUnavailable(f"{label} is already running.", "job_running")
            wait = self._ready_at.get(key, 0) - self._clock()
            if wait > 0:
                raise JobUnavailable(f"{label} can run again in {_wait_text(wait)}.", "job_cooldown",
                                     retry_after=int(-(-wait // 1)))
            self._ready_at.pop(key, None)
            self._running.add(key)

        def run():
            result, succeeded = None, False
            try:
                result = target()
                succeeded = True
            finally:
                with self._lock:
                    self._running.discard(key)
                    if cooldown and succeeded and cooldown_if(result):
                        self._ready_at[key] = self._clock() + cooldown

        try:
            threading.Thread(target=run, daemon=True, name=f"job-{job}-{user_id}").start()
        except BaseException:
            with self._lock:
                self._running.discard(key)
            raise

    def status(self, user_id):
        now = self._clock()
        with self._lock:
            result = {}
            for job in self.jobs:
                wait = self._ready_at.get((user_id, job), 0) - now
                result[job] = {"running": (user_id, job) in self._running,
                               "retry_after": int(-(-wait // 1)) if wait > 0 else 0}
            return result
