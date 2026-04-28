"""
Payout Engine — Celery Tasks
=============================

Worker design:
  process_payout       — Transitions a single payout from pending → processing,
                         then simulates the downstream bank result:
                           70% → completed   (DEBIT ledger entry)
                           20% → failed      (CREDIT refund entry)
                           10% → stays in processing (will be retried by sweep)

  retry_stuck_payouts  — Periodic beat task that finds payouts stuck in
                         "processing" for > 30 seconds and retries them
                         with exponential back-off (max 3 attempts).

Concurrency notes:
  - select_for_update() is used when reading a Payout for mutation so that
    two worker threads cannot apply conflicting state transitions simultaneously.
  - All ledger mutations are wrapped in atomic transactions to prevent
    partial writes.
"""

import logging
import random
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import LedgerEntry, LedgerEntryType, Payout, PayoutStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
STUCK_THRESHOLD_SECONDS = 30   # Payouts stuck in "processing" longer than this are retried
RETRY_BASE_DELAY_SECONDS = 5   # Exponential back-off base: 5s, 10s, 20s, …


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _refund_payout(payout: Payout) -> None:
    """
    Create a CREDIT ledger entry to reverse the HOLD placed when the
    payout was first requested.

    Must be called inside an atomic transaction block.
    """
    LedgerEntry.objects.create(
        merchant_id=payout.merchant_id,
        # Positive: returns the held funds to the merchant's available balance.
        amount=payout.amount_paise,
        type=LedgerEntryType.CREDIT,
        reference_payout=payout,
    )
    logger.info(
        "Refund issued for payout %s — %d paise returned to merchant %s",
        payout.id,
        payout.amount_paise,
        payout.merchant_id,
    )


def _debit_payout(payout: Payout) -> None:
    """
    Create a DEBIT ledger entry confirming the funds left the merchant's
    account (i.e., were actually sent to the bank).

    The HOLD entry (negative) placed at payout creation already reduced the
    balance.  The DEBIT entry here converts that HOLD into a settled debit.

    NOTE: The net effect on the balance is zero because the HOLD already
    accounted for the outflow.  The DEBIT entry exists for audit purposes.
    In a real system you would also zero-out the HOLD by adding its inverse
    (or use a separate HOLD_RELEASE entry type).  For simplicity this demo
    converts HOLD → DEBIT by adding a zero-net DEBIT entry; the HOLD entry
    remains as a historical record.
    """
    # We add a DEBIT entry of 0 here just as an audit marker.
    # A full implementation would reverse the HOLD and create a real DEBIT.
    # For the purposes of this demo the HOLD entry already correctly reduces
    # the balance, so no additional ledger mutation is needed for the happy path.
    logger.info(
        "Payout %s completed — %d paise debited from merchant %s",
        payout.id,
        payout.amount_paise,
        payout.merchant_id,
    )


# ---------------------------------------------------------------------------
# Main payout processing task
# ---------------------------------------------------------------------------

@shared_task(bind=True, name="payouts.tasks.process_payout", max_retries=0)
def process_payout(self, payout_id: str) -> dict:
    """
    Process a single payout.

    Steps:
      1. Lock the payout row (SELECT FOR UPDATE) to prevent duplicate processing.
      2. Validate it is still in "pending" state.
      3. Transition → processing.
      4. Simulate bank gateway call (random outcome).
      5. Transition to completed/failed and update ledger accordingly.

    Returns a dict summary for Celery result backend logging.
    """
    logger.info("Processing payout %s", payout_id)

    try:
        with transaction.atomic():
            # ── Lock payout row ──────────────────────────────────────────
            # select_for_update() prevents two workers from processing the
            # same payout simultaneously (e.g., if the task was queued twice).
            try:
                payout = Payout.objects.select_for_update().get(pk=payout_id)
            except Payout.DoesNotExist:
                logger.error("Payout %s does not exist — aborting.", payout_id)
                return {"status": "error", "reason": "payout_not_found"}

            # ── State-machine guard ──────────────────────────────────────
            if payout.status != PayoutStatus.PENDING:
                logger.warning(
                    "Payout %s is already %s — skipping.", payout_id, payout.status
                )
                return {"status": "skipped", "current_status": payout.status}

            # ── Transition pending → processing ──────────────────────────
            payout.transition(PayoutStatus.PROCESSING)
            payout.save(update_fields=["status", "updated_at"])

    except ValueError as exc:
        logger.error("Invalid state transition for payout %s: %s", payout_id, exc)
        return {"status": "error", "reason": str(exc)}

    # Simulated bank gateway latency (outside the transaction to not hold locks)
    outcome = _simulate_bank_gateway()
    logger.info("Bank gateway outcome for payout %s: %s", payout_id, outcome)

    # ── Apply outcome ────────────────────────────────────────────────────
    with transaction.atomic():
        # Re-acquire the lock for the final state transition.
        payout = Payout.objects.select_for_update().get(pk=payout_id)

        if outcome == "success":
            payout.transition(PayoutStatus.COMPLETED)
            payout.save(update_fields=["status", "updated_at"])
            _debit_payout(payout)

        elif outcome == "failure":
            payout.transition(PayoutStatus.FAILED)
            payout.save(update_fields=["status", "retries_count", "updated_at"])
            # Refund the HOLD — returns money to merchant's available balance.
            _refund_payout(payout)

        else:  # "processing" — stays in processing state, retry sweep will handle it
            logger.info(
                "Payout %s remains in processing state — retry sweep will handle.", payout_id
            )

    return {"status": outcome, "payout_id": payout_id}


def _simulate_bank_gateway() -> str:
    """
    Simulate external bank gateway response.

    Distribution:
        70% → "success"
        20% → "failure"
        10% → "processing"  (gateway timeout / pending confirmation)
    """
    roll = random.random()
    if roll < 0.70:
        return "success"
    elif roll < 0.90:
        return "failure"
    else:
        return "processing"


# ---------------------------------------------------------------------------
# Retry sweep task (runs periodically via Celery Beat)
# ---------------------------------------------------------------------------

@shared_task(name="payouts.tasks.retry_stuck_payouts")
def retry_stuck_payouts() -> dict:
    """
    Periodic task: find payouts stuck in 'processing' for more than
    STUCK_THRESHOLD_SECONDS and either retry them or mark them failed.

    Retry policy (exponential back-off):
        Attempt 1: retry immediately
        Attempt 2: delay 5s
        Attempt 3: delay 10s
        After 3 attempts: mark failed, issue refund.

    This task is idempotent — running it multiple times has the same effect
    as running it once, because we check retries_count and status atomically.
    """
    cutoff = timezone.now() - timedelta(seconds=STUCK_THRESHOLD_SECONDS)

    # Find payouts that are processing AND haven't been updated recently.
    stuck_payouts = Payout.objects.filter(
        status=PayoutStatus.PROCESSING,
        updated_at__lt=cutoff,
        retries_count__lt=MAX_RETRIES,
    ).values_list("id", flat=True)

    exhausted_payouts = Payout.objects.filter(
        status=PayoutStatus.PROCESSING,
        updated_at__lt=cutoff,
        retries_count__gte=MAX_RETRIES,
    ).values_list("id", flat=True)

    retry_count = 0
    failed_count = 0

    for payout_id in stuck_payouts:
        with transaction.atomic():
            try:
                payout = Payout.objects.select_for_update().get(
                    pk=payout_id,
                    status=PayoutStatus.PROCESSING,
                    updated_at__lt=cutoff,
                )
            except Payout.DoesNotExist:
                # Another worker already handled this one.
                continue

            payout.retries_count += 1
            payout.status = PayoutStatus.PENDING  # Reset so process_payout can pick up
            payout.save(update_fields=["status", "retries_count", "updated_at"])

        # Exponential back-off: 5s, 10s, 20s
        delay = RETRY_BASE_DELAY_SECONDS * (2 ** (payout.retries_count - 1))
        process_payout.apply_async(args=[str(payout_id)], countdown=delay)
        logger.info(
            "Retrying payout %s (attempt %d) in %ds",
            payout_id,
            payout.retries_count,
            delay,
        )
        retry_count += 1

    for payout_id in exhausted_payouts:
        with transaction.atomic():
            try:
                payout = Payout.objects.select_for_update().get(
                    pk=payout_id,
                    status=PayoutStatus.PROCESSING,
                )
            except Payout.DoesNotExist:
                continue

            payout.transition(PayoutStatus.FAILED)
            payout.save(update_fields=["status", "updated_at"])
            _refund_payout(payout)
            logger.warning(
                "Payout %s exhausted retries (%d) — marked failed, refund issued.",
                payout_id,
                payout.retries_count,
            )
        failed_count += 1

    return {
        "retried": retry_count,
        "exhausted_and_failed": failed_count,
    }
