"""TriPumpator — wejście aplikacji.

Pętla pracy: okno wyboru -> sesja (okna pomp + panel główny) ->
po zakończeniu sesji powrót do okna wyboru. Jedyna droga wyjścia
z aplikacji: anulowanie okna wyboru — dlatego QuitOnLastWindowClosed
jest wyłączone (zamknięcie okien nie może zostawiać działających
wątków roboczych).
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from core.session import Session
from ui.selector_window import SelectorWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("TriPumpator")
    app.setOrganizationName("WWSI")
    app.setQuitOnLastWindowClosed(False)

    state = {"session": None, "quitting": False}

    # ---------------------------------------------------------- sesja

    def start_session(config) -> None:
        session = Session()
        try:
            session.start(config)
        except Exception as e:          # np. port COM zajęty/zaginiął
            QMessageBox.critical(
                None, "TriPumpator",
                f"Nie udało się wystartować sesji:\n{e}")
            open_selector()
            return
        state["session"] = session
        session.session_ended.connect(on_session_ended)

    def on_session_ended() -> None:
        state["session"] = None
        if not state["quitting"]:
            open_selector()

    # ------------------------------------------------------- selector

    def open_selector() -> None:
        cfg: dict = {}

        def grab(c):
            cfg["v"] = c

        selector = SelectorWindow()
        selector.session_requested.connect(grab)
        if selector.exec() == QDialog.DialogCode.Accepted:
            start_session(cfg["v"])
        else:
            state["quitting"] = True
            app.quit()

    # siatka awaryjna: cokolwiek zakończy app -> wątki w dół
    def on_about_to_quit() -> None:
        if (s := state["session"]) is not None:
            s.shutdown_all()

    app.aboutToQuit.connect(on_about_to_quit)

    # KLUCZ: główna pętla startuje PIERWSZA, selector otwierany w niej
    # dopiero po starcie. quit() wywołany przed exec() jest ignorowany
    # przez Qt — stąd poprzedni bug: X na selectorze przed startem
    # sesji zostawiał proces-wisielca (QuitOnLastWindowClosed wyłączone).
    QTimer.singleShot(0, open_selector)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()