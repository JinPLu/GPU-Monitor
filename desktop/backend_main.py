"""PyInstaller entry point for the self-contained macOS app backend."""

from gpu_broker.cli import app


if __name__ == "__main__":
    app()
