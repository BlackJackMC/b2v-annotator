"""Two FIFO background lanes: one for remote I/O, one for local I/O/CPU.

Never access a QWidget from these functions. The completed signal is delivered
on the GUI thread to the screen's @pyqtSlot receiver.
"""
from itertools import count
from queue import PriorityQueue
from PyQt6.QtCore import QThread, pyqtSignal


class BackgroundLane(QThread):
    completed = pyqtSignal(int, object, object)  # job ID, result, exception

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = PriorityQueue()
        self._counter = count()
        self._stopping = False

    def submit(self, job_id, function, priority=5):
        if self._stopping:
            raise RuntimeError("Background lane is shutting down")
        self._jobs.put((priority, next(self._counter), job_id, function))

    def stop_after_jobs(self):
        if not self._stopping:
            self._stopping = True
            self._jobs.put((100, next(self._counter), None, None))

    def run(self):
        while True:
            entry = self._jobs.get()
            _, _, job_id, function = entry
            if job_id is None:
                return
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, exc
            self.completed.emit(job_id, result, error)
