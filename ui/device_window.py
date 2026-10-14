from PyQt6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QLabel
from PyQt6.QtCore import pyqtSignal


class DeviceWindow(QMainWindow):
    command_requested = pyqtSignal(dict)

    def __init__(self, device_id: int, port: str):
        super().__init__()
        self.device_id = device_id
        self._close_callback = None
        self.setWindowTitle(f"Urządzenie {device_id} — {port}")
        central = QWidget()
        layout = QVBoxLayout(central)
        self.status_label = QLabel("Oczekiwanie na dane...")
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

    def update_readings(self, data: dict):
        self.status_label.setText(str(data))

    def show_error(self, msg: str):
        self.status_label.setText(f"BŁĄD: {msg}")

    def set_close_callback(self, cb):
        self._close_callback = cb

    def closeEvent(self, event):
        if self._close_callback:
            self._close_callback()
        event.accept()