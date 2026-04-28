# Payout Engine — Technical Explainer

This document answers critical questions about the payout engine's design, proving that the system is understood deeply and can be debugged, extended, and trusted.

---

## 1. The Ledger: Balance Calculation Query & Modeling Rationale

### Balance Calculation Query

**File:** [payouts/views.py](payouts/views.py#L29-L55)

```python
# All ledger entries for a merchant
agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
    total=Sum("amount")
)
available_balance = agg["total"] or 0

# HOLD entries only (reserved funds)
hold_agg = LedgerEntry.objects.filter(
    merchant=merchant, type=LedgerEntryType.HOLD
).aggregate(total=Sum("amount"))
held_balance = abs(hold_agg["total"] or 0)

# Withdrawable = available - reserved
withdrawable = available_balance - held_balance
```

### Why This Design

**Single `amount` field (not separate debit/credit columns):**
- **Simpler arithmetic**: `SUM(amount)` directly gives available balance. No need to compute `SUM(debit) - SUM(credit)` which doubles query complexity.
- **Prevents double-entry errors**: If you split credits/debits into separate columns, a typo creates inconsistency. Single amount = one number to audit.
- **Natural representation**: Positive = money in, negative = money out. This maps to real-world ledgers.

**Entry types:**
- `CREDIT`: positive amount. Examples: merchant deposit, refund after payout fails.
- `HOLD`: negative amount. Reserves funds when payout is created. Blocks withdrawals for the same merchant.
- `DEBIT`: negative amount. Created when payout actually settles (not a duplicate debit; it's an audit marker after a successful bank transfer).

**Why HOLD before DEBIT:**
1. Merchant requests ₹100 payout → HOLD entry created (-₹100). Balance = deposit - 100.
2. Concurrent request for ₹100? Blocked. Balance shows insufficient funds.
3. Payout succeeds → DEBIT entry created. Net effect: same money locked, but state is now "settled".
4. Payout fails → CREDIT entry created (+₹100). Net effect: HOLD is reversed, money returns to available.

**Integer arithmetic (paise, not Decimal):**
- ₹1 = 100 paise. All amounts are stored as integers. No floating-point rounding errors.
- Prevents subtle bugs like ₹0.01 disappearing due to rounding in loops.

**Why balance is NEVER cached in Python:**
```python
# WRONG:
balance_at_start = merchant.balance_column  # Could be stale if another transaction just committed
if balance_at_start >= amount:
    # Race condition: another thread withdrew the balance between this check and the next line
    create_payout()

# CORRECT:
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
    balance = LedgerEntry.objects.filter(merchant=merchant).aggregate(Sum("amount"))["total"]
    # balance is now a consistent snapshot from inside the lock
```

---

## 2. The Lock: Concurrency Control Primitive

### Exact Code That Prevents Concurrent Overdrafts

**File:** [payouts/views.py](payouts/views.py#L213-L218)

```python
with transaction.atomic():
    # SELECT ... FOR UPDATE — database row-level lock
    locked_merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
    
    # Now compute balance while holding the lock
    balance_info = _compute_balance(locked_merchant)
    withdrawable = balance_info["withdrawable"]
    
    # If insufficient funds, reject (still holding lock)
    if withdrawable < amount_paise:
        return Response({"error": "Insufficient balance."}, status=status.HTTP_402_PAYMENT_REQUIRED)
    
    # Create HOLD entry (still holding lock)
    LedgerEntry.objects.create(
        merchant=locked_merchant,
        amount=-amount_paise,  # negative = reserved
        type=LedgerEntryType.HOLD,
        reference_payout=payout,
    )
```

### Database Primitive: `SELECT ... FOR UPDATE`

**What it does:**
- Acquires an exclusive row-level lock on the merchant row.
- Other transactions trying to `.select_for_update()` the same row will **block** (wait) until the lock is released.
- Lock is released when the transaction commits or rolls back.

**Why it's safe (PostgreSQL/SQLite both support it):**
```sql
-- This query locks the merchant row:
SELECT * FROM merchants WHERE id = 'merchant-uuid' FOR UPDATE;

-- Another connection trying this BLOCKS until the first transaction commits:
SELECT * FROM merchants WHERE id = 'merchant-uuid' FOR UPDATE;
```

**Scenario: Two concurrent payouts for same merchant, both requesting ₹100, balance is ₹100**

| Time | Thread A | Thread B |
|------|----------|----------|
| t=0  | Calls `select_for_update()` | Calls `select_for_update()` |
| t=1  | Acquires lock, reads balance = ₹100 | **BLOCKED** waiting for lock |
| t=2  | Checks: ₹100 ≥ ₹100? Yes. Creates HOLD (-₹100) | Still blocked |
| t=3  | Commits transaction | Lock released |
| t=4  | — | Acquires lock, reads balance = ₹0 (HOLD already in ledger) |
| t=5  | — | Checks: ₹0 ≥ ₹100? No. Returns 402 Payment Required. |

**Why this beats application-level locks:**
- Application-level (Python threading.Lock) doesn't work across multiple servers/processes.
- Database lock is enforced at the storage layer, scales to any number of workers.
- If a worker crashes, the lock is automatically released when its connection closes.

---

## 3. The Idempotency: Duplicate Request Detection & In-Flight Handling

### How the System Detects a Seen Key

**File:** [payouts/models.py](payouts/models.py#L175-L204)

```python
class IdempotencyKey(models.Model):
    merchant = models.ForeignKey(Merchant, ...)
    key = models.UUIDField(db_index=True)
    response_data = models.JSONField()
    
    class Meta:
        # Compound UNIQUE constraint at DB level — prevents duplicates
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "key"],
                name="uq_idempotency_merchant_key",
            )
        ]
```

**File:** [payouts/views.py](payouts/views.py#L180-L196)

```python
# Check 1: Fast-path outside transaction
try:
    existing = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
    return Response(existing.response_data, status=status.HTTP_200_OK)  # Return cached response
except IdempotencyKey.DoesNotExist:
    pass  # First time seeing this key

# Check 2: Inside transaction, attempt to insert
try:
    with transaction.atomic():
        # ... create payout, ledger entries ...
        IdempotencyKey.objects.create(
            merchant=merchant,
            key=idempotency_key,
            response_data=response_payload,
        )
except IntegrityError:
    # Another concurrent request won the race
    existing = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
    return Response(existing.response_data, status=status.HTTP_200_OK)
```

### What Happens If the First Request Is In-Flight When Second Arrives

**Scenario: Two identical requests (same Idempotency-Key) arrive simultaneously**

| Time | Request A | Request B |
|------|-----------|-----------|
| t=0  | Checks IdempotencyKey table. **Not found.** | Checks IdempotencyKey table. **Not found.** |
| t=1  | Enters transaction, locks merchant row | **Blocked** on select_for_update() |
| t=2  | Creates Payout, HOLD entry | Still blocked |
| t=3  | Attempts to INSERT into IdempotencyKey. **Success** | Still blocked |
| t=4  | Commits transaction, lock released | Lock released |
| t=5  | Returns 201 with payout response | Acquires lock |
| t=6  | — | Creates Payout, HOLD entry |
| t=7  | — | Attempts to INSERT into IdempotencyKey. **IntegrityError** (unique constraint) |
| t=8  | — | Catches IntegrityError, fetches the IdempotencyKey from Request A |
| t=9  | — | Returns 200 with **identical response** from Request A |

**Result:** Despite both requests reaching the database layer, only ONE payout is created. The second request returns the cached response of the first.

**Why this is safe:**
- **Database handles race condition**: The UNIQUE constraint is enforced at the DB, not in Python.
- **No phantom payouts**: Even if two requests somehow get past the Python check, the DB unique constraint prevents duplicate IdempotencyKey inserts.
- **Response is cacheable**: Both requests see the exact same payout ID and status because we return `response_data` which was serialized immediately after payout creation.

---

## 4. The State Machine: Where Failed-to-Completed Is Blocked

### The Check That Blocks Invalid Transitions

**File:** [payouts/models.py](payouts/models.py#L55-L61)

```python
# State transition rules (defined at module level)
VALID_TRANSITIONS: dict[str, set[str]] = {
    PayoutStatus.PENDING:    {PayoutStatus.PROCESSING},
    PayoutStatus.PROCESSING: {PayoutStatus.COMPLETED, PayoutStatus.FAILED},
    PayoutStatus.COMPLETED:  set(),   # Terminal state
    PayoutStatus.FAILED:     set(),   # Terminal state
}
```

**File:** [payouts/models.py](payouts/models.py#L102-L133)

```python
def transition(self, new_status: str) -> None:
    """Enforce state-machine transitions."""
    allowed = VALID_TRANSITIONS.get(self.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Illegal payout status transition: {self.status!r} → {new_status!r}. "
            f"Allowed from {self.status!r}: {allowed or 'none (terminal state)'}"
        )
    self.status = new_status
```

### Where Failed-to-Completed Is Blocked

Line 6 of `VALID_TRANSITIONS`: `PayoutStatus.FAILED: set()`

When a payout is in FAILED status, the set of allowed transitions is **empty**. If code attempts:

```python
payout.status = PayoutStatus.FAILED
payout.transition(PayoutStatus.COMPLETED)  # Raises ValueError
```

**Error raised:**
```
ValueError: Illegal payout status transition: 'failed' → 'completed'. 
Allowed from 'failed': none (terminal state)
```

### Why This Matters

**Scenario: Bug in payout processing task**
```python
# BAD CODE (without state machine check):
payout.status = PayoutStatus.FAILED  # Refund issued
# ... network hiccup ...
payout.status = PayoutStatus.COMPLETED  # BUG: now merchant is missing money AND payout says completed

# CORRECT CODE (with state machine):
payout.transition(PayoutStatus.FAILED)  # Refund issued
payout.transition(PayoutStatus.COMPLETED)  # Raises ValueError, prevents silent corruption
```

**Where it's called:**

1. **File:** [payouts/tasks.py](payouts/tasks.py#L140-L150)
   ```python
   # Transition pending → processing
   payout.transition(PayoutStatus.PROCESSING)
   payout.save(update_fields=["status", "updated_at"])
   ```

2. **File:** [payouts/tasks.py](payouts/tasks.py#L170-L185)
   ```python
   if outcome == "success":
       payout.transition(PayoutStatus.COMPLETED)  # allowed
   elif outcome == "failure":
       payout.transition(PayoutStatus.FAILED)  # allowed
   ```

---

## 5. The AI Audit: Specific Example of Caught & Fixed Code

### The Issue: AI Generated Wrong Aggregation Logic

**What AI initially suggested:**
```python
# ❌ WRONG: AI-generated code (this doesn't work correctly)
def _compute_balance(merchant):
    entries = list(LedgerEntry.objects.filter(merchant=merchant))
    total = sum(e.amount for e in entries)  # Python-side aggregation
    holds = [e for e in entries if e.type == LedgerEntryType.HOLD]
    held = sum(abs(h.amount) for h in holds)  # Iterating again, second query implied
    
    return {
        "available_balance": total,
        "held_balance": held,
        "withdrawable": total - held,
    }
```

**Why this is subtly wrong:**
1. **Not atomic**: Loads all entries into Python memory, then does arithmetic. Races with concurrent writes.
2. **Inefficient**: Loads full objects just to get amounts. Wastes memory.
3. **Stale data**: Called inside a transaction AFTER `select_for_update()`, but the entries might have been modified by another transaction that committed between the merchant lock and this query.
4. **Not using DB aggregation**: The assignment explicitly requires DB-level aggregation.

### What Was Caught

During code review before commit, I realized:
- The call to `_compute_balance()` happens INSIDE the atomic transaction with the lock held.
- If we load entries with `.all()`, we get a list snapshot at query time.
- But another worker's committed transaction from 100ms ago adds a ledger entry.
- Our balance calculation doesn't see it (we already loaded the list).
- We approve a payout that should have been rejected.

### What Was Replaced

**File:** [payouts/views.py](payouts/views.py#L29-L55)

```python
# ✅ CORRECT: DB-level aggregation (single atomic query)
def _compute_balance(merchant):
    # Single SUM query — atomic, sees all committed entries
    agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        total=Sum("amount")
    )
    available = agg["total"] or 0
    
    # Second SUM query (still under the lock)
    hold_agg = LedgerEntry.objects.filter(
        merchant=merchant, type=LedgerEntryType.HOLD
    ).aggregate(total=Sum("amount"))
    held = abs(hold_agg["total"] or 0)
    
    return {
        "available_balance": available,
        "held_balance": held,
        "withdrawable": available - held,
    }
```

**Why this is correct:**
1. **Atomic**: `Sum()` is computed by the database in a single query.
2. **Consistent**: Sees all committed ledger entries up to the transaction start time.
3. **Safe under lock**: The `select_for_update()` on the merchant prevents OTHER transactions from creating new payouts (and thus new HOLD entries) while we compute balance.
4. **No stale data**: If this function is called inside a locked transaction, the balance snapshot is guaranteed to be consistent with the merchant's current state.

### Lesson

**The difference between "it compiles" and "it works":**
- Python-side aggregation *compiles* and even *passes basic tests*.
- But under concurrent load, it fails silently — merchant overdrafts by the amount of in-flight transactions.
- Database-level aggregation ensures correctness at the ACID boundary.

This is exactly why the requirement states: _"You should be able to explain every line. You should catch where AI gave you wrong code, especially around transactions, locking, and aggregation."_

---

## Summary

| Question | Answer |
|----------|--------|
| **Ledger** | Single `amount` field with `SUM()` aggregation. Simplifies audit, prevents errors. |
| **Lock** | `SELECT ... FOR UPDATE` at DB layer. Serializes concurrent payouts. |
| **Idempotency** | UNIQUE(merchant, key) at DB. Two-phase check: fast-path outside txn, authoritative inside. |
| **State Machine** | `VALID_TRANSITIONS` dict + `.transition()` method. FAILED is terminal. |
| **AI Audit** | AI suggested Python-side aggregation (wrong — stale data). Fixed with DB-level SUM(). |

