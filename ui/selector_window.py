"""Okno wyboru: wiersze (port + model + setup) -> konfiguracja sesji.

Umowa:
    session_requested(list[(port|None, model_key, setup|None)])
        setup = None (checkbox off) albo dict z polami, które model
        wspiera: {"diameter": mm, "force": procent}
    SelectorWindow(scan_source=..., settings=..., error_reporter=...)
        scan_source    — seam skanera portów (testy podają fake)
        settings       — obiekt o API QSettings (value/setValue)
        error_reporter — callable(msg); domyślnie QMessageBox

Walidacja przy "Uruchom" (okno NIE zamyka się przy błędzie):
    model realny bez portu / port użyty 2 razy / zero wierszy.
Model demo ignoruje port (emit None).
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.pump.demo import DemoDriver
from core.pump.registry import REGISTRY
from core.port_scanner import PortInfo, scan_ports

import json
from pathlib import Path

# Magazyn ustawień: zwykły plik JSON obok projektu — czytelny,
# przenośny, usuwa się razem z aplikacją. Konstruktor przyjmuje
# ścieżkę (testy: plik tymczasowy); domyślnie tripumpator.json
# w katalogu głównym projektu.
SETTINGS_PATH = Path(__file__).resolve().parent.parent / "tripumpator.json"


class JsonSettings:
    """API zgodne z QSettings (value/setValue), backend: plik JSON.

    Zapis atomowy (tmp + replace) — przerwanie zapisu nie niszczy
    poprzedniego pliku. Porażka I/O (np. dysk read-only) jest cicha:
    ustawienia to wygoda, nie krytyczny stan aplikacji.
    """

    def __init__(self, path: Path | str = SETTINGS_PATH):
        self._path = Path(path)
        self._d: dict = {}
        try:
            self._d = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            pass                       # brak pliku / uszkodzony -> start od zera

    def value(self, key, default=None):
        return self._d.get(key, default)

    def setValue(self, key, value):
        self._d[key] = value
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._d, ensure_ascii=False,
                                      indent=2), "utf-8")
            tmp.replace(self._path)          # atomowy commit
        except OSError:
            pass

_PLACEHOLDER = "— brak portu (demo) —"
SETUP_HINT = (
    "Wartości startowe wysyłane do pompy po starcie sesji "
    "(średnica najpierw, potem siła).\n"
    "UWAGA: zmiana średnicy kasuje liczniki i rate na pompie."
)


class _Row(QWidget):
    """Jeden wiersz: port + model + setup (średnica, siła) + usuń."""

    def __init__(self, on_remove: Callable[["_Row"], None]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.port_cb = QComboBox()
        self.model_cb = QComboBox()

        self.dia_spin = QDoubleSpinBox()
        self.dia_spin.setDecimals(3)  # zakresy modeli mają 3 miejsca
        self.dia_spin.setSuffix(" mm")
        self.dia_spin.hide()
        self.force_spin = QDoubleSpinBox()
        self.force_spin.setDecimals(0)
        self.force_spin.setSuffix(" %")
        self.force_spin.hide()

        self.setup_chk = QCheckBox("Setup")
        self.setup_chk.setChecked(True)
        self.setup_chk.setToolTip(SETUP_HINT)

        self.rm_btn = QPushButton("–")
        self.rm_btn.setFixedWidth(32)
        self.rm_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.port_cb, 3)
        lay.addWidget(self.model_cb, 2)
        lay.addWidget(self.dia_spin, 1)
        lay.addWidget(self.force_spin, 1)
        lay.addWidget(self.setup_chk)
        lay.addWidget(self.rm_btn)

    # ------------------------------------------------------------ setup

    def _caps(self):
        return REGISTRY[self.model_cb.currentText()].capabilities

    def apply_caps(self) -> None:
        """Pokaż/ukryj pola setupu i ustaw zakresy wg modelu.

        Zmiana modelu ZACHOWUJE wpisaną wartość, jeśli mieści się
        w nowym zakresie (przeklikany model nie wyżyma danych);
        wartość domyślna tylko przy wyjściu poza zakres.
        """
        caps = self._caps()
        if caps.diameter_setting:
            dlo, dhi = caps.diameter_range
            cur = self.dia_spin.value()
            self.dia_spin.setRange(dlo, dhi)
            if not dlo <= cur <= dhi:
                self.dia_spin.setValue(dhi / 2.0)   # default: środek
            self.dia_spin.show()
        else:
            self.dia_spin.hide()
        if caps.force_setting:
            flo, fhi = caps.force_range
            cur = self.force_spin.value()
            self.force_spin.setRange(flo, fhi)
            if not flo <= cur <= fhi:
                self.force_spin.setValue(50)
            self.force_spin.show()
        else:
            self.force_spin.hide()

    def setup_out(self) -> dict | None:
        """Dict pól wspieranych przez model; None = nie wysyłaj."""
        if not self.setup_chk.isChecked():
            return None
        caps = self._caps()
        out: dict = {}
        if caps.diameter_setting:
            out["diameter"] = self.dia_spin.value()
        if caps.force_setting:
            out["force"] = self.force_spin.value()
        return out or None

    def apply_setup(self, setup: dict | None) -> None:
        """Zastosuj zapisany setup (dict z wartościami).

        None = brak zapisu: NIC nie robi — checkbox zostaje w stanie
        bieżącym (nowy wiersz = domyślnie ON). Jawne wyłączenie setupu
        przy reście robi _restore (setChecked(False)).
        """
        if setup is None:
            return
        self.setup_chk.setChecked(True)
        caps = self._caps()
        if "diameter" in setup and caps.diameter_setting:
            self.dia_spin.setValue(float(setup["diameter"]))
        if "force" in setup and caps.force_setting:
            self.force_spin.setValue(float(setup["force"]))
    # ------------------------------------------------------------ porty

    def set_ports(self, ports: list[PortInfo]) -> None:
        """Odśwież porty, zachowaj wybór PO DANYCH (nie po indeksie)."""
        prev = self.port_cb.currentData()
        self.port_cb.clear()
        self.port_cb.addItem(_PLACEHOLDER, None)
        for p in ports:
            self.port_cb.addItem(p.label, p.device)
        for i in range(self.port_cb.count()):
            if self.port_cb.itemData(i) == prev:
                self.port_cb.setCurrentIndex(i)
                break
        else:
            self.port_cb.setCurrentIndex(0)


class SelectorWindow(QDialog):
    session_requested = pyqtSignal(list)
    # [(port|None, model_key, setup|None), ...]

    def __init__(self, scan_source=None, settings=None,
                 error_reporter: Callable[[str], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("TriPumpator — konfiguracja stanowiska")
        self._scan_source = scan_source
        if settings is not None:  # jawny seam (testy)
            self._settings = settings
        else:
            self._settings = JsonSettings()  # tripumpator.json
        self._error = error_reporter or self._default_error
        self._ports: list[PortInfo] = []
        self.rows: list[_Row] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Wiersz = jedna pompa. Model demo nie wymaga portu COM. "
            "Setup = wartości startowe (średnica/siła) wysyłane po "
            "starcie sesji."))

        self._rows_box = QVBoxLayout()
        root.addLayout(self._rows_box)

        btns = QHBoxLayout()
        add_btn = QPushButton("+ Dodaj pompę")
        add_btn.clicked.connect(lambda: self._add_row())
        refresh_btn = QPushButton("Odśwież porty")
        refresh_btn.clicked.connect(self.refresh_ports)
        btns.addWidget(add_btn)
        btns.addWidget(refresh_btn)
        btns.addStretch()
        root.addLayout(btns)

        start_btn = QPushButton("▶ Uruchom sesję")
        start_btn.setStyleSheet("font-weight: bold;")
        start_btn.clicked.connect(self._emit)
        root.addWidget(start_btn)

        self.refresh_ports()
        self._restore()

    # ------------------------------------------------------------- API

    def refresh_ports(self) -> None:
        self._ports = scan_ports(source=self._scan_source)
        for row in self.rows:
            row.set_ports(self._ports)

    # ------------------------------------------------------- wiersze

    def _add_row(self, port: str | None = None,
                 model_key: str | None = None,
                 setup: dict | None = None) -> _Row:
        row = _Row(self._remove_row)
        row.model_cb.currentTextChanged.connect(
            lambda _, r=row: self._on_model_changed(r))
        row.model_cb.addItems(list(REGISTRY))     # sygnał -> apply_caps
        self._rows_box.addWidget(row)
        self.rows.append(row)
        row.set_ports(self._ports)
        if model_key:
            row.model_cb.setCurrentText(model_key)   # -> apply_caps
        if port:
            i = row.port_cb.findData(port)
            if i >= 0:
                row.port_cb.setCurrentIndex(i)
        self._on_model_changed(row)
        row.apply_setup(setup)
        return row

    def _remove_row(self, row: _Row) -> None:
        self.rows.remove(row)
        self._rows_box.removeWidget(row)
        row.deleteLater()

    def _on_model_changed(self, row: _Row) -> None:
        demo = REGISTRY[row.model_cb.currentText()] is DemoDriver
        row.port_cb.setEnabled(not demo)
        row.apply_caps()

    # ---------------------------------------------------------- start

    def _emit(self) -> None:
        if not self.rows:
            self._error("Dodaj przynajmniej jedną pompę.")
            return
        cfg: list[tuple] = []
        seen: set[str] = set()
        for row in self.rows:
            key = row.model_cb.currentText()
            demo = REGISTRY[key] is DemoDriver
            port = row.port_cb.currentData()
            if not demo:
                if port is None:
                    self._error(f"{key}: wybierz port COM dla tego modelu.")
                    return
                if port in seen:
                    self._error(
                        f"Port {port} jest już przypisany innej pompie.")
                    return
                seen.add(port)
            cfg.append((None if demo else port, key, row.setup_out()))
        self._settings.setValue("rows", [[p, k, s] for p, k, s in cfg])
        self.session_requested.emit(cfg)
        self.accept()

    def _restore(self) -> None:
        saved = self._settings.value("rows", None)
        if saved:
            try:
                for entry in saved:
                    port, key = entry[0], entry[1]
                    if len(entry) > 2:
                        self._add_row(port, key, entry[2])
                    else:
                        row = self._add_row(port, key)
                        row.setup_chk.setChecked(False)  # legacy v1
                return
            except Exception:
                pass  # zepsute ustawienia -> układ domyślny
        self._add_row()

    def _default_error(self, msg: str) -> None:
        QMessageBox.warning(self, "Konfiguracja", msg)


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    class FakePort:
        def __init__(self, device, desc="", vid=None, pid=None, sn=None,
                     hwid=""):
            self.device, self.description = device, desc
            self.vid, self.pid = vid, pid
            self.serial_number, self.hwid = sn, hwid

    class FakeSettings:
        def __init__(self):        self.d = {}
        def value(self, k, default=None): return self.d.get(k, default)
        def setValue(self, k, v):   self.d[k] = v

    fake_ports = [
        FakePort("COM3", "USB Serial Port", vid=0x0403, pid=0x6001, sn="A1"),
        FakePort("COM5", "CH340", vid=0x1A86, pid=0x7523, sn=None),
    ]
    src = lambda: fake_ports

    AL, P11, DEMO = ("WPI AL-1000", "Harvard Pump 11 Elite",
                     "Pompa demo (symulator)")

    from core.pump.registry import REGISTRY

    errs: list[str] = []
    got: list = []

    # 1. domyślny wiersz (AL): średnica widoczna, siła nie, setup ON
    st = FakeSettings()
    s = SelectorWindow(scan_source=src, settings=st,
                       error_reporter=errs.append)
    s.session_requested.connect(got.append)
    r0 = s.rows[0]
    assert r0.model_cb.currentText() == AL
    assert r0.dia_spin.isVisibleTo(r0)
    assert not r0.force_spin.isVisibleTo(r0)
    assert r0.setup_chk.isChecked()
    assert r0.port_cb.count() == 3 and r0.port_cb.isEnabled()

    # 2. AL: brak force -> setup_out tylko średnica
    r0.dia_spin.setValue(14.43)
    r0.port_cb.setCurrentIndex(1)                 # COM3
    assert r0.setup_out() == {"diameter": 14.43}

    # 3. P11: oba pola + zakresy z capabilities; wartość 14.43 zachowana
    r0.model_cb.setCurrentText(P11)
    assert r0.force_spin.isVisibleTo(r0)
    assert r0.force_spin.minimum() == 1.0
    assert r0.force_spin.maximum() == 100.0
    assert abs(r0.dia_spin.minimum() - 0.103) < 1e-9
    assert r0.dia_spin.decimals() == 3
    assert r0.dia_spin.value() == 14.43          # przeklikany model nie wyżyma
    r0.force_spin.setValue(40)

    # 4. emit z setupem (P11: średnica+siła); zapis v2; accept
    s._emit()
    assert not errs
    assert got == [[("COM3", P11,
                     {"diameter": 14.43, "force": 40.0})]]
    assert st.d["rows"] == [["COM3", P11,
                             {"diameter": 14.43, "force": 40.0}]]
    assert s.result() == 1

    # 5. odtworzenie v2 (nowa instancja, ten sam settings)
    s2 = SelectorWindow(scan_source=src, settings=st,
                        error_reporter=errs.append)
    assert len(s2.rows) == 1
    r = s2.rows[0]
    assert r.model_cb.currentText() == P11
    assert r.port_cb.currentData() == "COM3"
    assert r.setup_chk.isChecked()
    assert r.dia_spin.value() == 14.43
    assert r.force_spin.value() == 40.0

    # 6. setup OFF -> trzeci element None; port realny bez zmian
    got.clear()
    s3 = SelectorWindow(scan_source=src, settings=FakeSettings(),
                        error_reporter=errs.append)
    s3.session_requested.connect(got.append)
    s3.rows[0].port_cb.setCurrentIndex(1)
    s3.rows[0].setup_chk.setChecked(False)
    s3._emit()
    assert not errs
    assert got == [[("COM3", AL, None)]]

    # 7. demo: port None + setup dozwolony (ma średnicę i siłę)
    got.clear()  # zerowanie NIEopcjonalne:
    s4 = SelectorWindow(scan_source=src,  # bez tego got[0] to wpis
                        settings=FakeSettings(),  # z testu 6 (AL na COM3)
                        error_reporter=errs.append)
    s4.session_requested.connect(got.append)
    s4.rows[0].model_cb.setCurrentText(DEMO)  # i NIC więcej przed _emit
    assert not s4.rows[0].port_cb.isEnabled()
    assert s4.rows[0].dia_spin.isVisibleTo(s4.rows[0])
    assert s4.rows[0].force_spin.isVisibleTo(s4.rows[0])
    s4._emit()
    assert not errs
    demo_key = next(k for k in REGISTRY if REGISTRY[k] is DemoDriver)
    cfg = got[0]  # config z emitu (LISTA wierszy, nie wiersz!)
    assert cfg[0][0] is None and cfg[0][1] == demo_key
    assert set(cfg[0][2]) == {"diameter", "force"}

    # 8. legacy v1 (2-elementowe rows) restoruje się z setup OFF
    st_legacy = FakeSettings()
    st_legacy.d["rows"] = [["COM5", AL]]
    s5 = SelectorWindow(scan_source=src, settings=st_legacy,
                        error_reporter=errs.append)
    assert len(s5.rows) == 1
    assert s5.rows[0].port_cb.currentData() == "COM5"
    assert not s5.rows[0].setup_chk.isChecked()

    # 9. walidacje (regresje): bez portu, duplikat, zero wierszy
    errs.clear()
    s6 = SelectorWindow(scan_source=src, settings=FakeSettings(),
                        error_reporter=errs.append)
    s6._emit()                                    # AL + placeholder
    assert errs and s6.result() == 0
    errs.clear()
    s6.rows[0].port_cb.setCurrentIndex(1)         # COM3
    s6._add_row()
    s6.rows[1].port_cb.setCurrentIndex(1)         # też COM3
    s6._emit()
    assert errs and s6.result() == 0
    errs.clear()
    for row in list(s6.rows):
        s6._remove_row(row)
    s6._emit()
    assert errs and s6.result() == 0
    errs.clear()

    # 10. refresh: wybór portu zachowany po danych (regresja)
    s7 = SelectorWindow(scan_source=src, settings=FakeSettings(),
                        error_reporter=errs.append)
    s7.rows[0].port_cb.setCurrentIndex(2)         # COM5
    fake_ports.append(FakePort("COM7", "FTDI", vid=0x0403, pid=0x6001,
                               sn="B2"))
    s7.refresh_ports()
    assert s7.rows[0].port_cb.count() == 4
    assert s7.rows[0].port_cb.currentData() == "COM5"

    # 11. persystencja JSON (regresja): round-trip, cicha porażka I/O,
    #     uszkodzony plik, scenariusz przez selector
    import tempfile
    from pathlib import Path
    from ui.selector_window import JsonSettings

    with tempfile.TemporaryDirectory() as td:
        jpath = Path(td) / "settings.json"
        js = JsonSettings(jpath)
        js.setValue("rows", [["COM3", AL]])
        assert JsonSettings(jpath).value("rows") == [["COM3", AL]]

        bad = JsonSettings(Path(td) / "no-dir" / "x.json")
        bad.setValue("k", 1)
        assert bad.value("k") == 1

        (Path(td) / "broken.json").write_text("{nie-json", "utf-8")
        assert JsonSettings(Path(td) / "broken.json").value("x") is None

        got.clear()
        s8 = SelectorWindow(scan_source=src, settings=JsonSettings(jpath),
                             error_reporter=errs.append)
        s8.session_requested.connect(got.append)
        s8.rows[0].port_cb.setCurrentIndex(1)
        s8.rows[0].setup_chk.setChecked(False)
        s8._emit()
        assert got == [[("COM3", AL, None)]]
        s9 = SelectorWindow(scan_source=src, settings=JsonSettings(jpath),
                             error_reporter=lambda m: None)
        assert len(s9.rows) == 1
        assert s9.rows[0].port_cb.currentData() == "COM3"
        assert s9.rows[0].model_cb.currentText() == AL

    print("selector_window: OK")