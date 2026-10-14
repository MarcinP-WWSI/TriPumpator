from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot


class DeviceWorker(QObject):
    """Pracuje w osobnym wątku. Tu jest cała komunikacja ze sprzętem."""
    measurement = pyqtSignal(object)   # np. dict z odczytami
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, port: str):
        super().__init__()
        self.port = port
        self._running = True

    @pyqtSlot()
    def run(self):
        try:
            # zamiast tego: połączenie z realnym urządzeniem
            while self._running:
                data = {"temperature": 21.5, "voltage": 3.3}
                self.measurement.emit(data)
                self.thread().msleep(200)
        finally:
            self.finished.emit()

    @pyqtSlot()
    def stop(self):
        self._running = False


class Device:
    """Otoka: wątek + worker + referencja do okna."""

    def __init__(self, port: str, parent=None):
        self.port = port
        self.thread = QThread(parent)
        self.worker = DeviceWorker(port)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.window = None  # przypiszemy po utworzeniu UI

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.worker.stop()
        self.thread.quit()
        self.thread.wait(2000)