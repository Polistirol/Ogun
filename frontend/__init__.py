"""Frontend desktop nativo per il master agent (PySide6, niente browser)."""

__all__ = ["main"]


def main() -> int:
    from frontend.app import main as _main

    return _main()
