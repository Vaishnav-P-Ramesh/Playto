"""
Celery application entry-point for payout_engine.

Imported by workers via:  celery -A payout_engine worker -l info
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "payout_engine.settings")

app = Celery("payout_engine")

# Read Celery configuration from Django settings (keys prefixed with CELERY_).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all INSTALLED_APPS.
app.autodiscover_tasks()
