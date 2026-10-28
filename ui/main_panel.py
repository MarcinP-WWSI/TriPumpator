"""Panel główny: zbiorczy widok stanowiska + operacje globalne.

Umowa:
    set_pumps([(pump_id, model_name), ...])   buduje/odświeża tabelę
    update_pump(pump_id, PumpStatus)          wpycha status pompy
    set_link(pump_id, online: bool)           link + licznik online
    clear_pumps()                             reset tabeli

Sygnały wychodzące (subskrybuje Session):
    start_all / stop_all / end_session

Panel nie zna handles ani okien pomp — dystrybucją komend zajmuje
się Session, dzięki czemu operacje globalne są identyczne dla
każdego modelu pompy.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QMainWindow, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QLabel,
)
from core.pump.base import PumpStatus, fmt_dispensed

_COLS = ["#", "Model", "Link", "Stan", "Kierunek", "Rate",
         "Dozowane", "Alarm"]


class MainPanel(QMainWindow):
    start_all = pyqtSignal()
    stop_all = pyqtSignal()
    end_session = pyqtSignal()
    open_programator = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TriPumpator — panel główny")
        self._rows: dict[int, int] = {}          # pump_id -> wiersz
        self._links: dict[int, bool] = {}        # pump_id -> online

        central = QWidget()
        root = QVBoxLayout(central)

        head = QHBoxLayout()
        self.online_label = QLabel("Online: 0/0")
        self.start_all_btn = QPushButton("▶ START wszystkie")
        self.start_all_btn.setStyleSheet(
            "font-weight: bold; background-color: #2e7d32; color: white;")
        self.start_all_btn.clicked.connect(self.start_all.emit)
        self.stop_all_btn = QPushButton("■ STOP wszystkie")
        self.stop_all_btn.setStyleSheet(
            "font-weight: bold; background-color: #b71c1c; color: white;")
        self.stop_all_btn.clicked.connect(self.stop_all.emit)
        self.end_btn = QPushButton("Zakończ sesję")
        self.end_btn.clicked.connect(self.end_session.emit)
        for b in (self.start_all_btn, self.stop_all_btn, self.end_btn):
            head.addWidget(b)

        self.btn_prog = QPushButton("⚙ Programator")
        self.btn_prog.clicked.connect(self.open_programator.emit)
        head.addWidget(self.btn_prog)

        head.addStretch()
        head.addWidget(self.online_label)
        root.addLayout(head)

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        root.addWidget(self.table)

        self.setCentralWidget(central)

    # ------------------------------------------------------------- API

    def set_pumps(self, pumps: list[tuple[int, str]]) -> None:
        """Zbuduj tabelę: [(pump_id, model_name), ...]."""
        self._rows.clear()
        self._links.clear()
        self.table.setRowCount(len(pumps))
        for i, (pid, name) in enumerate(pumps):
            self._rows[pid] = i
            self._links[pid] = False
            self._set_cell(i, 0, str(pid))
            self._set_cell(i, 1, name)
            self._set_cell(i, 2, "OFFLINE")
            for col in range(3, len(_COLS)):
                self._set_cell(i, col, "—")
        self._refresh_online()

    def update_pump(self, pump_id: int, st: PumpStatus) -> None:
        """Status pompy -> wiersz. Nieznane id: ignorowane."""
        if (row := self._rows.get(pump_id)) is None:
            return
        state = "pracuje" if st.running else \
                ("stop" if st.running is False else "—")
        self._set_cell(row, 3, state)
        self._set_cell(row, 4, st.direction or "—")
        self._set_cell(row, 5,
                       f"{st.rate:.3f} {st.rate_unit}"
                       if st.rate is not None else "—")
        self._set_cell(row, 6,
                       fmt_dispensed(st.dispensed, st.dispensed_unit)
                       if st.dispensed is not None else "—")
        self._set_cell(row, 7, st.alarm or "")

    def set_link(self, pump_id: int, online: bool) -> None:
        if pump_id not in self._rows:
            return
        self._links[pump_id] = online
        self._set_cell(self._rows[pump_id], 2,
                       "ONLINE" if online else "OFFLINE")
        self._refresh_online()

    def clear_pumps(self) -> None:
        self._rows.clear()
        self._links.clear()
        self.table.setRowCount(0)
        self._refresh_online()

    # --------------------------------------------------------- pomocnicze

    def _set_cell(self, row: int, col: int, text: str) -> None:
        self.table.setItem(row, col, QTableWidgetItem(text))

    def _refresh_online(self) -> None:
        n = sum(1 for v in self._links.values() if v)
        self.online_label.setText(f"Online: {n}/{len(self._links)}")

    def closeEvent(self, event) -> None:
        self.end_session.emit()
        event.accept()


if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    from core.pump.base import PumpStatus

    m = MainPanel()

    events: list[str] = []
    m.start_all.connect(lambda: events.append("start"))
    m.stop_all.connect(lambda: events.append("stop"))
    m.end_session.connect(lambda: events.append("end"))

    # 1. budowa tabeli
    m.set_pumps([(1, "WPI AL-1000"), (2, "Pompa demo"), (3, "WPI AL-1000")])
    assert m.table.rowCount() == 3
    assert m.table.item(0, 1).text() == "WPI AL-1000"
    assert m.table.item(2, 0).text() == "3"
    assert m.table.item(1, 2).text() == "OFFLINE"
    assert m.online_label.text() == "Online: 0/3"

    # 2. operacje globalne (klik = emit synchroniczny)
    m.start_all_btn.click()
    m.stop_all_btn.click()
    assert events == ["start", "stop"]

    # 3. status pompy -> wiersz (pump 2 -> wiersz 1)
    m.update_pump(2, PumpStatus(running=True, direction="WDR",
                                rate=120.0, rate_unit="mL/h",
                                dispensed=1.5, dispensed_unit="mL"))
    assert m.table.item(1, 3).text() == "pracuje"
    assert m.table.item(1, 4).text() == "WDR"
    assert m.table.item(1, 5).text() == "120.000 mL/h"
    assert m.table.item(1, 6).text() == "1.500 mL"
    assert m.table.item(1, 7).text() == ""

    # 4. stop i alarm nadpisują właściwe komórki
    m.update_pump(2, PumpStatus(running=False, alarm="S"))
    assert m.table.item(1, 3).text() == "stop"
    assert m.table.item(1, 7).text() == "S"

    # 4b. dozowanie auto-skaluje jednostki (µL przy małych wartościach)
    m.update_pump(1, PumpStatus(dispensed=50.0, dispensed_unit="µL"))
    assert m.table.item(0, 6).text() == "50.000 µL"
    m.update_pump(1, PumpStatus(dispensed=0.0004, dispensed_unit="mL"))
    assert m.table.item(0, 6).text() == "0.400 µL"

    # 5. statusy None -> myślniki
    m.update_pump(1, PumpStatus())
    assert m.table.item(0, 3).text() == "—"
    assert m.table.item(0, 5).text() == "—"

    # 6. nieznana pompa: no-op, bez wyjątku
    m.update_pump(99, PumpStatus(running=True))
    assert m.table.rowCount() == 3

    # 7. linki + licznik
    m.set_link(1, True)
    assert m.table.item(0, 2).text() == "ONLINE"
    assert m.online_label.text() == "Online: 1/3"
    m.set_link(2, True)
    m.set_link(3, False)
    assert m.online_label.text() == "Online: 2/3"
    m.set_link(1, False)
    assert m.table.item(0, 2).text() == "OFFLINE"
    assert m.online_label.text() == "Online: 1/3"

    # 8. czyszczenie
    m.clear_pumps()
    assert m.table.rowCount() == 0
    assert m.online_label.text() == "Online: 0/0"
    m.update_pump(1, PumpStatus(running=True))      # po czyszczeniu: no-op

    # 9. zamknięcie panelu = koniec sesji
    m.close()
    assert events == ["start", "stop", "end"]

    print("main_panel: OK")