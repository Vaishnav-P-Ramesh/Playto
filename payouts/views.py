"""
Payout Engine — Views
======================

Critical concurrency/integrity notes (read before modifying):

1. LOCKING:
   The merchant row is locked via `select_for_update()` inside an atomic
   transaction block.  This serialises all concurrent payout requests for the
   same merchant at the DB level.  Two simultaneous requests will proceed
   one-at-a-time; the second will block until the first commits or rolls back.

2. IDEMPOTENCY:
   Before opening any transaction we check the IdempotencyKey table.  If a
   matching (merchant, key) record exists we return the stored response
   immediately.  The first request inserts the IdempotencyKey record inside
   the same atomic transaction that creates the payout, so if the transaction
   rolls back the key record is also rolled back — ensuring no "ghost" keys.

3. BALANCE COMPUTATION:
   Balance is NEVER read from a Python variable or a stored column.  It is
   always computed inside the DB transaction (after the lock) via:
       SUM(ledger_entries.amount) WHERE merchant_id = X
   This means Python never sees potentially stale cached data.

4. STATE TRANSITIONS:
   All status changes go through `payout.transition(new_status)` which
   validates the move against VALID_TRANSITIONS.  Invalid moves raise
   ValueError → 400 to the client.
"""

import uuid

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import IdempotencyKey, LedgerEntry, LedgerEntryType, Merchant, Payout, PayoutStatus
from .serializers import (
    BalanceSerializer,
    CreatePayoutSerializer,
    LedgerEntrySerializer,
    MerchantSerializer,
    PayoutSerializer,
)
from .tasks import process_payout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_balance(merchant: Merchant) -> dict:
    """
    Compute balance figures entirely inside the DB (CRITICAL for correctness).

    WHY DB AGGREGATION (not Python):
      - Atomic: computed within a single SQL query, not multiple round-trips
      - Consistent: snapshot is taken by the DB at query time
      - Thread-safe: no risk of stale Python values competing with other threads

    WHEN TO CALL:
      - ALWAYS inside atomic transaction block after select_for_update()
      - This ensures balance reflects all committed entries and locks prevent updates

    Returns:
      available_balance:  SUM(amount) for all entries (can be negative if debits > credits)
      held_balance:       ABS(SUM(amount)) for HOLD entries only (always positive)
      withdrawable:       available - held = funds not reserved for pending payouts

    EXAMPLE:
      Merchant credits: +100,000 paise
      Pending payout hold: -40,000 paise
      Result:
        available_balance = 60,000 (net of the hold)
        held_balance = 40,000 (amount reserved)
        withdrawable = 20,000 (can spend this much without canceling payouts)
    """
    # ─ Aggregate ALL ledger entries (single DB query) ─────────────────────
    agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        total=Sum("amount")
    )
    # SUM returns None if no rows exist; treat as 0 (zero balance)
    available = agg["total"] or 0

    # ─ Aggregate only HOLD entries to compute held_balance ─────────────────
    # HOLDs are stored as negative, so ABS(SUM(negative values)) = positive
    hold_agg = LedgerEntry.objects.filter(
        merchant=merchant, type=LedgerEntryType.HOLD
    ).aggregate(total=Sum("amount"))
    # Convert negative HOLD sum to positive held_balance
    held = abs(hold_agg["total"] or 0)

    return {
        "available_balance": available,
        "held_balance": held,
        # Withdrawable = available - reserved (pure integer math, no DB call)
        "withdrawable": available - held,
    }


# ---------------------------------------------------------------------------
# Merchant Views
# ---------------------------------------------------------------------------

class MerchantListCreateView(APIView):
    """GET /api/v1/merchants/  — list all merchants.
       POST /api/v1/merchants/ — create a merchant."""

    def get(self, request: Request) -> Response:
        merchants = Merchant.objects.all().order_by("created_at")
        return Response(MerchantSerializer(merchants, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = MerchantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        merchant = serializer.save()
        return Response(MerchantSerializer(merchant).data, status=status.HTTP_201_CREATED)


class MerchantDetailView(APIView):
    """GET /api/v1/merchants/<merchant_id>/"""

    def get(self, request: Request, merchant_id: str) -> Response:
        merchant = get_object_or_404(Merchant, pk=merchant_id)
        return Response(MerchantSerializer(merchant).data)


# ---------------------------------------------------------------------------
# Balance View
# ---------------------------------------------------------------------------

class BalanceView(APIView):
    """
    GET /api/v1/merchants/<merchant_id>/balance/

    Returns the merchant's current balance computed entirely via DB aggregation.
    No lock is held here — this is a read-only snapshot and is eventually
    consistent with in-flight transactions.
    """

    def get(self, request: Request, merchant_id: str) -> Response:
        merchant = get_object_or_404(Merchant, pk=merchant_id)
        balance = _compute_balance(merchant)
        payload = {
            **balance,
            "merchant_id": merchant.id,
            "merchant_name": merchant.name,
        }
        return Response(BalanceSerializer(payload).data)


# ---------------------------------------------------------------------------
# Payout Views
# ---------------------------------------------------------------------------

class PayoutCreateView(APIView):
    """
    POST /api/v1/payouts/

    Required headers:
        Idempotency-Key: <UUID>
        X-Merchant-Id:   <merchant UUID>

    Body (JSON):
        { "amount_paise": 50000, "bank_account_id": "ICICI_ACC_123" }

    Flow (strictly ordered — see module docstring for design rationale):
        1. Parse & validate input
        2. Check idempotency key (outside transaction — fast path)
        3. Open atomic DB transaction
        4. Lock merchant row with SELECT FOR UPDATE
        5. Compute balance via DB aggregation (inside locked transaction)
        6. Check sufficient funds
        7. Create Payout (status=pending)
        8. Insert HOLD ledger entry (negative amount)
        9. Insert IdempotencyKey record (rolls back if transaction fails)
       10. Commit transaction
       11. Enqueue Celery task (outside transaction — fire-and-forget)
    """

    def post(self, request: Request) -> Response:
        # ─────────────────────────────────────────────────────────────────
        # STEP 1: Input Validation (outside transaction)
        # ─────────────────────────────────────────────────────────────────
        # Parse and validate the request body using DRF serializer.
        # This is intentionally OUTSIDE any transaction so we fail fast on
        # invalid input without touching the database.
        serializer = CreatePayoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount_paise: int = serializer.validated_data["amount_paise"]
        bank_account_id: str = serializer.validated_data["bank_account_id"]

        # ─────────────────────────────────────────────────────────────────
        # Extract merchant identity and idempotency key from headers.
        # In production, merchant_id would be derived from authenticated JWT.
        # ─────────────────────────────────────────────────────────────────
        merchant_id = request.headers.get("X-Merchant-Id")
        if not merchant_id:
            return Response(
                {"error": "X-Merchant-Id header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        idempotency_key_str = request.headers.get("Idempotency-Key")
        if not idempotency_key_str:
            return Response(
                {"error": "Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            idempotency_key = uuid.UUID(idempotency_key_str)
        except ValueError:
            return Response(
                {"error": "Idempotency-Key must be a valid UUID."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        merchant = get_object_or_404(Merchant, pk=merchant_id)

        # ─────────────────────────────────────────────────────────────────
        # STEP 2: IDEMPOTENCY CHECK (outside transaction — fast path)
        # ─────────────────────────────────────────────────────────────────
        # IMPORTANT: This is a best-effort check to avoid unnecessary
        # transactions for duplicate requests. It is NOT the authoritative
        # guard — that is the database UNIQUE constraint below.
        #
        # Why check twice?
        #   1. Here: fast path for known retries (no transaction overhead)
        #   2. In DB: unique constraint prevents race conditions between
        #      two first-time requests arriving simultaneously
        #
        # What happens if two identical requests arrive in parallel:
        #   - Both skip this check (IdempotencyKey doesn't exist yet)
        #   - Both enter the transaction block
        #   - One wins the unique constraint check at Step 9
        #   - The other catches IntegrityError and fetches the winner's response
        try:
            existing = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
            # Return EXACTLY the same response as the first request.
            # Status is 200 (not 201) because this is a duplicate request.
            return Response(existing.response_data, status=status.HTTP_200_OK)
        except IdempotencyKey.DoesNotExist:
            pass  # First time seeing this key — proceed normally.

        # ─────────────────────────────────────────────────────────────────
        # STEPS 3-9: ATOMIC TRANSACTION BLOCK
        # ─────────────────────────────────────────────────────────────────
        # All of these steps happen atomically. If any step fails or raises,
        # the entire transaction rolls back (including the IdempotencyKey
        # record at Step 9, preventing "ghost" keys).
        try:
            with transaction.atomic():
                # ───────────────────────────────────────────────────────
                # STEP 4: DATABASE LOCK (the heart of concurrency control)
                # ───────────────────────────────────────────────────────
                # SELECT ... FOR UPDATE acquires a row-level EXCLUSIVE lock
                # at the database layer. This is NOT a Python-level lock;
                # it is enforced by PostgreSQL/SQLite itself.
                #
                # Semantics:
                #   - First request locks merchant row
                #   - Second concurrent request BLOCKS in the select_for_update() call
                #   - First request completes and commits
                #   - Second request now proceeds (continues execution)
                #
                # This is why concurrent overdrafts are impossible:
                # two requests for the same merchant are SERIALIZED at the DB.
                locked_merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)

                # ───────────────────────────────────────────────────────
                # STEP 5: BALANCE COMPUTATION (inside lock)
                # ───────────────────────────────────────────────────────
                # Compute balance AFTER the lock so we read the consistent
                # snapshot. The lock ensures no other transaction is modifying
                # ledger entries for this merchant while we read.
                balance_info = _compute_balance(locked_merchant)
                withdrawable: int = balance_info["withdrawable"]

                # ───────────────────────────────────────────────────────
                # STEP 6: INSUFFICIENT FUNDS CHECK
                # ───────────────────────────────────────────────────────
                # If merchant doesn't have enough withdrawable balance,
                # reject the request with HTTP 402 (Payment Required).
                # This fails BEFORE any ledger mutation, so the merchant's
                # balance remains unchanged.
                if withdrawable < amount_paise:
                    return Response(
                        {
                            "error": "Insufficient balance.",
                            "withdrawable_paise": withdrawable,
                            "requested_paise": amount_paise,
                        },
                        status=status.HTTP_402_PAYMENT_REQUIRED,
                    )

                # ───────────────────────────────────────────────────────
                # STEP 7: CREATE PAYOUT RECORD
                # ───────────────────────────────────────────────────────
                # Insert a new Payout with status=PENDING. This is the
                # authoritative record that the payout was requested.
                payout = Payout.objects.create(
                    merchant=locked_merchant,
                    amount_paise=amount_paise,
                    bank_account_id=bank_account_id,
                    status=PayoutStatus.PENDING,
                )

                # ───────────────────────────────────────────────────────
                # STEP 8: RESERVE FUNDS VIA HOLD LEDGER ENTRY
                # ───────────────────────────────────────────────────────
                # Create a HOLD entry (negative amount) that reserves funds.
                # This is CRITICAL for preventing overdrafts:
                #
                #   Scenario: Merchant has ₹100, requests two ₹60 payouts
                #   - Request 1 locks merchant, checks balance (₹100), creates HOLD (-₹60)
                #   - Balance now SUM = ₹40
                #   - Request 2 acquires lock, checks balance (₹40 < ₹60), rejected!
                #
                # The HOLD entry persists until:
                #   - Payout succeeds → DEBIT entry (settled state)
                #   - Payout fails → CREDIT entry (refund, returns to available)
                #
                # Why negative? Because we use SUM(amount) for balance.
                # Negative entries reduce the sum, blocking other withdrawals.
                LedgerEntry.objects.create(
                    merchant=locked_merchant,
                    amount=-amount_paise,          # negative == reserved
                    type=LedgerEntryType.HOLD,
                    reference_payout=payout,
                )

                # ───────────────────────────────────────────────────────
                # STEP 9: INSERT IDEMPOTENCY KEY (unique constraint)
                # ───────────────────────────────────────────────────────
                # Store the response so retries with the same key return
                # the identical response.
                #
                # RACE CONDITION HANDLING:
                #   If two simultaneous first-time requests both reach here
                #   (both bypassed Step 2 because IdempotencyKey didn't exist),
                #   the database's UNIQUE constraint will reject one of them.
                #
                #   Winner: successfully inserts IdempotencyKey
                #   Loser:  gets IntegrityError (caught at line "except IntegrityError")
                #
                # Why unique constraint (not just primary key)?
                #   UNIQUE(merchant, key) ensures that for a given merchant,
                #   each idempotency key can only be inserted once, globally.
                response_payload = PayoutSerializer(payout).data
                IdempotencyKey.objects.create(
                    merchant=locked_merchant,
                    key=idempotency_key,
                    response_data=response_payload,
                )

        except IntegrityError:
            # ───────────────────────────────────────────────────────
            # RACE CONDITION: Two identical first-time requests raced
            # ───────────────────────────────────────────────────────
            # The unique constraint on (merchant, key) rejected our insert.
            # This means another request already inserted the IdempotencyKey.
            # Fetch and return their response to ensure idempotency.
            existing = get_object_or_404(
                IdempotencyKey, merchant=merchant, key=idempotency_key
            )
            return Response(existing.response_data, status=status.HTTP_200_OK)

        # ─────────────────────────────────────────────────────────────────
        # STEP 11: ENQUEUE BACKGROUND WORKER (outside transaction)
        # ─────────────────────────────────────────────────────────────────
        # Dispatch the payout to Celery AFTER the transaction commits.
        # This happens OUTSIDE the atomic block to avoid holding the DB
        # connection open while Celery/Redis processes the task.
        #
        # Why countdown=1? Gives a small delay to ensure the database
        # connection has fully committed before the worker tries to read.
        process_payout.apply_async(args=[str(payout.id)], countdown=1)

        return Response(response_payload, status=status.HTTP_201_CREATED)


class PayoutListView(APIView):
    """GET /api/v1/payouts/?merchant_id=<uuid>"""

    def get(self, request: Request) -> Response:
        merchant_id = request.query_params.get("merchant_id")
        qs = Payout.objects.all().order_by("-created_at")
        if merchant_id:
            qs = qs.filter(merchant_id=merchant_id)
        return Response(PayoutSerializer(qs, many=True).data)


class PayoutDetailView(APIView):
    """GET /api/v1/payouts/<payout_id>/"""

    def get(self, request: Request, payout_id: str) -> Response:
        payout = get_object_or_404(Payout, pk=payout_id)
        return Response(PayoutSerializer(payout).data)


# ---------------------------------------------------------------------------
# Ledger View
# ---------------------------------------------------------------------------

class LedgerView(APIView):
    """GET /api/v1/merchants/<merchant_id>/ledger/"""

    def get(self, request: Request, merchant_id: str) -> Response:
        merchant = get_object_or_404(Merchant, pk=merchant_id)
        entries = LedgerEntry.objects.filter(merchant=merchant).order_by("-created_at")[:100]
        return Response(LedgerEntrySerializer(entries, many=True).data)


# ---------------------------------------------------------------------------
# Credit (Demo / Setup) View
# ---------------------------------------------------------------------------

class CreditMerchantView(APIView):
    """
    POST /api/v1/merchants/<merchant_id>/credit/

    Demo-only endpoint to add funds to a merchant's ledger so that payout
    flows can be tested without an external payment gateway.
    """

    def post(self, request: Request, merchant_id: str) -> Response:
        merchant = get_object_or_404(Merchant, pk=merchant_id)
        amount_paise = request.data.get("amount_paise")
        if not isinstance(amount_paise, int) or amount_paise <= 0:
            return Response(
                {"error": "amount_paise must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            entry = LedgerEntry.objects.create(
                merchant=merchant,
                amount=amount_paise,
                type=LedgerEntryType.CREDIT,
            )

        balance = _compute_balance(merchant)
        return Response(
            {
                "ledger_entry_id": entry.id,
                "credited_paise": amount_paise,
                **balance,
            },
            status=status.HTTP_201_CREATED,
        )
