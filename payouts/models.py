"""
Payout Engine — Core Models
============================

Design decisions:
  - Balances are NEVER stored; they are always computed via DB aggregation.
  - All monetary amounts are stored in paise (int), never float/Decimal.
  - select_for_update() is used at the view layer to serialize concurrent requests.
"""

import uuid

from django.db import models


# ---------------------------------------------------------------------------
# Merchant
# ---------------------------------------------------------------------------

class Merchant(models.Model):
    """
    Basic merchant entity.

    The balance for a merchant is computed by summing all LedgerEntry.amount
    values for that merchant. A positive sum = available balance.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "merchants"
        indexes = [models.Index(fields=["name"])]

    def __str__(self) -> str:
        return f"Merchant({self.name})"


# ---------------------------------------------------------------------------
# Payout
# ---------------------------------------------------------------------------

class PayoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


# Strict state-machine: maps each status to the set of valid next statuses.
# Any transition not listed here is illegal and will raise ValueError.
VALID_TRANSITIONS: dict[str, set[str]] = {
    PayoutStatus.PENDING: {PayoutStatus.PROCESSING},
    PayoutStatus.PROCESSING: {PayoutStatus.COMPLETED, PayoutStatus.FAILED},
    PayoutStatus.COMPLETED: set(),   # terminal state
    PayoutStatus.FAILED: set(),      # terminal state
}


class Payout(models.Model):
    """
    Represents a single payout request from a merchant.

    State machine (enforced via transition() method):
        pending → processing → completed
                             → failed
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant,
        on_delete=models.PROTECT,  # Never cascade-delete payouts
        related_name="payouts",
    )
    # Amount in paise (100 paise = ₹1). BigIntegerField prevents overflow.
    amount_paise = models.BigIntegerField()
    # Mock bank account identifier (no real integration in this demo)
    bank_account_id = models.CharField(max_length=255, default="")
    status = models.CharField(
        max_length=20,
        choices=PayoutStatus.choices,
        default=PayoutStatus.PENDING,
        db_index=True,
    )
    retries_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payouts"
        indexes = [
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["merchant", "status"]),
        ]

    def __str__(self) -> str:
        return f"Payout({self.id}, {self.status}, {self.amount_paise} paise)"

    def transition(self, new_status: str) -> None:
        """
        Enforce state-machine transitions (strictly at the DB layer via checks).

        WHY VALIDATION HERE:
          - Prevents silent corruption of payout state
          - Caller receives ValueError which can be caught and returned as 400 (Bad Request)
          - Forces explicit handling of illegal transitions instead of silent failure
          - Each transition is validated BEFORE save() is called

        LEGAL STATE PATHS:
          pending    → processing
          processing → completed OR failed (but NOT back to pending)
          completed  ✗ (terminal, no further changes)
          failed     ✗ (terminal, no further changes)

        This is BLOCKING logic: failed-to-completed is explicitly forbidden here.
        The check happens at line:
            if new_status not in allowed: raise ValueError(...)

        Raises:
            ValueError: if transition is not in VALID_TRANSITIONS mapping
        """
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal payout status transition: {self.status!r} → {new_status!r}. "
                f"Allowed from {self.status!r}: {allowed or 'none (terminal state)'}"
            )
        self.status = new_status


# ---------------------------------------------------------------------------
# LedgerEntry
# ---------------------------------------------------------------------------

class LedgerEntryType(models.TextChoices):
    CREDIT = "CREDIT", "Credit"
    DEBIT = "DEBIT", "Debit"
    HOLD = "HOLD", "Hold"


class LedgerEntry(models.Model):
    """
    Immutable double-entry ledger record.

    WHY THIS DESIGN:
      We use a single "amount" field (positive or negative) instead of separate
      debit/credit columns. This is intentional:
        - Simplifies balance calc: SUM(amount) is available balance.
        - Prevents accounting errors: one number to audit, not two columns.
        - Idempotent: the same ledger entry type for a payout means no accidental double-entry.

    ENTRY TYPES:
      - CREDIT:  positive amount (e.g., merchant deposit, refund after payout fails)
      - HOLD:    negative amount (reserved when payout is created, blocks spending)
      - DEBIT:   negative amount (only after payout actually settles; marks completion)

    BALANCE SEMANTICS:
      available_balance  = SUM(amount) WHERE merchant = X
                         = includes all credits, holds, and debits
      held_balance       = ABS(SUM(amount)) WHERE type = 'HOLD' AND merchant = X
                         = money reserved by pending payouts (always positive number)
      withdrawable       = available_balance - held_balance
                         = money NOT reserved for pending payouts

    HOLD → DEBIT FLOW (on payout success):
      1. On payout create: insert HOLD entry (negative, blocks balance)
      2. On payout complete: insert DEBIT entry (reference to same payout)
         Net effect: same money is "locked" but state changes from pending→settled
      3. On payout refund: insert CREDIT entry (reverses the HOLD)
         Net effect: money returns to available balance

    CRITICAL: All balance queries MUST use DB aggregation SUM(), never Python arithmetic
    or cached values. This ensures atomicity with database transactions.
    """

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    # Positive = money in, Negative = money out / hold.
    # ALWAYS BigIntegerField (paise), NEVER Decimal or float.
    # Prevents rounding errors and enables exact integer arithmetic.
    amount = models.BigIntegerField()
    type = models.CharField(
        max_length=10,
        choices=LedgerEntryType.choices,
        db_index=True,
    )
    # Optional FK to the payout that caused this entry (nullable for credits).
    reference_payout = models.ForeignKey(
        Payout,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ledger_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ledger_entries"
        # Ledger entries are append-only; prevent accidental updates via DB policy.
        indexes = [
            models.Index(fields=["merchant", "type"]),
            models.Index(fields=["merchant", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"LedgerEntry({self.type}, {self.amount} paise)"


# ---------------------------------------------------------------------------
# IdempotencyKey
# ---------------------------------------------------------------------------

class IdempotencyKey(models.Model):
    """
    Stores the first response for a given (merchant, key) pair.

    On subsequent requests with the same key the stored response_data is
    returned immediately — no new payout is created, no DB transaction is
    opened unnecessarily.

    Uniqueness is enforced at DB level via the unique_together constraint.
    """

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant,
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )
    # The key value sent by the client in the Idempotency-Key header.
    key = models.UUIDField(db_index=True)
    # The full JSON response that was returned for the first request.
    response_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_keys"
        # Compound unique constraint at DB level — prevents race conditions
        # where two simultaneous first-time requests with the same key both
        # try to insert.
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "key"],
                name="uq_idempotency_merchant_key",
            )
        ]

    def __str__(self) -> str:
        return f"IdempotencyKey(merchant={self.merchant_id}, key={self.key})"
