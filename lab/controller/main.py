"""ASGI entrypoint for the OpsPilot Lab Controller service."""

from lab.controller.api import app

__all__ = ["app"]
