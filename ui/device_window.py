"""Okno jednej pompy: UI z PumpCapabilities, mowa wyłącznie w Op.

Kontrakt:
    PumpWindow(pump_id, handle) — podpina się pod sygnały handle;
    detach() — rozłącza sygnały (używa Session przy usuwaniu pompy);
    closed  — sygnał dla Session, gdy użytkownik zamknie okno.

Dozowanie: dwa liczniki obok siebie:
    disp_label — wartość Z POMPY (DIS; autorytatywna, w jednostkach
                 pompy, auto-skalowana do mL/µL)
    est_label  — SZACUNEK lokalny: ustawiony rate × czas od ostatniego
                 statusu (nie zna prędkości purge — zaniża przy purge;
                 zatrzymuje się przy stall, pompa też — rozjazd = sygnał)

Wszystkie programowe modyfikacje kontrolek idą pod flagą _syncing,
żeby odświeżenie statusu nie strzelało komendą zwrotną do pompy.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.pump.base import (
    BasePumpDriver, Op, PumpCapabilities, PumpStatus, fmt_dispensed,
    fmt_volume_ml, norm_unit,
)


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

        # szacunek dozowania: per kierunek, w mL
        self._est: dict[str, float] = {"INF": 0.0, "WDR": 0.0}
        self._last_ts: float | None = None

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

        # rate: spin + jednostka + przycisk + wartość ustawiona
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
        self.rate_cur = QLabel("—")
        grid.addWidget(self.rate_spin, row, 1)
        grid.addWidget(self.rate_unit, row, 2)
        grid.addWidget(self.set_rate_btn, row, 3)
        grid.addWidget(self.rate_cur, row, 4)
        row += 1

        # target volume — opcjonalnie; combo jednostek przy >1
        self.target_spin: QDoubleSpinBox | None = None
        self.target_unit: QComboBox | None = None
        self.target_cur: QLabel | None = None
        if caps.target_volume:
            grid.addWidget(QLabel("Target vol.:"), row, 0)
            self.target_spin = QDoubleSpinBox()
            self.target_spin.setRange(0.0, 99999.0)
            self.target_spin.setDecimals(3)
            if len(caps.volume_units) > 1:
                self.target_unit = QComboBox()
                self.target_unit.addItems(caps.volume_units)
            self.set_target_btn = QPushButton("Ustaw")
            self.set_target_btn.clicked.connect(self._on_set_target)
            self.target_cur = QLabel("—")
            grid.addWidget(self.target_spin, row, 1)
            if self.target_unit is not None:
                grid.addWidget(self.target_unit, row, 2)
            grid.addWidget(self.set_target_btn, row, 3)
            grid.addWidget(self.target_cur, row, 4)
            row += 1

        # średnica — opcjonalnie; też z wartością aktualną
        self.dia_spin: QDoubleSpinBox | None = None
        self.dia_cur: QLabel | None = None
        if caps.diameter_setting:
            grid.addWidget(QLabel("Średnica [mm]:"), row, 0)
            self.dia_spin = QDoubleSpinBox()
            dlo, dhi = caps.diameter_range
            self.dia_spin.setRange(dlo, dhi)
            self.dia_spin.setDecimals(2)
            self.dia_spin.setValue(dhi / 2.0)
            self.set_dia_btn = QPushButton("Ustaw")
            self.set_dia_btn.clicked.connect(self._on_set_diameter)
            self.dia_cur = QLabel("—")
            grid.addWidget(self.dia_spin, row, 1)
            grid.addWidget(self.set_dia_btn, row, 3)
            grid.addWidget(self.dia_cur, row, 4)
            row += 1

            # dozowanie — opcjonalnie; pompa i szacunek obok siebie,
            # z wierszem nagłówkowym nad kolumnami wartości
            self.disp_label: QLabel | None = None
            self.est_label: QLabel | None = None
            self.disp_hdr: QLabel | None = None
            self.est_hdr: QLabel | None = None
            if caps.dispensed_query:
                grid.addWidget(QLabel("Dozowane:"), row, 0)
                self.disp_hdr = QLabel("z pompy")
                self.est_hdr = QLabel("szacunek")
                for hdr in (self.disp_hdr, self.est_hdr):
                    hdr.setStyleSheet("color: #666;")
                grid.addWidget(self.disp_hdr, row, 1)
                grid.addWidget(self.est_hdr, row, 2)
                row += 1

                self.disp_label = QLabel("—")
                self.disp_label.setToolTip(
                    "Licznik pompy (autorytatywny; jednostki auto-skalowane)")
                self.est_label = QLabel("—")
                self.est_label.setToolTip(
                    "Szacunek lokalny: ustawiony rate × czas.\n"
                    "Nie zna prędkości purge (zaniża) i najazdów; rozjazd\n"
                    "z licznikiem pompy = stall / purge / zgubiona komenda.")
                self.clear_btn = QPushButton("Zeruj")
                self.clear_btn.clicked.connect(self._on_clear)
                grid.addWidget(self.disp_label, row, 1)
                grid.addWidget(self.est_label, row, 2)
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
        kw: dict = {"volume": self.target_spin.value()}
        if self.target_unit is not None:
            kw["unit"] = norm_unit(self.target_unit.currentText())
        self._submit(Op.SET_TARGET_VOLUME, **kw)

    def _on_set_diameter(self) -> None:
        # pompa kasuje liczniki przy zmianie średnicy — szacunek też
        self._reset_est()
        self._submit(Op.SET_DIAMETER, mm=self.dia_spin.value())

    def _on_clear(self) -> None:
        self._reset_est()
        self._submit(Op.CLEAR_DISPENSED)

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

            # szacunek: rate × dt od ostatniego statusu (tylko w biegu)
            now = time.monotonic()
            if self._last_ts is not None:
                dt = now - self._last_ts
                if st.running and st.rate is not None:
                    ml_h = BasePumpDriver.to_base_unit(
                        st.rate, st.rate_unit or "mL/h", "mL/h")
                    if ml_h is not None:
                        key = st.direction or "INF"
                        self._est[key] += ml_h * dt / 3600.0
            self._last_ts = now

            if st.dispensed is not None and self.disp_label is not None:
                self.disp_label.setText(
                    fmt_dispensed(st.dispensed, st.dispensed_unit))
                self.est_label.setText(
                    fmt_volume_ml(self._est[st.direction or "INF"]))

            # wartości aktualnie ustawione (z pompy, nie z GUI)
            if st.rate is not None:
                self.rate_cur.setText(
                    f"{st.rate:g} {st.rate_unit or ''}".strip())
            if st.target_volume is not None and self.target_cur is not None:
                self.target_cur.setText(
                    f"{st.target_volume:g} "
                    f"{st.target_volume_unit or ''}".strip())
            if st.diameter is not None and self.dia_cur is not None:
                self.dia_cur.setText(f"Ø{st.diameter:g} mm")

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

    def _reset_est(self) -> None:
        self._est = {"INF": 0.0, "WDR": 0.0}
        self._last_ts = None

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

        def __getattr__(self, name):
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
    assert w.disp_label is not None and w.est_label is not None
    assert w.rate_unit.currentText() == "mL/h"
    rate_items = {w.rate_unit.itemText(i)
                  for i in range(w.rate_unit.count())}
    assert {"mL/h", "µL/h", "µL/min"} <= rate_items
    assert w.target_unit is not None

    # 2. start/stop
    w.start_btn.click()
    assert h.ops[-1] == (Op.START, {})
    w.stop_btn.click()
    assert h.ops[-1] == (Op.STOP, {})

    # 3. set rate
    w.rate_spin.setValue(250.0)
    w.set_rate_btn.click()
    assert h.ops[-1] == (Op.SET_RATE, {"value": 250.0, "unit": "mL/h"})

    # 4. walidacja odrzuca -> błąd, brak submita
    n = len(h.ops)
    h.validate_result = "za duże"
    w.set_rate_btn.click()
    assert len(h.ops) == n and "za duże" in w.error_label.text()
    h.validate_result = None

    # 5. status: wartości aktualne + dozowanie pompy w mL, szacunek 0
    h.status.emit(PumpStatus(running=True, direction="INF",
                             rate=120.0, rate_unit="mL/h",
                             target_volume=0.5,
                             target_volume_unit="mL",
                             diameter=14.43,
                             dispensed=1.25, dispensed_unit="mL"))
    assert w.disp_label.text() == "1.250 mL"
    assert w.rate_cur.text() == "120 mL/h"
    assert w.target_cur.text() == "0.5 mL"
    assert w.dia_cur.text() == "Ø14.43 mm"
    assert w.est_label.text() == fmt_volume_ml(0.0)
    assert not w.start_btn.isEnabled() and w.stop_btn.isEnabled()

    # 6. auto-jednostki: pompa raportuje µL -> czytelny mikrolitr
    h.status.emit(PumpStatus(running=True, direction="INF",
                             rate=120.0, rate_unit="mL/h",
                             dispensed=50.0, dispensed_unit="µL"))
    assert w.disp_label.text() == "50.000 µL"

    # 7. szacunek akumuluje: symulujemy 60 s biegu 120 mL/h -> ~2 mL
    w._last_ts = time.monotonic() - 60.0
    h.status.emit(PumpStatus(running=True, direction="INF",
                             rate=120.0, rate_unit="mL/h",
                             dispensed=1.25, dispensed_unit="mL"))
    assert abs(w._est["INF"] - 2.0) < 0.01
    assert w.est_label.text() == "2.000 mL"

    # 8. szacunek w µL: rate 120000 µL/h = 120 mL/h; 60 s -> ~2 mL
    w._est["INF"] = 0.0
    w._last_ts = time.monotonic() - 60.0
    h.status.emit(PumpStatus(running=True, direction="INF",
                             rate=120000.0, rate_unit="µL/h",
                             dispensed=0.02, dispensed_unit="mL"))
    assert abs(w._est["INF"] - 2.0) < 0.01
    assert w.est_label.text() == "2.000 mL"
    assert w.disp_label.text() == "20.000 µL"   # 0.02 mL auto -> µL

    # 9. alarm pokazuje się i znika
    h.status.emit(PumpStatus(running=False, alarm="S"))
    assert "ALARM" in w.alarm_banner.text() and not w.alarm_banner.isHidden()
    h.status.emit(PumpStatus(running=False))
    assert w.alarm_banner.isHidden()

    # 10. kierunek: klik użytkownika -> submit; sync z statusu -> cisza;
    #     szacunek WDR osobny (INF nietknięty)
    w.dir_combo.setCurrentText("WDR")
    assert h.ops[-1] == (Op.SET_DIRECTION, {"direction": "WDR"})
    n = len(h.ops)
    est_inf = w._est["INF"]
    h.status.emit(PumpStatus(running=True, direction="WDR",
                             rate=60.0, rate_unit="mL/h",
                             dispensed=0.3, dispensed_unit="mL"))
    assert len(h.ops) == n and w.dir_combo.currentText() == "WDR"
    assert w._est["INF"] == est_inf and w.est_label.text() == \
        fmt_volume_ml(w._est["WDR"])

    # 11. purge za potwierdzeniem i bez
    w.purge_btn.click()
    assert h.ops[-1] == (Op.PURGE, {})
    n = len(h.ops)
    w._confirm_purge = lambda: False
    w.purge_btn.click()
    assert len(h.ops) == n

    # 12. clear i diameter resetują szacunek (pompa kasuje liczniki)
    w._est = {"INF": 5.0, "WDR": 1.0}
    w.clear_btn.click()
    assert h.ops[-1] == (Op.CLEAR_DISPENSED, {})
    assert w._est == {"INF": 0.0, "WDR": 0.0} and w._last_ts is None
    w._est = {"INF": 5.0, "WDR": 1.0}
    w.dia_spin.setValue(14.43)
    w.set_dia_btn.click()
    assert h.ops[-1] == (Op.SET_DIAMETER, {"mm": 14.43})
    assert w._est == {"INF": 0.0, "WDR": 0.0}

    # 13. target z jednostką
    w.target_spin.setValue(500.0)
    w.target_unit.setCurrentText("µL")
    w.set_target_btn.click()
    assert h.ops[-1] == (Op.SET_TARGET_VOLUME,
                         {"volume": 500.0, "unit": "µL"})

    # 14. wersja i link
    h.version.emit("DEMO-1.0")
    assert w.version_label.text() == "DEMO-1.0"
    h.online.emit()
    assert w.link_label.text() == "ONLINE"
    h.offline.emit()
    assert w.link_label.text() == "OFFLINE"

    # 15. model okrojony: brak opcjonalnych kontrolek
    caps_mini = PumpCapabilities(name="Mini", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0)
    w2 = PumpWindow(2, FakeHandle(caps_mini))
    assert w2.dir_combo is None and w2.purge_btn is None
    assert w2.target_spin is None and w2.dia_spin is None
    assert w2.disp_label is None and w2.est_label is None

    # 16. model bez zmiany rate w biegu: kontrolki blokują się
    caps_lock = PumpCapabilities(name="Lock", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0,
                                 set_rate_while_running=False)
    h3 = FakeHandle(caps_lock)
    w3 = PumpWindow(3, h3)
    assert w3.rate_spin.isEnabled()
    h3.status.emit(PumpStatus(running=True))
    assert not w3.rate_spin.isEnabled() and not w3.set_rate_btn.isEnabled()

    # 17. detach: po rozłączeniu status nie rusza okna
    before = w.disp_label.text()
    w.detach()
    h.status.emit(PumpStatus(running=True, direction="INF",
                             dispensed=99.0, dispensed_unit="mL"))
    assert w.disp_label.text() == before

    # 18. closed przy zamknięciu
    got_close = []
    w3.closed.connect(lambda: got_close.append(True))
    w3.close()
    assert got_close == [True]

    print("device_window: OK")