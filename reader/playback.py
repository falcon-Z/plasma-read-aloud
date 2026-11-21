"""Coordinate playback commands with the speech worker."""

from threading import Condition, Event


class ReadingJob(Event):
    def __init__(self):
        super().__init__()
        self._changed = Condition()
        self._count = 0
        self._index = 0
        self._interrupted = Event()
        self.paused = False

    def prepare(self, count):
        with self._changed:
            self._count = count

    def navigation(self):
        with self._changed:
            return self._index > 0, self._index + 1 < self._count

    def stop(self):
        with self._changed:
            self.set()
            self._interrupted.set()
            self._changed.notify()

    def toggle_pause(self):
        with self._changed:
            if self.is_set() or self._index >= self._count:
                return False
            self.paused = not self.paused
            self._interrupt()
            return True

    def seek(self, delta):
        with self._changed:
            index = self._index + delta
            if self.is_set() or not 0 <= index < self._count:
                return False
            self._index = index
            self._interrupt()
            return True

    def _interrupt(self):
        self._interrupted.set()
        self._interrupted = Event()
        self._changed.notify()

    def sentence(self):
        with self._changed:
            self._changed.wait_for(lambda: not self.paused or self.is_set())
            if not self.is_set() and self._index < self._count:
                return self._index, self._interrupted
            return None

    def complete(self, interrupted):
        with self._changed:
            if not interrupted.is_set():
                self._index += 1
