"""The web UI: FastAPI plus plain JavaScript, no build step."""

from .app import create_app

__all__ = ["create_app"]
