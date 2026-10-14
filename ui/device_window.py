"""Okno jednej pompy: UI z PumpCapabilities, mowa wyłącznie w Op.

Kontrakt:
    PumpWindow(pump_id, handle) — podpina się pod sygnały handle;
    detach() — rozłącza sygnały (używa Session przy usuwaniu pompy);
    closed  — sygnał dla Session, gdy użytkownik zamknie okno.

Wszystkie programowe modyfikacje kontrolek idą pod flagą _syncing,
żeby odświeżenie statusu nie strzelało komendą zwrotną do pompy.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.pump.base import Op, PumpCapabilities, PumpStatus, norm_unit


class PumpWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, pump_id: int, handle,
                 confirm_purge: Callable[[], bool] | None = None,
                 parent=None):
        super().__init__(parent)
        self._handle = handle
        self._syncing = False
        self._confirm_purge = confirm_purge or self._default_confirm_purge
        caps = handle.capabilities

        self.setWindowTitle(f"Pompa {pump_id} — {caps.name}")
        self._pump_id = pump_id

        self._build_ui(caps)
        self._attach()

    # ---------------------------------------------------------------- UI

    def _build_ui(self, caps: PumpCapabilities) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        # nagłówek: link | model | wersja fw
        head = QHBoxLayout()
        self.link_label = QLabel("OFFLINE")
        self.link_label.setStyleSheet("font-weight: bold;")
        self.model_label = QLabel(caps.name)
        self.version_label = QLabel("")
        head.addWidget(self.link_label)
        head.addWidget(self.model_label)
        head.addStretch()
        head.addWidget(self.version_label)
        root.addLayout(head)

        # banner alarmu (ukryty dopóki alarm aktywny)
        self.alarm_banner = QLabel("")
        self.alarm_banner.setStyleSheet(
            "background-color: #c62828; color: white; padding: 4px;"
            "font-weight: bold;")
        self.alarm_banner.hide()
        root.addWidget(self.alarm_banner)

        grid = QGridLayout()
        row = 0

        # kierunek — tylko gdy model ma więcej niż jeden
        self.dir_combo: QComboBox | None = None
        if len(caps.directions) > 1:
            grid.addWidget(QLabel("Kierunek:"), row, 0)
            self.dir_combo = QComboBox()
            self.dir_combo.addItems(caps.directions)
            self.dir_combo.currentTextChanged.connect(self._on_direction)
            grid.addWidget(self.dir_combo, row, 1)
            row += 1

        # rate: spin + jednostka + przycisk
        grid.addWidget(QLabel("Rate:"), row, 0)
        self.rate_spin = QDoubleSpinBox()
        lo = caps.rate_min
        hi = caps.rate_max if caps.rate_max != float("inf") else 99999.0
        self.rate_spin.setRange(lo, hi)
        self.rate_spin.setDecimals(3)
        self.rate_unit = QComboBox()
        self.rate_unit.addItems(caps.rate_units)
        self.set_rate_btn = QPushButton("Ustaw")
        self.set_rate_btn.clicked.connect(self._on_set_rate)
        grid.addWidget(self.rate_spin, row, 1)
        grid.addWidget(self.rate_unit, row, 2)
        grid.addWidget(self.set_rate_btn, row, 3)
        row += 1

        # target volume — opcjonalnie
        self.target_spin: QDoubleSpinBox | None = None
        if caps.target_volume:
            grid.addWidget(QLabel("Target vol.:"), row, 0)
            self.target_spin = QDoubleSpinBox()
            self.target_spin.setRange(0.0, 99999.0)
            self.target_spin.setDecimals(3)
            self.set_target_btn = QPushButton("Ustaw")
            self.set_target_btn.clicked.connect(self._on_set_target)
            grid.addWidget(self.target_spin, row, 1)
            grid.addWidget(self.set_target_btn, row, 3)
            row += 1

        # średnica — opcjonalnie
        self.dia_spin: QDoubleSpinBox | None = None
        if caps.diameter_setting:
            grid.addWidget(QLabel("Średnica [mm]:"), row, 0)
            self.dia_spin = QDoubleSpinBox()
            dlo, dhi = caps.diameter_range
            self.dia_spin.setRange(dlo, dhi)
            self.dia_spin.setDecimals(2)
            self.dia_spin.setValue(dhi / 2.0)
            self.set_dia_btn = QPushButton("Ustaw")
            self.set_dia_btn.clicked.connect(self._on_set_diameter)
            grid.addWidget(self.dia_spin, row, 1)
            grid.addWidget(self.set_dia_btn, row, 3)
            row += 1

        # dispensed — opcjonalnie
        self.disp_label: QLabel | None = None
        if caps.dispensed_query:
            grid.addWidget(QLabel("Dozowane:"), row, 0)
            self.disp_label = QLabel("—")
            self.clear_btn = QPushButton("Zeruj")
            self.clear_btn.clicked.connect(
                lambda: self._submit(Op.CLEAR_DISPENSED))
            grid.addWidget(self.disp_label, row, 1)
            grid.addWidget(self.clear_btn, row, 3)
            row += 1

        root.addLayout(grid)

        # sterowanie biegiem
        run = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.start_btn.clicked.connect(lambda: self._submit(Op.START))
        self.stop_btn.clicked.connect(lambda: self._submit(Op.STOP))
        run.addWidget(self.start_btn)
        run.addWidget(self.stop_btn)

        self.purge_btn: QPushButton | None = None
        if caps.purge:
            self.purge_btn = QPushButton("Purge")
            self.purge_btn.setToolTip(
                "Pełna prędkość! Stop kończy purge.")
            self.purge_btn.clicked.connect(self._on_purge)
            run.addWidget(self.purge_btn)
        run.addStretch()
        root.addLayout(run)

        # pasek błędu komend (UX walidacja / odrzucenia pompy)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e65100;")
        root.addWidget(self.error_label)

        self.setCentralWidget(central)

    # ------------------------------------------------------------ sygnały

    def _attach(self) -> None:
        h = self._handle
        h.status.connect(self.on_status)
        h.version.connect(self.on_version)
        h.error.connect(self.on_error)
        h.online.connect(self.on_online)
        h.offline.connect(self.on_offline)

    def detach(self) -> None:
        """Rozłącz sygnały handle — woła Session przed usunięciem pompy."""
        h = self._handle
        for sig, slot in ((h.status, self.on_status),
                          (h.version, self.on_version),
                          (h.error, self.on_error),
                          (h.online, self.on_online),
                          (h.offline, self.on_offline)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass                      # już rozłączone

    # --------------------------------------------------------- handlery GUI

    def _on_direction(self, text: str) -> None:
        if self._syncing:
            return
        self._submit(Op.SET_DIRECTION, direction=text)

    def _on_set_rate(self) -> None:
        value = self.rate_spin.value()
        unit = norm_unit(self.rate_unit.currentText())
        if (msg := self._handle.validate_rate(value, unit)) is not None:
            self._show_error(msg)
            return
        self._show_error("")
        self._submit(Op.SET_RATE, value=value, unit=unit)

    def _on_set_target(self) -> None:
        v = self.target_spin.value()
        self._submit(Op.SET_TARGET_VOLUME, volume=v)  # 0 = praca ciągła

    def _on_set_diameter(self) -> None:
        self._submit(Op.SET_DIAMETER, mm=self.dia_spin.value())

    def _on_purge(self) -> None:
        if self._confirm_purge():
            self._submit(Op.PURGE)

    def _default_confirm_purge(self) -> bool:
        return QMessageBox.question(
            self, "Purge",
            "Purge = pełna prędkość w bieżącym kierunku. Kontynuować?"
        ) == QMessageBox.StandardButton.Yes

    # --------------------------------------------------------- sloty statusu

    def on_status(self, st: PumpStatus) -> None:
        caps = self._handle.capabilities
        self._syncing = True
        try:
            if st.alarm:
                self.alarm_banner.setText(f"⚠ ALARM: {st.alarm}")
                self.alarm_banner.show()
            else:
                self.alarm_banner.hide()

            self.start_btn.setEnabled(not st.running)
            self.stop_btn.setEnabled(bool(st.running))

            if st.direction and self.dir_combo is not None:
                self.dir_combo.setCurrentText(st.direction)

            if st.dispensed is not None and self.disp_label is not None:
                unit = st.dispensed_unit or ""
                self.disp_label.setText(f"{st.dispensed:.3f} {unit}".strip())

            # model bez zmiany rate w biegu: blokuj kontrolki
            if not caps.set_rate_while_running:
                for w in (self.rate_spin, self.rate_unit,
                          self.set_rate_btn):
                    w.setEnabled(not st.running)
        finally:
            self._syncing = False

    def on_version(self, v: str) -> None:
        self.version_label.setText(v)

    def on_error(self, msg: str) -> None:
        self._show_error(msg)

    def on_online(self) -> None:
        self.link_label.setText("ONLINE")
        self.link_label.setStyleSheet(
            "font-weight: bold; color: #2e7d32;")

    def on_offline(self) -> None:
        self.link_label.setText("OFFLINE")
        self.link_label.setStyleSheet(
            "font-weight: bold; color: #b71c1c;")

    # ------------------------------------------------------------ pomocnicze

    def _submit(self, op: Op, **kw) -> None:
        if not self._handle.submit(op, **kw):
            self._show_error("pompa niedostępna (kolejka martwa)")

    def _show_error(self, msg: str) -> None:
        self.error_label.setText(msg)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        event.accept()


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    from core.pump.base import PumpCapabilities
    from core.pump.demo import DemoDriver

    class FakeHandle:
        """Handle bez wątku: te same sygnały, submit do listy."""
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Sigs(QObject):
            status = pyqtSignal(object)
            version = pyqtSignal(str)
            error = pyqtSignal(str)
            online = pyqtSignal()
            offline = pyqtSignal()

        def __init__(self, caps):
            self._s = FakeHandle._Sigs()
            self.capabilities = caps
            self.ops: list = []
            self.validate_result: str | None = None

        def __getattr__(self, name):          # status/version/... -> _s
            if name.startswith("__"):
                raise AttributeError(name)
            return getattr(self._s, name)

        def submit(self, op, **kw):
            self.ops.append((op, kw))
            return True

        def validate_rate(self, value, unit):
            return self.validate_result

    caps_full = DemoDriver.capabilities
    h = FakeHandle(caps_full)
    w = PumpWindow(1, h, confirm_purge=lambda: True)

    # 1. UI zbudowane z capabilities (model pełny)
    assert w.dir_combo is not None and w.purge_btn is not None
    assert w.target_spin is not None and w.dia_spin is not None
    assert w.disp_label is not None
    assert w.rate_unit.currentText() == "mL/h"

    # 2. start/stop
    w.start_btn.click()
    assert h.ops[-1] == (Op.START, {})
    w.stop_btn.click()
    assert h.ops[-1] == (Op.STOP, {})

    # 3. set rate (bez błędu walidacji)
    w.rate_spin.setValue(250.0)
    w.set_rate_btn.click()
    assert h.ops[-1] == (Op.SET_RATE, {"value": 250.0, "unit": "mL/h"})

    # 4. walidacja odrzuca -> błąd w labelu, brak submita
    n = len(h.ops)
    h.validate_result = "za duże"
    w.set_rate_btn.click()
    assert len(h.ops) == n and "za duże" in w.error_label.text()
    h.validate_result = None

    # 5. status: running, dispensed, blokada startu
    h.status.emit(PumpStatus(running=True, direction="INF",
                             dispensed=1.25, dispensed_unit="mL"))
    assert "1.250 mL" in w.disp_label.text()
    assert not w.start_btn.isEnabled() and w.stop_btn.isEnabled()

    # 6. alarm pokazuje się i znika
    h.status.emit(PumpStatus(running=False, alarm="S"))
    assert "ALARM" in w.alarm_banner.text() and not w.alarm_banner.isHidden()
    h.status.emit(PumpStatus(running=False))
    assert w.alarm_banner.isHidden()

    # 7. kierunek: klik użytkownika -> submit; sync z statusu -> cisza
    w.dir_combo.setCurrentText("WDR")
    assert h.ops[-1] == (Op.SET_DIRECTION, {"direction": "WDR"})
    n = len(h.ops)
    h.status.emit(PumpStatus(running=True, direction="WDR"))
    assert len(h.ops) == n and w.dir_combo.currentText() == "WDR"

    # 8. purge za potwierdzeniem i bez
    w.purge_btn.click()
    assert h.ops[-1] == (Op.PURGE, {})
    n = len(h.ops)
    w._confirm_purge = lambda: False
    w.purge_btn.click()
    assert len(h.ops) == n

    # 9. target / średnica / zerowanie
    w.target_spin.setValue(10.0)
    w.set_target_btn.click()
    assert h.ops[-1] == (Op.SET_TARGET_VOLUME, {"volume": 10.0})
    w.dia_spin.setValue(14.43)
    w.set_dia_btn.click()
    assert h.ops[-1] == (Op.SET_DIAMETER, {"mm": 14.43})
    w.clear_btn.click()
    assert h.ops[-1] == (Op.CLEAR_DISPENSED, {})

    # 10. wersja i link
    h.version.emit("DEMO-1.0")
    assert w.version_label.text() == "DEMO-1.0"
    h.online.emit()
    assert w.link_label.text() == "ONLINE"
    h.offline.emit()
    assert w.link_label.text() == "OFFLINE"

    # 11. model okrojony: brak opcjonalnych kontrolek
    caps_mini = PumpCapabilities(name="Mini", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0)
    w2 = PumpWindow(2, FakeHandle(caps_mini))
    assert w2.dir_combo is None and w2.purge_btn is None
    assert w2.target_spin is None and w2.dia_spin is None
    assert w2.disp_label is None

    # 12. model bez zmiany rate w biegu: kontrolki blokują się
    caps_lock = PumpCapabilities(name="Lock", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0,
                                 set_rate_while_running=False)
    h3 = FakeHandle(caps_lock)
    w3 = PumpWindow(3, h3)
    assert w3.rate_spin.isEnabled()
    h3.status.emit(PumpStatus(running=True))
    assert not w3.rate_spin.isEnabled() and not w3.set_rate_btn.isEnabled()

    # 13. detach: po rozłączeniu status nie rusza okna
    before = w.disp_label.text()
    w.detach()
    h.status.emit(PumpStatus(running=True, direction="INF",
                             dispensed=99.0, dispensed_unit="mL"))
    assert w.disp_label.text() == before

    # 14. closed przy zamknięciu
    got_close = []
    w3.closed.connect(lambda: got_close.append(True))
    w3.close()
    assert got_close == [True]

    print("device_window: OK")