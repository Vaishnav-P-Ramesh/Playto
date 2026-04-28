"""
Management command: seed_demo_data

Creates demo merchants and credits their accounts so the UI is immediately
usable without manual API calls.

IDEMPOTENT: Safe to run multiple times. Uses get_or_create to avoid duplicates.

Usage:
    python manage.py seed_demo_data

Creates:
  - One "Demo Merchant" with ₹10,000 (1,000,000 paise) starting balance
  - Ready for immediate payout testing
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from payouts.models import LedgerEntry, LedgerEntryType, Merchant


class Command(BaseCommand):
    help = "Seed demo merchant and initial ledger credit (idempotent)."

    def handle(self, *args, **options):
        with transaction.atomic():
            # ─ Get or create demo merchant (idempotent) ──────────────────
            merchant, created = Merchant.objects.get_or_create(name="Demo Merchant")
            if created:
                self.stdout.write(self.style.SUCCESS(f"✓ Created merchant: {merchant.id}"))
            else:
                self.stdout.write(f"  Merchant already exists: {merchant.id}")

            # ─ Credit merchant with ₹10,000 (always creates new entry) ────
            # Note: Each run creates a NEW ledger entry. This is intentional:
            # ledger entries are immutable and append-only. If you run this
            # command twice, the merchant's balance doubles (as it should).
            LedgerEntry.objects.create(
                merchant=merchant,
                amount=1_000_000,   # ₹10,000 = 1,000,000 paise
                type=LedgerEntryType.CREDIT,
            )
            self.stdout.write(self.style.SUCCESS(
                f"✓ Credited ₹10,000 (1,000,000 paise) to {merchant.name} ({merchant.id})"
            ))
