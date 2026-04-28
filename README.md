# Payout Engine

A production-grade Django REST API for merchant payouts with database-level concurrency control, idempotency, and a strict state machine. All code is AI-native: every line is commented and understandable.

**Live Demo:** [Deployed URL will go here after deployment]

---

## Quick Start (5 minutes)

### Prerequisites
- Python 3.10+
- Django 6.0
- PostgreSQL (or SQLite for local dev)
- Redis (for Celery)

### Local Development Setup

**1. Clone and enter the workspace:**
```bash
cd playto
```

**2. Create a virtual environment:**
```bash
python -m venv venv
source venv/Scripts/activate  # Windows
# or
source venv/bin/activate  # macOS/Linux
```

**3. Install dependencies:**
```bash
pip install -r requirements.txt
```

**4. Initialize the database:**
```bash
python manage.py migrate
```

**5. Seed demo data:**
```bash
python manage.py seed_demo_data
```

This creates a demo merchant with ₹10,000 starting balance.

**6. Start the development server:**
```bash
python manage.py runserver
```

Server runs at `http://localhost:8000`.

**7. (Optional) Start Celery worker** (for background payout processing):
```bash
# In another terminal, with venv activated:
celery -A payout_engine worker --loglevel=info
```

---

## Architecture

### Core Components

**Merchant Model**
- UUID primary key
- Name and created_at timestamp
- No balance column (always computed from ledger)

**Ledger Entry Model**
- Immutable, append-only records
- Types: CREDIT (positive), HOLD (negative reserve), DEBIT (negative settled)
- Single `amount` field (paise, integer)
- Balance = SUM(amount) for a merchant

**Payout Model**
- Status: PENDING → PROCESSING → COMPLETED/FAILED
- Mandatory idempotency key (Idempotency-Key header)
- Retries count for retry sweep

**IdempotencyKey Model**
- Unique constraint on (merchant, key) to prevent duplicates
- Stores full response JSON for replay

### Request Flow

```
POST /api/v1/payouts/
  ↓
[1] Validate input (outside transaction)
  ↓
[2] Check idempotency key (fast-path read)
  ↓
[3-9] Atomic transaction:
      [4] Lock merchant row (SELECT FOR UPDATE)
      [5] Compute balance (DB aggregation)
      [6] Check sufficient funds
      [7] Create Payout (status=pending)
      [8] Create HOLD ledger entry (reserves funds)
      [9] Insert IdempotencyKey (unique constraint guards race conditions)
  ↓
[10] Enqueue Celery task (outside transaction)
  ↓
Response: 201 with Payout details
```

### Concurrency Control

**Database-Level Lock (SELECT FOR UPDATE)**
- Merchant row is locked when processing a payout request
- Concurrent requests for same merchant are serialized at the DB layer
- Prevents race conditions that would cause overdrafts

**Example:** Merchant has ₹100. Two concurrent requests for ₹100 each:
1. Request A locks merchant, checks balance (₹100), creates HOLD (-₹100) → balance now ₹0
2. Request B locks merchant, checks balance (₹0 < ₹100), rejects with 402

### Idempotency Handling

**Two-Phase Idempotency Check:**
1. **Fast-path** (outside transaction): Check if idempotency key exists in DB. Return cached response immediately.
2. **Authoritative** (inside transaction): UNIQUE constraint on (merchant, key) prevents duplicate inserts if two identical requests race.

**Result:** Retrying the same request (same Idempotency-Key) always returns the same response, never creates duplicate payouts.

### State Machine

```
PENDING
  ↓ (start processing)
  ↓
PROCESSING
  ├─→ COMPLETED (payout succeeded)
  │     ↓ (terminal, no further transitions)
  │
  └─→ FAILED (payout failed, refund issued)
        ↓ (terminal, no further transitions)
```

All transitions go through `payout.transition()` which validates against VALID_TRANSITIONS. Invalid transitions raise ValueError → 400.

**Blocked transition:** FAILED → COMPLETED is explicitly forbidden.

---

## API Endpoints

### Merchants

**Create Merchant:**
```bash
POST /api/v1/merchants/
Content-Type: application/json

{ "name": "Acme Inc" }
```

**List Merchants:**
```bash
GET /api/v1/merchants/
```

**Get Merchant:**
```bash
GET /api/v1/merchants/{merchant_id}/
```

**Get Balance:**
```bash
GET /api/v1/merchants/{merchant_id}/balance/
```

Response:
```json
{
  "merchant_id": "...",
  "merchant_name": "Demo Merchant",
  "available_balance": 1000000,
  "held_balance": 0,
  "withdrawable": 1000000
}
```

**Credit Merchant (demo only):**
```bash
POST /api/v1/merchants/{merchant_id}/credit/
Content-Type: application/json

{ "amount_paise": 100000 }
```

**Get Ledger:**
```bash
GET /api/v1/merchants/{merchant_id}/ledger/
```

Returns last 100 ledger entries.

### Payouts

**Create Payout:**
```bash
POST /api/v1/payouts/
Content-Type: application/json
Idempotency-Key: {UUID}
X-Merchant-Id: {merchant_uuid}

{
  "amount_paise": 50000,
  "bank_account_id": "ICICI_ACC_123"
}
```

Response (201 Created):
```json
{
  "id": "payout-uuid",
  "merchant": "merchant-uuid",
  "amount_paise": 50000,
  "bank_account_id": "ICICI_ACC_123",
  "status": "pending",
  "retries_count": 0,
  "created_at": "2024-04-27T10:00:00Z",
  "updated_at": "2024-04-27T10:00:00Z"
}
```

**List Payouts:**
```bash
GET /api/v1/payouts/?merchant_id={merchant_uuid}
```

**Get Payout:**
```bash
GET /api/v1/payouts/{payout_id}/
```

---

## Testing

### Run All Tests
```bash
python manage.py test payouts.tests
```

### Run Specific Test Classes

**Concurrency Test** (verifies no overdrafts):
```bash
python manage.py test payouts.tests.ConcurrencyTest
```

**Idempotency Test** (verifies duplicate detection):
```bash
python manage.py test payouts.tests.IdempotencyTest
```

### What the Tests Verify

**ConcurrencyTest:**
- Two simultaneous payout requests for the same merchant
- Only ONE succeeds; the other gets 402 (insufficient balance)
- Final balance is correct (not negative)

**IdempotencyTest:**
- Reusing the same Idempotency-Key returns identical response
- No duplicate payouts are created
- Different keys create separate payouts

**StateMachineTest:**
- Valid transitions work (PENDING → PROCESSING → COMPLETED)
- Invalid transitions raise ValueError (e.g., COMPLETED → PENDING)
- Terminal states cannot transition further

**BalanceTest:**
- Credits increase balance
- HOLDs reduce withdrawable balance
- Balance is computed correctly from ledger

**InsufficientFundsTest:**
- Payout exceeding balance returns 402
- No payout is created if funds are insufficient

---

## Database Models

### Merchant
```python
id: UUID (primary key)
name: CharField
created_at: DateTimeField (auto)
```

### Payout
```python
id: UUID (primary key)
merchant: ForeignKey(Merchant)
amount_paise: BigIntegerField
bank_account_id: CharField
status: CharField (choices: pending, processing, completed, failed)
retries_count: PositiveSmallIntegerField
created_at: DateTimeField (auto)
updated_at: DateTimeField (auto)
```

### LedgerEntry
```python
id: BigAutoField (primary key)
merchant: ForeignKey(Merchant)
amount: BigIntegerField (positive or negative, in paise)
type: CharField (choices: CREDIT, HOLD, DEBIT)
reference_payout: ForeignKey(Payout, nullable)
created_at: DateTimeField (auto)
```

### IdempotencyKey
```python
id: BigAutoField (primary key)
merchant: ForeignKey(Merchant)
key: UUIDField
response_data: JSONField
created_at: DateTimeField (auto)

Unique Constraint: (merchant, key)
```

---

## Environment Variables

### Local Development (SQLite)
```env
DEBUG=True
SECRET_KEY=dev-secret-key
DJANGO_DB=sqlite  # default
```

### Production (PostgreSQL)
```env
DEBUG=False
SECRET_KEY=<generate-a-secret>
DJANGO_DB=postgres
DB_NAME=payout_db
DB_USER=postgres
DB_PASSWORD=<secure-password>
DB_HOST=db.example.com
DB_PORT=5432
CELERY_BROKER_URL=redis://redis.example.com:6379/0
CELERY_RESULT_BACKEND=redis://redis.example.com:6379/0
```

---

## Deployment

### Deploy to Render

**1. Create a new PostgreSQL database on Render.**

**2. Create a new Web Service, connect to this repo.**

**3. Set environment variables:**
```
DEBUG=False
DJANGO_DB=postgres
DB_NAME=<from Render>
DB_USER=<from Render>
DB_PASSWORD=<from Render>
DB_HOST=<from Render>
DB_PORT=5432
SECRET_KEY=<generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
```

**4. Build/start commands:**
- Build: `pip install -r requirements.txt && python manage.py migrate`
- Start: `gunicorn payout_engine.wsgi:application --bind 0.0.0.0:$PORT`

**5. Add a background job service for Celery:**
```
Command: celery -A payout_engine worker --loglevel=info
```

**6. Seed the database:**
```bash
python manage.py seed_demo_data
```

### Deploy to Railway

Similar process. Use Railway's Postgres and Redis add-ons.

---

## Key Design Decisions

### Why No Cached Balance Column

Balance is **never** stored in the Merchant table. It is **always** computed from the ledger.

**Rationale:**
- Single source of truth: ledger entries are append-only and immutable
- Prevents stale cache bugs
- Consistency: if a ledger entry is created, balance immediately reflects it

### Why BigInteger (Paise, Not Decimal)

All amounts are stored as integers (paise). ₹1.00 = 100 paise.

**Rationale:**
- Eliminates floating-point rounding errors
- Clearer intent: money is discrete units, not fractions
- Faster database queries (integer arithmetic is faster)

### Why SELECT FOR UPDATE (Not Application Lock)

We use database-level locking, not Python threading.Lock or file locks.

**Rationale:**
- Works across multiple servers/workers
- Automatic cleanup if a worker crashes
- Consistent with the ACID model

### Why IdempotencyKey Table

We store the full response, not just a flag "this request was seen".

**Rationale:**
- Idempotent clients need to get the exact same response, not a generic "already processed" message
- Auditable: we can see which response was cached
- Supports payment gateway callbacks (webhook idempotency)

---

## Debugging & Troubleshooting

### Balance Is Negative (Shouldn't Happen)

This is a data integrity bug. Check:
1. Are all payout payouts properly transitioning HOLD → DEBIT or HOLD → refund?
2. Run: `SELECT SUM(amount) FROM ledger_entries WHERE merchant_id = '<id>'` to verify.

### Payout Stuck in Processing

The retry sweep task should pick it up after 30 seconds. Check:
1. Is Celery worker running?
2. Is Redis available?
3. Check Celery logs for errors.

### Idempotency Key Not Working

Check:
1. Are you sending the exact same UUID in the Idempotency-Key header?
2. Is the database write succeeding (check logs for IntegrityError)?

### Cannot Create Payout (402 Immediately)

1. Check merchant's balance: `GET /api/v1/merchants/{id}/balance/`
2. Check ledger: `GET /api/v1/merchants/{id}/ledger/`
3. Are there pending payouts with HOLDs? Those reduce withdrawable balance.

---

## Code Walkthrough

See [EXPLAINER.md](EXPLAINER.md) for detailed explanations of:
1. **The Ledger**: Balance calculation query and modeling
2. **The Lock**: Database primitive preventing concurrent overdrafts
3. **The Idempotency**: Duplicate detection and in-flight handling
4. **The State Machine**: Where failed-to-completed is blocked
5. **The AI Audit**: Example of caught and fixed AI-generated code

---

## File Structure

```
playto/
├── manage.py
├── requirements.txt
├── EXPLAINER.md                      # Technical details (read first)
├── db.sqlite3                        # SQLite (dev only)
├── payout_engine/
│   ├── settings.py                   # Django config
│   ├── urls.py                       # Root URL routing
│   ├── wsgi.py                       # WSGI entry point
│   ├── asgi.py
│   └── celery.py                     # Celery config
└── payouts/
    ├── models.py                     # Merchant, Payout, LedgerEntry, IdempotencyKey
    ├── views.py                      # REST API endpoints
    ├── serializers.py                # DRF serializers
    ├── tasks.py                      # Celery tasks
    ├── urls.py                       # Payout app URLs
    ├── tests.py                      # Concurrency + Idempotency tests
    ├── admin.py
    ├── apps.py
    └── management/
        └── commands/
            └── seed_demo_data.py     # CLI to seed merchants
```

---

## Production Checklist

- [ ] Set `DEBUG=False` in production
- [ ] Generate and store a strong `SECRET_KEY`
- [ ] Use PostgreSQL (not SQLite)
- [ ] Use Redis for Celery (not local broker)
- [ ] Set `ALLOWED_HOSTS` to your domain
- [ ] Enable HTTPS/SSL
- [ ] Run migrations: `python manage.py migrate`
- [ ] Seed initial merchants: `python manage.py seed_demo_data`
- [ ] Collect static files: `python manage.py collectstatic`
- [ ] Run tests: `python manage.py test`
- [ ] Set up monitoring/alerting for payout processing
- [ ] Set up log aggregation (for Celery warnings)
- [ ] Run the retry sweep periodically (Celery Beat)

---

## License

MIT

---

## Questions?

Refer to [EXPLAINER.md](EXPLAINER.md) for design rationale and code walkthrough.

