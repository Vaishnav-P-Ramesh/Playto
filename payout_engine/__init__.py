"""
Make Celery available when Django starts so that shared_task decorators work.
"""

from .celery import app as celery_app  # noqa: F401

__all__ = ("celery_app",)
