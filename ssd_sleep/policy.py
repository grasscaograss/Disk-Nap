import time
from contextlib import contextmanager
from threading import Lock

from .macos import SleepError


class AccessGate:
    """Keep backing storage offline until a request needs it."""

    def __init__(self, backing, idle_seconds, *, clock=time.monotonic, sample_activity=None, check_access=None):
        self.backing = backing
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.sample_activity = sample_activity
        self.check_access = check_access
        self.last_access = clock()
        self.active = 0
        self.asleep = False
        self.system_sleep = False
        self.lock = Lock()

    @contextmanager
    def access(self):
        with self.lock:
            if self.system_sleep:
                raise SleepError("The Mac is preparing to sleep.")
            woke = self.asleep
            if woke:
                self.backing.wake()
                self.asleep = False
            if self.check_access is not None:
                self.check_access()
            self.active += 1
        try:
            yield woke
        finally:
            with self.lock:
                self.active -= 1
                self.last_access = self.clock()

    def _suspend_backing(self, reason):
        if self.active or self.asleep:
            return False
        try:
            self.backing.sleep(reason)
        finally:
            self.last_access = self.clock()
        self.asleep = True
        return True

    def idle_check(self):
        with self.lock:
            if self.system_sleep or self.active or self.asleep:
                return False
            if self.sample_activity is not None and self.sample_activity():
                self.last_access = self.clock()
            if self.clock() - self.last_access < self.idle_seconds:
                return False
            return self._suspend_backing("idle")

    def will_sleep(self):
        with self.lock:
            self.system_sleep = True
            return self._suspend_backing("system-sleep")

    def did_wake(self):
        with self.lock:
            self.system_sleep = False
            self.last_access = self.clock()
