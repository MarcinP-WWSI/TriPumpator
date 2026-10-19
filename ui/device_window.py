"""Okno jednej pompy: UI z PumpCapabilities, mowa wyłącznie w Op.

Kontrakt:
    PumpWindow(pump_id, handle) — podpina się pod sygnały handle;
    detach() — rozłącza sygnały (używa Session przy usuwaniu pompy);
    closed  — sygnał dla Session, gdy użytkownik zamknie okno.

Target: tryb Ciągły/Objętość/Czas budowany z capabilities. Combobox
trybu jest WYŁĄCZNIE wejściem użytkownika (poll go nie rusza — nie
wyżyma wyboru przed kliknięciem "Ustaw"); stan pompy pokazuje
etykieta "Aktualnie" (jak rate_cur). Ustawienie jednego targetu
czyści drugi (semantyka Quick Start: volume ALBO czas).

Bieg blokuje parametry kontekstowe: średnicę, siłę i target —
realne pompy i tak odrzucają te komendy w biegu (AL-1000: tylko na
postoju; P11: tylko z ekranu Quick Start), blokada zamiast błędu.

Siatka 6 kolumn: 0=nazwa, 1-3=wejścia, 4=przycisk, 5=Aktualnie —
przyciski i wartości bieżące wyrównane w pionie dla wszystkich wierszy.

Dozowanie: licznik pompy (autorytatywny) + szacunek lokalny obok.
Programowe modyfikacje kontrolek pod flagą _syncing.
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
    fmt_duration, fmt_volume_ml, norm_unit, time_to_seconds,
)

MODE_CONT = "Ciągły"
MODE_VOL = "Objętość"
MODE_TIME = "Czas"
TIME_UNITS = ("s", "min", "h")

FORCE_HINT = (
    "Zalecenia producenta (Harvard):\n"
    "plastik ≤20 ml ~50%, >20 ml ~100%\n"
    "szkło/szkło ≤20 ml 20–30%, >20 ml 30–50%\n"
    "szkło/plastik ≤250 µl 30%, ≤5 ml 50%\n"
    "stal 100%. Minimum ~20% (tarcie mechanizmu).\n"
    "Szklane strzykawki = NISKA wartość!"
)


class PumpWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, pump_id: int, handle,
                 confirm_purge: Callable[[], bool] | None = None,
                 parent=None):
        super().__init__(parent)
        self._handle = handle
        self._syncing = False
        self._running = False                 # dla locków parametrów
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

        self.alarm_banner = QLabel("")
        self.alarm_banner.setStyleSheet(
            "background-color: #c62828; color: white; padding: 4px;"
            "font-weight: bold;")
        self.alarm_banner.hide()
        root.addWidget(self.alarm_banner)

        # siatka: 0=nazwa | 1..3=wejścia | 4=przycisk | 5=Aktualnie
        grid = QGridLayout()
        grid.setColumnStretch(5, 1)
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

        # rate
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
        grid.addWidget(self.set_rate_btn, row, 4)
        grid.addWidget(self.rate_cur, row, 5)
        row += 1

        # target: tryb + wartość + jednostka (tryby wg capabilities)
        self.target_mode: QComboBox | None = None
        self.target_spin: QDoubleSpinBox | None = None
        self.target_unit: QComboBox | None = None
        self.target_cur: QLabel | None = None
        if caps.target_volume or caps.target_time:
            grid.addWidget(QLabel("Target:"), row, 0)
            self.target_mode = QComboBox()
            modes = [MODE_CONT]
            if caps.target_volume:
                modes.append(MODE_VOL)
            if caps.target_time:
                modes.append(MODE_TIME)
            self.target_mode.addItems(modes)
            self.target_mode.currentTextChanged.connect(
                self._on_target_mode)
            self.target_spin = QDoubleSpinBox()
            self.target_spin.setRange(0.0, 99999.0)
            self.target_spin.setDecimals(3)
            self.target_unit = QComboBox()
            self.set_target_btn = QPushButton("Ustaw")
            self.set_target_btn.clicked.connect(self._on_set_target)
            self.target_cur = QLabel("—")
            grid.addWidget(self.target_mode, row, 1)
            grid.addWidget(self.target_spin, row, 2)
            grid.addWidget(self.target_unit, row, 3)
            grid.addWidget(self.set_target_btn, row, 4)
            grid.addWidget(self.target_cur, row, 5)
            row += 1
            self._on_target_mode(self.target_mode.currentText())

        # średnica — opcjonalnie
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
            grid.addWidget(self.set_dia_btn, row, 4)
            grid.addWidget(self.dia_cur, row, 5)
            row += 1

        # siła — opcjonalnie (P11 Elite)
        self.force_spin: QDoubleSpinBox | None = None
        self.force_cur: QLabel | None = None
        if caps.force_setting:
            grid.addWidget(QLabel("Siła [%]:"), row, 0)
            self.force_spin = QDoubleSpinBox()
            flo, fhi = caps.force_range
            self.force_spin.setRange(flo, fhi)
            self.force_spin.setDecimals(0)
            self.force_spin.setValue(50)
            self.set_force_btn = QPushButton("Ustaw")
            self.set_force_btn.clicked.connect(self._on_set_force)
            self.force_cur = QLabel("—")
            self.force_cur.setToolTip(FORCE_HINT)
            grid.addWidget(self.force_spin, row, 1)
            grid.addWidget(self.set_force_btn, row, 4)
            grid.addWidget(self.force_cur, row, 5)
            row += 1

        # dozowanie: pompa + szacunek, z nagłówkami kolumn
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
                "Nie zna prędkości purge (zaniża); rozjazd z licznikiem\n"
                "pompy = stall / purge / zgubiona komenda.")
            self.clear_btn = QPushButton("Zeruj")
            self.clear_btn.clicked.connect(self._on_clear)
            grid.addWidget(self.disp_label, row, 1)
            grid.addWidget(self.est_label, row, 2)
            grid.addWidget(self.clear_btn, row, 4)
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
            self.purge_btn.setToolTip("Pełna prędkość! Stop kończy purge.")
            self.purge_btn.clicked.connect(self._on_purge)
            run.addWidget(self.purge_btn)
        run.addStretch()
        root.addLayout(run)

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
        h = self._handle
        for sig, slot in ((h.status, self.on_status),
                          (h.version, self.on_version),
                          (h.error, self.on_error),
                          (h.online, self.on_online),
                          (h.offline, self.on_offline)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

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

    def _on_target_mode(self, mode: str) -> None:
        """Rekonfiguruj jednostki/dostępność spinu wg trybu.

        Wyłączone wejście: tryb Ciągły ALBO pompa w biegu (parametry
        kontekstowe zablokowane — realne pompy je odrzucają w biegu).
        """
        caps = self._handle.capabilities
        self.target_unit.clear()
        if mode == MODE_VOL:
            self.target_unit.addItems(list(caps.volume_units))
        elif mode == MODE_TIME:
            self.target_unit.addItems(list(TIME_UNITS))
        active = mode != MODE_CONT and not self._running
        self.target_spin.setEnabled(active)
        self.target_unit.setEnabled(active)

    def _on_set_target(self) -> None:
        caps = self._handle.capabilities
        mode = self.target_mode.currentText()
        if mode == MODE_CONT:
            if caps.target_volume:
                self._submit(Op.SET_TARGET_VOLUME, volume=0)
            if caps.target_time:
                self._submit(Op.SET_TARGET_TIME, seconds=0)
        elif mode == MODE_VOL:
            if caps.target_time:      # Quick Start: volume ALBO czas
                self._submit(Op.SET_TARGET_TIME, seconds=0)
            kw = {"volume": self.target_spin.value()}
            if self.target_unit.count():
                kw["unit"] = norm_unit(self.target_unit.currentText())
            self._submit(Op.SET_TARGET_VOLUME, **kw)
        else:                          # MODE_TIME
            if caps.target_volume:
                self._submit(Op.SET_TARGET_VOLUME, volume=0)
            secs = time_to_seconds(
                self.target_spin.value(),
                self.target_unit.currentText() or "s")
            self._submit(Op.SET_TARGET_TIME, seconds=secs)

    def _on_set_diameter(self) -> None:
        self._reset_est()
        self._submit(Op.SET_DIAMETER, mm=self.dia_spin.value())

    def _on_set_force(self) -> None:
        self._submit(Op.SET_FORCE, percent=self.force_spin.value())

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
            self._running = bool(st.running)

            if st.alarm:
                self.alarm_banner.setText(f"⚠ ALARM: {st.alarm}")
                self.alarm_banner.show()
            else:
                self.alarm_banner.hide()

            self.start_btn.setEnabled(not st.running)
            self.stop_btn.setEnabled(bool(st.running))

            if st.direction and self.dir_combo is not None:
                self.dir_combo.setCurrentText(st.direction)

            # parametry kontekstowe: blokada w biegu (AL-1000/P11 i tak
            # odrzucają — blokada zamiast błędu w pasku)
            if self.dia_spin is not None:
                self.dia_spin.setEnabled(not st.running)
                self.set_dia_btn.setEnabled(not st.running)
            if self.force_spin is not None:
                self.force_spin.setEnabled(not st.running)
                self.set_force_btn.setEnabled(not st.running)
            if self.target_spin is not None:
                active = self.target_mode.currentText() != MODE_CONT
                ok = active and not st.running
                self.target_spin.setEnabled(ok)
                self.target_unit.setEnabled(ok)
                self.set_target_btn.setEnabled(not st.running)
            # UWAGA: target_mode NIE jest ruszany — to wejście
            # użytkownika; stan pompy pokazuje target_cur niżej

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

            if st.rate is not None:
                self.rate_cur.setText(
                    f"{st.rate:g} {st.rate_unit or ''}".strip())
            if st.diameter is not None and self.dia_cur is not None:
                self.dia_cur.setText(f"Ø{st.diameter:g} mm")
            if st.force is not None and self.force_cur is not None:
                self.force_cur.setText(f"{st.force:g} %")

            # target: TYLKO etykieta stanu pompy (obie wartości, jeśli są)
            if self.target_cur is not None:
                parts = []
                if st.target_volume is not None:
                    parts.append(f"{st.target_volume:g} "
                                 f"{st.target_volume_unit or ''}".strip())
                if st.target_time is not None:
                    parts.append(fmt_duration(st.target_time))
                self.target_cur.setText(
                    " | ".join(parts) if parts else "ciągła")

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

    # ================= w: model pełny (demo caps) ======================
    h = FakeHandle(DemoDriver.capabilities)
    w = PumpWindow(1, h, confirm_purge=lambda: True)

    # 1. struktura
    assert w.dir_combo is not None and w.purge_btn is not None
    assert w.force_spin is not None and w.force_cur is not None
    assert w.target_mode is not None and w.target_spin is not None
    assert [w.target_mode.itemText(i)
            for i in range(w.target_mode.count())] == \
        ["Ciągły", "Objętość", "Czas"]
    assert not w.target_spin.isEnabled()          # tryb domyślny: Ciągły

    # 2. start/stop
    w.start_btn.click()
    assert h.ops[-1] == (Op.START, {})
    w.stop_btn.click()
    assert h.ops[-1] == (Op.STOP, {})

    # 3. rate + walidacja
    w.rate_spin.setValue(250.0)
    w.set_rate_btn.click()
    assert h.ops[-1] == (Op.SET_RATE, {"value": 250.0, "unit": "mL/h"})
    n = len(h.ops)
    h.validate_result = "za duże"
    w.set_rate_btn.click()
    assert len(h.ops) == n and "za duże" in w.error_label.text()
    h.validate_result = None

    # 4. status pełny: wartości aktualne; tryb NIE ruszany przez poll
    h.status.emit(PumpStatus(running=True, direction="INF",
                             rate=120.0, rate_unit="mL/h",
                             target_volume=0.5,
                             target_volume_unit="mL",
                             diameter=14.43,
                             dispensed=1.25, dispensed_unit="mL"))
    assert w.rate_cur.text() == "120 mL/h"
    assert w.target_cur.text() == "0.5 mL"
    assert w.target_mode.currentText() == "Ciągły"   # wejście nietknięte!
    assert w.dia_cur.text() == "Ø14.43 mm"
    assert w.disp_label.text() == "1.250 mL"
    assert not w.start_btn.isEnabled() and w.stop_btn.isEnabled()

    # 4b. bieg blokuje parametry kontekstowe (dia/force/target)
    assert not w.dia_spin.isEnabled() and not w.set_dia_btn.isEnabled()
    assert not w.force_spin.isEnabled()
    assert not w.set_force_btn.isEnabled()
    assert not w.set_target_btn.isEnabled()
    assert not w.target_spin.isEnabled()
    # zmiana trybu w biegu: spin dalej zablokowany
    w.target_mode.setCurrentText("Objętość")
    assert not w.target_spin.isEnabled()
    h.status.emit(PumpStatus(running=False))
    assert w.dia_spin.isEnabled() and w.set_dia_btn.isEnabled()
    assert w.force_spin.isEnabled() and w.set_force_btn.isEnabled()
    assert w.target_spin.isEnabled()  # tryb "Objętość" + postój -> OK
    assert w.set_target_btn.isEnabled()

    # 5. alarm
    h.status.emit(PumpStatus(running=False, alarm="S"))
    assert "ALARM" in w.alarm_banner.text()
    h.status.emit(PumpStatus(running=False))
    assert w.alarm_banner.isHidden()

    # 6. kierunek: klik -> submit; sync -> cisza
    w.dir_combo.setCurrentText("WDR")
    assert h.ops[-1] == (Op.SET_DIRECTION, {"direction": "WDR"})
    n = len(h.ops)
    h.status.emit(PumpStatus(running=True, direction="WDR"))
    assert len(h.ops) == n
    h.status.emit(PumpStatus(running=False))       # postój dla testu 8

    # 7. purge
    w.purge_btn.click()
    assert h.ops[-1] == (Op.PURGE, {})
    n = len(h.ops)
    w._confirm_purge = lambda: False
    w.purge_btn.click()
    assert len(h.ops) == n

    # 8. target: objętość (czyści czas), czas (czyści objętość), ciągły
    w.target_mode.setCurrentText("Objętość")
    assert w.target_spin.isEnabled()
    assert {w.target_unit.itemText(i)
            for i in range(w.target_unit.count())} == {"mL", "µL"}
    w.target_spin.setValue(500.0)
    w.target_unit.setCurrentText("µL")
    w.set_target_btn.click()
    assert h.ops[-2:] == [(Op.SET_TARGET_TIME, {"seconds": 0}),
                          (Op.SET_TARGET_VOLUME,
                           {"volume": 500.0, "unit": "µL"})]

    w.target_mode.setCurrentText("Czas")
    assert {w.target_unit.itemText(i)
            for i in range(w.target_unit.count())} == {"s", "min", "h"}
    w.target_spin.setValue(2.0)
    w.target_unit.setCurrentText("min")
    w.set_target_btn.click()
    assert h.ops[-2:] == [(Op.SET_TARGET_VOLUME, {"volume": 0}),
                          (Op.SET_TARGET_TIME, {"seconds": 120.0})]

    w.target_mode.setCurrentText("Ciągły")
    assert not w.target_spin.isEnabled()
    w.set_target_btn.click()
    assert h.ops[-2:] == [(Op.SET_TARGET_VOLUME, {"volume": 0}),
                          (Op.SET_TARGET_TIME, {"seconds": 0})]

    # 8b. poll aktualizuje etykietę, NIE walczy o tryb z użytkownikiem
    w.target_mode.setCurrentText("Czas")           # wybór użytkownika
    h.status.emit(PumpStatus(running=False, target_time=90.0))
    assert w.target_cur.text() == "1.5 min"
    assert w.target_mode.currentText() == "Czas"   # nietknięty przez poll
    h.status.emit(PumpStatus(running=False, target_volume=5.0,
                             target_volume_unit="µL"))
    assert w.target_cur.text() == "5 µL"
    assert w.target_mode.currentText() == "Czas"
    h.status.emit(PumpStatus(running=False))
    assert w.target_cur.text() == "ciągła"

    # 9. siła
    w.force_spin.setValue(45.0)
    w.set_force_btn.click()
    assert h.ops[-1] == (Op.SET_FORCE, {"percent": 45.0})

    # 10. wersja/link
    h.version.emit("DEMO-1.0")
    assert w.version_label.text() == "DEMO-1.0"
    h.online.emit()
    assert w.link_label.text() == "ONLINE"
    h.offline.emit()
    assert w.link_label.text() == "OFFLINE"

    # 11. detach
    before = w.disp_label.text()
    w.detach()
    h.status.emit(PumpStatus(running=True, direction="INF",
                             dispensed=99.0, dispensed_unit="mL"))
    assert w.disp_label.text() == before

    # ============ w_al: AL-1000-like (bez czasu i siły) ================
    caps_al = PumpCapabilities(name="AL-like", rate_units=("mL/h",),
                               rate_min=0.1, rate_max=50.0,
                               target_volume=True,
                               volume_units=("mL", "µL"))
    h_al = FakeHandle(caps_al)
    w_al = PumpWindow(2, h_al)

    assert [w_al.target_mode.itemText(i)
            for i in range(w_al.target_mode.count())] == \
        ["Ciągły", "Objętość"]                       # brak trybu Czas
    assert w_al.force_spin is None                   # brak wiersza siły
    w_al.target_mode.setCurrentText("Objętość")
    w_al.target_spin.setValue(10.0)
    w_al.set_target_btn.click()
    assert h_al.ops[-1] == (Op.SET_TARGET_VOLUME,
                            {"volume": 10.0, "unit": "mL"})
    assert all(op != Op.SET_TARGET_TIME for op, _ in h_al.ops)
    w_al.target_mode.setCurrentText("Ciągły")
    w_al.set_target_btn.click()
    assert h_al.ops[-1] == (Op.SET_TARGET_VOLUME, {"volume": 0})
    assert all(op != Op.SET_TARGET_TIME for op, _ in h_al.ops)

    # ============ w_t: tylko czas ======================================
    caps_t = PumpCapabilities(name="TimeOnly", rate_units=("mL/h",),
                              rate_min=0.1, rate_max=50.0,
                              target_time=True)
    h_t = FakeHandle(caps_t)
    w_t = PumpWindow(3, h_t)

    assert [w_t.target_mode.itemText(i)
            for i in range(w_t.target_mode.count())] == \
        ["Ciągły", "Czas"]
    w_t.target_mode.setCurrentText("Czas")
    w_t.target_spin.setValue(30.0)
    w_t.set_target_btn.click()
    assert h_t.ops[-1] == (Op.SET_TARGET_TIME, {"seconds": 30.0})
    assert all(op != Op.SET_TARGET_VOLUME for op, _ in h_t.ops)
    w_t.target_mode.setCurrentText("Ciągły")
    w_t.set_target_btn.click()
    assert h_t.ops[-1] == (Op.SET_TARGET_TIME, {"seconds": 0})

    # ============ w_mini: nic opcjonalnego =============================
    caps_mini = PumpCapabilities(name="Mini", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0)
    w_mini = PumpWindow(4, FakeHandle(caps_mini))
    assert w_mini.target_mode is None and w_mini.target_spin is None
    assert w_mini.force_spin is None and w_mini.dir_combo is None
    assert w_mini.purge_btn is None and w_mini.disp_label is None

    # ============ w_lock: blokada rate w biegu (regresja) ==============
    caps_lock = PumpCapabilities(name="Lock", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0,
                                 set_rate_while_running=False)
    h_lock = FakeHandle(caps_lock)
    w_lock = PumpWindow(5, h_lock)
    assert w_lock.rate_spin.isEnabled()
    h_lock.status.emit(PumpStatus(running=True))
    assert not w_lock.rate_spin.isEnabled()

    # ============ closed (regresja) ====================================
    got_close = []
    w_lock.closed.connect(lambda: got_close.append(True))
    w_lock.close()
    assert got_close == [True]

    print("device_window: OK")