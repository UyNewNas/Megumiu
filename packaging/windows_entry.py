"""Executable entry point used by PyInstaller."""

from multiprocessing import freeze_support

from megumiu.cli import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
