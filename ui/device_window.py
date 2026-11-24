"""Okno jednej pompy: UI z Designera (pyuic6), mowa wyłącznie w Op.

Kontrakt:
    PumpWindow(pump_id, handle) — podpina się pod sygnały handle;
    detach() — rozłącza sygnały (używa Session przy usuwaniu pompy);
    closed  — sygnał dla Session, gdy użytkownik zamknie okno.

GEOMETRIA (kontrakt po epopei z pikselami):
    - rozmiary i szerokości NADAJE Designer (.ui) — kod ich nie rusza;
    - DWA stałe rozmiary okna: rozwinięty (= .ui, święty) i zwinięty;
    - pomiar obu DOPERO w showEvent (przed pierwszym show layout
      jest nieświeży — minimum/WM rozciągają okno; wykazane printem
      [RETUNE] na MainPanelu: resize "dostaje" X, a okno po show
      było inne);
    - po pomiarze toggl = setFixedSize(zapamiętany) — zero pomiarów
      po drodze, zero ghost growth.

Target: tryb Ciągły/Objętość/Czas wg capabilities; combobox trybu
jest WYŁĄCZNIE wejściem użytkownika (poll go nie rusza); stan pompy
pokazuje etykieta "Aktualnie". Ustawienie jednego targetu czyści
drugi (Quick Start: volume ALBO czas).

Bieg blokuje parametry kontekstowe: średnicę, siłę i target —
realne pompy i tak odrzucają te komendy w biegu.

Dozowanie: licznik pompy (autorytatywny) + szacunek lokalny obok.
Programowe modyfikacje kontrolek pod flagą _syncing.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QMessageBox, QWidget,
)

from core.pump.base import (
    BasePumpDriver, Op, PumpStatus, fmt_dispensed,
    fmt_duration, fmt_volume_ml, norm_unit, time_to_seconds,
)
from ui.selector_window import JsonSettings
from ui.ui_pump_window import Ui_PumpWindow

MODE_CONT = "Ciągły"
MODE_VOL = "Objętość"
MODE_TIME = "Czas"
TIME_UNITS = ("s", "min", "h")

FORCE_HINT = (
    "<b>Zalecenia producenta</b> (Harvard):\n <br>"
    "<b>plastik</b> ≤20 ml ~50%, >20 ml ~100%\n <br>"
    "<b>szkło/szkło</b> ≤20 ml 20–30%, >20 ml 30–50%\n <br>"
    "<b>szkło/plastik</b> ≤250 µl 30%, ≤5 ml 50%\n <br>"
    "<b>stal</b> 100%.\n <br>"
    "<b>Minimum ~20% (tarcie mechanizmu).</b>\n <br>"
    "<b>Szklane strzykawki = NISKA wartość!</b> <br>"
)

ALARM_STYLE = (
    "background-color: #c62828; color: white; padding: 4px;"
    "font-weight: bold;")
BANNER_IDLE_STYLE = (
    "background-color: transparent; color: transparent; padding: 4px;"
    "font-weight: bold;")


class PumpWindow(QWidget):
    closed = pyqtSignal()

    def __init__(self, pump_id, handle, confirm_purge=None,
                 settings=None, parent=None):
        super().__init__(parent)
        self._handle = handle
        self._syncing = False
        self._running = False
        self._confirm_purge = confirm_purge or self._default_confirm_purge
        self._settings = settings if settings is not None else JsonSettings()
        caps = handle.capabilities
        self._est = {"INF": 0.0, "WDR": 0.0}
        self._last_ts = None
        self._pump_id = pump_id

        # rozmiary: dopiero showEvent zna prawdę (patrz _measure_sizes)
        self._size_expanded: tuple[int, int] | None = None
        self._size_collapsed: tuple[int, int] | None = None
        self._pending_collapse: bool = False   # stan restorowany przed show

        # 1. UI w całości z Designera — kod NIE rusza geometrii
        self.ui = Ui_PumpWindow()
        self.ui.setupUi(self)
        self.setWindowTitle(f"Pompa {pump_id} — {caps.name}")

        # 2. aliasy: reszta kodu i testy czytają jak dotąd
        # 2. aliasy: reszta kodu i testy czytają jak dotąd
        for name in ("link_label", "model_label", "version_label",
                     "collapse_btn", "alarm_banner", "settings_box",
                     "dir_combo", "dir_cur", "rate_spin", "rate_unit",
                     "set_rate_btn", "rate_cur", "target_mode",
                     "target_spin", "target_unit", "set_target_btn",
                     "target_cur", "dia_spin", "set_dia_btn", "dia_cur",
                     "force_spin", "set_force_btn", "force_cur",
                     "disp_row_hdr", "disp_hdr", "est_hdr",
                     "disp_label", "est_label",
                     "clear_btn", "start_btn", "stop_btn", "purge_btn",
                     "error_label"):
            setattr(self, name, getattr(self.ui, name))

        # 3. widoczność wg capabilities (PRZED pomiarami w showEvent!)
        self._apply_caps_visibility(caps)

        # 3b. dane dynamiczne wg capabilities (Designer daje puste
        #     kontrolki — to warstwa, którą stare _build_ui robiło,
        #     a nowa musi robić tuż po widocznościach)
        self._populate(caps)

        # 4. connecty — zawsze w kodzie
        self.collapse_btn.toggled.connect(self._on_collapse)
        self.dir_combo.currentTextChanged.connect(self._on_direction)
        self.set_rate_btn.clicked.connect(self._on_set_rate)
        self.target_mode.currentTextChanged.connect(self._on_target_mode)
        self.set_target_btn.clicked.connect(self._on_set_target)
        self.set_dia_btn.clicked.connect(self._on_set_diameter)
        self.set_force_btn.clicked.connect(self._on_set_force)
        self.clear_btn.clicked.connect(self._on_clear)
        self.purge_btn.clicked.connect(self._on_purge)
        self.start_btn.clicked.connect(lambda: self._submit(Op.START))
        self.stop_btn.clicked.connect(lambda: self._submit(Op.STOP))

        # 5. restor zwinięcia: stan zapamiętany, APLIKACJA w showEvent
        #    (przed show setFixedSize jest niepewny — patrz _measure_sizes)
        self._pending_collapse = bool(self._settings.value(
            "pump_settings_collapsed", False))

        self._attach()

    # ---------------------------------------------------- geometria/show

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._size_expanded is not None:
            return                    # już zmierzone (ponowny show)
        self._size_expanded = (self.width(), self.height())

        self.settings_box.setVisible(False)
        self.adjustSize()
        self._size_collapsed = (self.width(), self.height())
        self.settings_box.setVisible(True)

        # sync PRZYCISKU z restorowanym stanem okna: Designer startuje
        # unchecked; bez tego restor collapsed=True zostawia przycisk
        # unchecked i PIERWSZY klik toggluje False->True (= zwija
        # już-zwinięte) zamiast rozwijać. blockSignals: restor nie
        # odpala _on_collapse (nie pisze do settings).
        self.collapse_btn.blockSignals(True)
        self.collapse_btn.setChecked(self._pending_collapse)
        self.collapse_btn.blockSignals(False)

        self._apply_collapse(self._pending_collapse)
    def _apply_collapse(self, collapsed: bool) -> None:
        """Zwiń/rozwiń: jeśli rozmiary jeszcze nie zmierzone (okno
        niewidoczne — tylko zapamiętaj stan), w przeciwnym razie
        setFixedSize na zapamiętany rozmiar danego stanu."""
        if self._size_expanded is None:
            self._pending_collapse = collapsed
            return
        self.settings_box.setVisible(not collapsed)
        self.collapse_btn.setText("▶" if collapsed else "◀")
        self.setFixedSize(*(self._size_collapsed if collapsed
                            else self._size_expanded))

    def _on_collapse(self, checked: bool) -> None:
        self._apply_collapse(checked)
        self._settings.setValue("pump_settings_collapsed", checked)

    def _apply_caps_visibility(self, caps) -> None:
        u = self.ui
        multi = len(caps.directions) > 1
        for w in (u.direction_label, u.dir_combo, u.dir_cur):
            w.setVisible(multi)
        has_target = caps.target_volume or caps.target_time
        for w in (u.target_label, u.target_mode, u.target_spin,
                  u.target_unit, u.set_target_btn, u.target_cur):
            w.setVisible(has_target)
        u.diameter_label.setVisible(caps.diameter_setting)
        u.dia_spin.setVisible(caps.diameter_setting)
        u.set_dia_btn.setVisible(caps.diameter_setting)
        u.dia_cur.setVisible(caps.diameter_setting)
        u.force_label.setVisible(caps.force_setting)
        u.force_spin.setVisible(caps.force_setting)
        u.set_force_btn.setVisible(caps.force_setting)
        u.force_cur.setVisible(caps.force_setting)
        u.purge_btn.setVisible(caps.purge)
        # dozowanie — LUKA MIGRACYJNA (wykryta testem w_mini): stare
        # _build_ui tworzyło te widgety tylko przy dispensed_query;
        # Designer daje je zawsze -> capabilities je chowają
        has_disp = caps.dispensed_query
        for w in (u.disp_row_hdr, u.disp_hdr, u.est_hdr,
                  u.disp_label, u.est_label, u.clear_btn):
            w.setVisible(has_disp)

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
                self.alarm_banner.setStyleSheet(ALARM_STYLE)
            else:
                self.alarm_banner.setText(" ")
                self.alarm_banner.setStyleSheet(BANNER_IDLE_STYLE)

            self.start_btn.setEnabled(not st.running)
            self.stop_btn.setEnabled(bool(st.running))

            if st.direction and self.dir_combo is not None:
                self.dir_combo.setCurrentText(st.direction)
            if st.direction and self.dir_cur is not None:
                self.dir_cur.setText(st.direction)

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
        self.error_label.setToolTip(msg)   # pełna treść; label ma 2 linie

    def closeEvent(self, event) -> None:
        self.closed.emit()
        event.accept()


    def _populate(self, caps) -> None:
        """Wypełnij kontrolki danymi z capabilities + wartości startowe.

        Luka migracyjna z _build_ui: Designer daje PUSTE combo i spiny
        bez zakresów — bez tej metody GUI jest martwe (pad asercji
        itemText == [Ciągły, Objętość, Czas]). Kod NIE rusza geometrii
        (kontrakt z Designera) — tylko dane i zakresy.
        """
        # kierunek
        self.dir_combo.addItems(list(caps.directions))
        # rate
        self.rate_unit.addItems(list(caps.rate_units))
        lo = caps.rate_min
        hi = caps.rate_max if caps.rate_max != float("inf") else 99999.0
        self.rate_spin.setRange(lo, hi)
        self.rate_spin.setDecimals(3)
        # target: tryby + jednostki (jak _on_target_mode, ale startowo)
        modes = [MODE_CONT]
        if caps.target_volume:
            modes.append(MODE_VOL)
        if caps.target_time:
            modes.append(MODE_TIME)
        self.target_mode.addItems(modes)
        self.target_spin.setRange(0.0, 99999.0)
        self.target_spin.setDecimals(3)
        self._on_target_mode(self.target_mode.currentText())  # unit combo
        # średnica
        if caps.diameter_setting:
            dlo, dhi = caps.diameter_range
            self.dia_spin.setRange(dlo, dhi)
            self.dia_spin.setDecimals(2)
            self.dia_spin.setValue(dhi / 2.0)
        # siła
        if caps.force_setting:
            self.force_spin.setValue(50)
        # startowe teksty (Designerowe placeholdery do wyciszenia)
        self.alarm_banner.setText(" ")
        self.alarm_banner.setStyleSheet(BANNER_IDLE_STYLE)
        self.error_label.setText("")
        self.error_label.setToolTip("")
        # teksty "Aktualnie" — zamiast Designerowego "TextLabel"
        for lbl in (self.dir_cur, self.rate_cur, self.target_cur,
                    self.dia_cur, self.force_cur,
                    self.disp_label, self.est_label):
            lbl.setText("—")
        self.version_label.setText("")
        self.disp_hdr.setText("z pompy")
        self.est_hdr.setText("szacunek")
        self.disp_row_hdr.setText("Dozowane:")
        # tooltipy
        self.force_cur.setToolTip(FORCE_HINT)
        self.disp_label.setToolTip(
            "Licznik pompy (autorytatywny; jednostki auto-skalowane)")
        self.est_label.setToolTip(
            "Szacunek lokalny: ustawiony rate × czas.\n"
            "Nie zna prędkości purge (zaniża); rozjazd z licznikiem\n"
            "pompy = stall / purge / zgubiona komenda.")
        self.collapse_btn.setToolTip(
            "Zwiń/rozwiń pola ustawiania.\n"
            "Aktualne wartości, dozowanie i stery zostają widoczne.")


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

    def shown(win):
        """Pokaż okno (showEvent mierzy) + dostarcz zdarzenia."""
        win.show()
        app.processEvents()

    # ================= w: model pełny (demo caps) ======================
    h = FakeHandle(DemoDriver.capabilities)
    w = PumpWindow(1, h, confirm_purge=lambda: True)
    shown(w)          # showEvent: pomiar obu rozmiarów + restor stanu

    # 1. struktura
    assert w.dir_combo is not None and w.purge_btn is not None
    assert w.force_spin is not None and w.force_cur is not None
    assert w.target_mode is not None and w.target_spin is not None
    assert [w.target_mode.itemText(i)
            for i in range(w.target_mode.count())] == \
        ["Ciągły", "Objętość", "Czas"]
    assert not w.target_spin.isEnabled()          # tryb domyślny: Ciągły
    assert w._size_expanded is not None and w._size_collapsed is not None
    assert w._size_collapsed < w._size_expanded   # zwinięty mniejszy

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
    w.target_mode.setCurrentText("Objętość")
    assert not w.target_spin.isEnabled()
    h.status.emit(PumpStatus(running=False))
    assert w.dia_spin.isEnabled() and w.set_dia_btn.isEnabled()
    assert w.force_spin.isEnabled() and w.set_force_btn.isEnabled()
    assert w.target_spin.isEnabled()
    assert w.set_target_btn.isEnabled()

    # 5. alarm: wiersz zarezerwowany — tekst/styl się zmieniają
    h.status.emit(PumpStatus(running=False, alarm="S"))
    assert "ALARM" in w.alarm_banner.text()
    h.status.emit(PumpStatus(running=False))
    assert w.alarm_banner.text() == " "
    assert w.alarm_banner.isVisibleTo(w)

    # 6. kierunek
    w.dir_combo.setCurrentText("WDR")
    assert h.ops[-1] == (Op.SET_DIRECTION, {"direction": "WDR"})
    n = len(h.ops)
    h.status.emit(PumpStatus(running=True, direction="WDR"))
    assert len(h.ops) == n
    h.status.emit(PumpStatus(running=False))

    # 7. purge
    w.purge_btn.click()
    assert h.ops[-1] == (Op.PURGE, {})
    n = len(h.ops)
    w._confirm_purge = lambda: False
    w.purge_btn.click()
    assert len(h.ops) == n

    # 8. target: objętość/czas/ciągły (jak dotąd)
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

    # 8b. poll aktualizuje etykietę, NIE walczy o tryb
    w.target_mode.setCurrentText("Czas")
    h.status.emit(PumpStatus(running=False, target_time=90.0))
    assert w.target_cur.text() == "1.5 min"
    assert w.target_mode.currentText() == "Czas"
    h.status.emit(PumpStatus(running=False, target_volume=5.0,
                             target_volume_unit="µL"))
    assert w.target_cur.text() == "5 µL"
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
    shown(w_al)

    assert [w_al.target_mode.itemText(i)
            for i in range(w_al.target_mode.count())] == \
        ["Ciągły", "Objętość"]
    w_al.target_mode.setCurrentText("Objętość")
    w_al.target_spin.setValue(10.0)
    w_al.set_target_btn.click()
    assert h_al.ops[-1] == (Op.SET_TARGET_VOLUME,
                            {"volume": 10.0, "unit": "mL"})
    assert all(op != Op.SET_TARGET_TIME for op, _ in h_al.ops)
    w_al.target_mode.setCurrentText("Ciągły")
    w_al.set_target_btn.click()
    assert h_al.ops[-1] == (Op.SET_TARGET_VOLUME, {"volume": 0})

    # ============ w_t: tylko czas ======================================
    caps_t = PumpCapabilities(name="TimeOnly", rate_units=("mL/h",),
                              rate_min=0.1, rate_max=50.0,
                              target_time=True)
    h_t = FakeHandle(caps_t)
    w_t = PumpWindow(3, h_t)
    shown(w_t)

    assert [w_t.target_mode.itemText(i)
            for i in range(w_t.target_mode.count())] == \
        ["Ciągły", "Czas"]
    w_t.target_mode.setCurrentText("Czas")
    w_t.target_spin.setValue(30.0)
    w_t.set_target_btn.click()
    assert h_t.ops[-1] == (Op.SET_TARGET_TIME, {"seconds": 30.0})
    w_t.target_mode.setCurrentText("Ciągły")
    w_t.set_target_btn.click()
    assert h_t.ops[-1] == (Op.SET_TARGET_TIME, {"seconds": 0})


    # ============ w_mini: nic opcjonalnego — wszystko UKRYTE ===========
    # (Designer: widgety istnieją zawsze; capabilities je chowają.
    #  UWAGA: settings z dysku może mieć collapsed=True — testy
    #  zakładające rozwinięty start dostają świeży FakeSt!)
    class FakeSt:  # (jeśli już zdefiniowane wyżej — nie duplikuj)
        def __init__(self):   self.d = {}

        def value(self, k, default=None): return self.d.get(k, default)

        def setValue(self, k, v):         self.d[k] = v


    caps_mini = PumpCapabilities(name="Mini", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0)
    w_mini = PumpWindow(4, FakeHandle(caps_mini), settings=FakeSt())
    shown(w_mini)
    assert w_mini.rate_spin.isVisibleTo(w_mini)  # rozwinięty start
    for name in ("target_mode", "target_spin", "target_unit",
                 "force_spin", "dir_combo", "purge_btn",
                 "disp_label", "est_label", "clear_btn", "dia_spin"):
        assert not getattr(w_mini, name).isVisibleTo(w_mini), \
            f"w_mini: {name} widoczne, a caps go nie wspiera"

    # ============ w_lock: blokada rate w biegu (regresja) ==============
    caps_lock = PumpCapabilities(name="Lock", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0,
                                 set_rate_while_running=False)
    h_lock = FakeHandle(caps_lock)
    w_lock = PumpWindow(5, h_lock)
    shown(w_lock)
    assert w_lock.rate_spin.isEnabled()
    h_lock.status.emit(PumpStatus(running=True))
    assert not w_lock.rate_spin.isEnabled()

    # ============ closed (regresja) ====================================
    got_close = []
    w_lock.closed.connect(lambda: got_close.append(True))
    w_lock.close()
    assert got_close == [True]

    # 19. DETERMINIZM ROZMIARÓW (showEvent jako jedyny punkt pomiaru)
    class FakeSt:
        def __init__(self):   self.d = {}
        def value(self, k, default=None): return self.d.get(k, default)
        def setValue(self, k, v):         self.d[k] = v

    st_e, st_c = FakeSt(), FakeSt()
    st_c.d["pump_settings_collapsed"] = True

    w_e1 = PumpWindow(11, FakeHandle(DemoDriver.capabilities),
                      settings=st_e)
    w_e2 = PumpWindow(12, FakeHandle(DemoDriver.capabilities),
                      settings=st_e)
    w_k1 = PumpWindow(13, FakeHandle(DemoDriver.capabilities),
                      settings=st_c)
    w_k2 = PumpWindow(14, FakeHandle(DemoDriver.capabilities),
                      settings=st_c)
    shown(w_e1); shown(w_e2); shown(w_k1); shown(w_k2)

    size_e = (w_e1.width(), w_e1.height())
    assert (w_e2.width(), w_e2.height()) == size_e, \
        f"dwa rozwinięte: {(w_e2.width(), w_e2.height())} != {size_e}"

    size_c = (w_k1.width(), w_k1.height())
    assert (w_k2.width(), w_k2.height()) == size_c, \
        f"dwa zwinięte: {(w_k2.width(), w_k2.height())} != {size_c}"
    assert size_c[0] <= size_e[0] and size_c[1] < size_e[1], \
        f"zwinięte {size_c} nie mniejsze od rozwiniętego {size_e}"

    # toggle w obie strony — wraca co do wymiaru
    w_k1.collapse_btn.setChecked(False)
    app.processEvents()
    assert (w_k1.width(), w_k1.height()) == size_e, \
        f"rozwinięcie po togglu {(w_k1.width(), w_k1.height())} != {size_e}"
    w_k1.collapse_btn.setChecked(True)
    app.processEvents()
    assert (w_k1.width(), w_k1.height()) == size_c, \
        f"zwinięcie po togglu {(w_k1.width(), w_k1.height())} != {size_c}"
    w_k1.collapse_btn.setChecked(False)
    app.processEvents()
    assert (w_k1.width(), w_k1.height()) == size_e, "runda 2: rozwinięcie"

    # statusy/długie teksty nie ruszają geometrii
    w_e1._handle.status.emit(PumpStatus(
        running=True, rate=99999.999, rate_unit="µL/min",
        direction="WDR", dispensed=1234.567, dispensed_unit="mL"))
    app.processEvents()
    assert (w_e1.width(), w_e1.height()) == size_e, "status ruszył rozmiar"
    assert w_e1.alarm_banner.isVisibleTo(w_e1)

    # 19d. persystencja zwinięcia (stan aplikowany w showEvent!)
    st_p = FakeSt()
    w_p = PumpWindow(15, FakeHandle(DemoDriver.capabilities),
                     settings=st_p)
    # PRZED show: stan tylko zapamiętany, nic nie ustawione
    assert st_p.d == {}
    shown(w_p)                        # showEvent: pomiar + _apply_collapse
    assert st_p.d == {}
    assert w_p.rate_spin.isVisibleTo(w_p)   # default: rozwinięta

    w_p.collapse_btn.setChecked(True)
    assert st_p.d == {"pump_settings_collapsed": True}
    assert not w_p.rate_spin.isVisibleTo(w_p)
    assert w_p.rate_cur.isVisibleTo(w_p)
    assert w_p.disp_label.isVisibleTo(w_p)

    w_p.start_btn.click()
    assert w_p._handle.ops[-1] == (Op.START, {})
    w_p._handle.status.emit(PumpStatus(running=True, direction="WDR"))
    assert w_p.dir_cur.text() == "WDR"

    w_p.collapse_btn.setChecked(False)
    assert st_p.d == {"pump_settings_collapsed": False}
    assert w_p.rate_spin.isVisibleTo(w_p)

    # restor False -> rozwinięta; restor True -> zwinięta
    w_p2 = PumpWindow(16, FakeHandle(DemoDriver.capabilities),
                      settings=st_p)
    shown(w_p2)
    assert st_p.d == {"pump_settings_collapsed": False}
    assert w_p2.rate_spin.isVisibleTo(w_p2)

    st_q = FakeSt()
    st_q.d["pump_settings_collapsed"] = True
    w_q = PumpWindow(17, FakeHandle(DemoDriver.capabilities),
                     settings=st_q)
    shown(w_q)
    assert st_q.d == {"pump_settings_collapsed": True}
    assert not w_q.rate_spin.isVisibleTo(w_q)
    assert w_q.rate_cur.isVisibleTo(w_q)

    # 19e. PIERWSZY klik po restorze collapsed=True rozwija od razu
    #      (regresja: showEvent nie synchronizował przycisku — klik
    #      togglował False->True = zwijał już-zwinięte)
    st_r = FakeSt()
    st_r.d["pump_settings_collapsed"] = True
    w_r = PumpWindow(18, FakeHandle(DemoDriver.capabilities),
                     settings=st_r)
    shown(w_r)
    assert w_r.collapse_btn.isChecked()  # przycisk = stan okna!
    assert not w_r.rate_spin.isVisibleTo(w_r)  # zwinięte od startu
    w_r.collapse_btn.setChecked(False)  # PIERWSZY klik
    app.processEvents()
    assert w_r.rate_spin.isVisibleTo(w_r), \
        "pierwszy klik po restorze collapsed nie rozwija"
    assert st_r.d == {"pump_settings_collapsed": False}  # zapis usera
    w_r.collapse_btn.setChecked(True)  # drugi: zwija
    app.processEvents()
    assert not w_r.rate_spin.isVisibleTo(w_r)

    print("device_window: OK")