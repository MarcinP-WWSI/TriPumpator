import sys
from PyQt6.QtWidgets import QApplication
from core.session import Session
from ui.selector_window import SelectorWindow


def main():
    app = QApplication(sys.argv)

    session = Session()

    selector = SelectorWindow()
    selector.session_requested.connect(session.start)
    selector.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
