from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from core.device import Device
from ui.device_window import DeviceWindow
from ui.main_panel import MainPanel


class Session(QObject):
    """Zarządza urządzeniami i oknami. Jeden obiekt na całą aplikację."""
    global_state_changed = pyqtSignal(dict)

    def __init__(self, port: str | None, addresses=(1, 2, 3), parent=None):
        super().__init__(parent)
        if port:
            factory = lambda: SerialTransport(port)
        else:
            factory = lambda: MockTransport(addresses)   # tryb demo

        self.worker = PumpNetworkWorker(factory, addresses)
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.windows = {}
        self.worker.poll_result.connect(self._route)
        self.worker.alarm.connect(self._route_alarm)

    def start(self):
        for addr in ...:                      # po adresach z okna wyboru
            win = DeviceWindow(device_id=addr)
            self.windows[addr] = win
            win.command_requested.connect(
                lambda text, a=addr: self.worker.enqueue(a, text))
            win.show()
        self.thread.start()

    def _route(self, parsed):
        if (win := self.windows.get(parsed["address"])):
            win.update_status(parsed)

    def _route_alarm(self, addr, msg):
        if (win := self.windows.get(addr)):
            win.show_alarm(msg)

    def discover(self, max_addr: int = 9):
        found = set()
        collect = lambda parsed: found.add(parsed["address"])
        self.worker.poll_result.connect(collect)
        for a in range(max_addr + 1):
            self.worker.enqueue(a, "")
        QTimer.singleShot(3000, lambda: (
            self.worker.poll_result.disconnect(collect),
            self.selector.show_detected(sorted(found))))

    def start(self, ports: list[str]):
        """Wołane z okna wyboru po zatwierdzeniu liczby urządzeń."""
        for i, port in enumerate(ports, start=1):
            dev = Device(port)
            dev.window = DeviceWindow(device_id=i, port=port)
            dev.window.set_close_callback(lambda d=dev: self.remove_device(d))

            # okno -> logika
            dev.window.command_requested.connect(
                lambda cmd, d=dev: self.handle_command(d, cmd))

            # logika -> okno
            dev.worker.measurement.connect(dev.window.update_readings)
            dev.worker.error.connect(dev.window.show_error)

            self.devices.append(dev)
            dev.window.show()
            dev.start()

        self.main_panel = MainPanel(num_devices=len(ports))
        self.main_panel.global_command.connect(self.handle_global_command)
        self.main_panel.show()

    def handle_command(self, device: Device, cmd: dict):
        # np. parsowanie i wysyłka do worker.device
        pass

    def handle_global_command(self, cmd: dict):
        # np. "start wszystkich", "zapis do pliku", "zakończ"
        for dev in self.devices:
            pass  # rozdziel komendę do urządzeń
        self.global_state_changed.emit(cmd)

    def remove_device(self, device: Device):
        device.shutdown()
        if device.window:
            device.window.close()
        if device in self.devices:
            self.devices.remove(device)

    def shutdown_all(self):
        for dev in list(self.devices):
            self.remove_device(dev)
        if self.main_panel:
            self.main_panel.close()