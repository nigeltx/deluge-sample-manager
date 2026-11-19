"""Start the app: python -m dsm"""
import logging
import sys


def main() -> int:
    from dsm import APP_NAME, __version__
    from dsm.logs import install_excepthook, setup
    setup()
    logging.getLogger("dsm").info("Starting %s %s", APP_NAME, __version__)
    install_excepthook()
    from PySide6.QtWidgets import QApplication
    from dsm.settings import Settings
    from dsm.ui.main_window import MainWindow
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    w = MainWindow(Settings.load())
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
