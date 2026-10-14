import queue
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from .base import Op, PumpStatus


class PumpHandle:
    """Obiekt widziany przez GUI. Wrzuca Op do kolejki, nic więcej."""
    def __init__(self, worker):
        self._w = worker
        self.capabilities = worker.driver.capabilities

    def submit(self, op: Op, **kw):
        self._w._queue.put((op, kw))


class PumpWorker(QObject):
    status = pyqtSignal(object)              # PumpStatus (poll)
    command_done = pyqtSignal(object, object)  # Op, PumpStatus
    error = pyqtSignal(str)

    def __init__(self, driver: object, poll_period: float = 0.5):
        super().__init__()
        self.driver = driver
        self._poll_period = poll_period
        self._queue: queue.Queue = queue.Queue()
        self._running = True

    @pyqtSlot()
    def run(self):
        while self._running:
            try:
                op, kw = self._queue.get(timeout=self._poll_period)
                try:
                    st = self.driver.execute(op, **kw)
                    self.command_done.emit(op, st)
                except Exception as e:
                    self.error.emit(f"{self.driver.capabilities.name}: {e}")
            except queue.Empty:
                if st := self.driver.poll():
                    self.status.emit(st)

    @pyqtSlot()
    def stop(self):
        self._running = False


def make_pump(port: str, model_key: str, parent=None) -> PumpHandle:
    """Fabryka: port + model -> wątek + driver + handle. Wywoływane z Session."""
    from core.transport.serial import SerialTransport
    from core.pump.registry import REGISTRY

    transport = SerialTransport(port)          # lub MockTransport w trybie demo
    driver = REGISTRY[model_key](transport)

    thread = QThread(parent)
    worker = PumpWorker(driver)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.started.connect(lambda: worker.error.emit(   # weryfikacja łączności
        worker.driver.query_version()))
    thread.start()
    return PumpHandle(worker)