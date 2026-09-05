### Hour log for cleints

custom app fro client hours tracking

Centerpiece of this app is a **prepaid hour allocation engine**: clients buy
blocks of hours that expire, consultants log work against those blocks, and the
app produces a monthly statement of what was purchased, consumed, expired, and
(if consumption ever outran what was available) run into overdraft.

This document is the design record for that engine: the doctypes, the single
algorithm that moves hours around, and — edge case by edge case — the problem,
the decision, why it's correct, exactly what changed, and which test proves it.

---

## 0. Decisions, one line each

- **Retroactive consumption** — full recompute from source data on every insert; the old statement is kept and flagged superseded, never overwritten in place.
- **Expiry boundary** — inclusive: a block is usable for work logged *on* its expiry date, enforced in exactly one function (`is_block_usable`) called everywhere the check is needed.
- **Partial consumption across 3+ blocks** — greedy walk in expiry order, one `HL Allocation` row per block touched, however many that takes.
- **Zero and negative hours** — zero is a recorded no-op; a negative entry must name the original entry it corrects and can only restore hours to the exact blocks (and/or overdraft bucket) that entry actually drew from, walked in LIFO order.
- **Block bought after work was done** — blocks are ordered by expiry first, purchase date only breaks an exact tie; a block also can't cover work performed before its own purchase date.
- **Floating-point hours** — every calculation uses `Decimal` built from `str(value)`, never from the raw float; only the final DB write converts back to float.
- **Overdraft** — hours with no unexpired block to cover them become one dedicated allocation row (`purchased_block = None`, `is_overdraft = 1`), reported as its own statement field; never rejected, never drawn from an expired block.

### How to run it

```bash
bench --site <site> set-config allow_tests true   # once, on a dev/test site
bench --site <site> run-tests --app hour_log --skip-test-records
bench --site <site> execute hour_log.hour_log_for_cleints.examples.generate_sample.run
```
The last command regenerates `examples/sample_input.json` / `sample_output.json` from a live run and prints each month's revision history.

---

## 1. Architecture

All engine code lives under one module, `hour_log_for_cleints`:

| DocType | Role | Mutable after creation? |
|---|---|---|
| **HL Purchased Block** | one row per block of hours a client bought | No — append-only source data |
| **HL Consumption Entry** | one row per unit of work logged, including negative "correction" entries | No — append-only source data |
| **HL Allocation** | which block (or the overdraft bucket) each consumption entry drew from / restored | Never edited directly — only ever produced by the engine |
| **HL Monthly Statement** (+ **HL Monthly Statement Line**) | a versioned, per-period snapshot for reporting | Never edited directly — only ever produced by the engine |

Only **HL Purchased Block** and **HL Consumption Entry** are source data — a
person (or an integration) creates those. Everything else is a *derived*,
fully rebuildable projection of those two tables, produced by
`hour_log_for_cleints/engine.py`:

```
hour_log_for_cleints/
  engine.py                  the one algorithm (_replay) + everything built on it
  hl_test_helpers.py          shared test fixtures (make_client / make_block / make_entry)
  api.py                      two whitelisted endpoints (generate_statement, statement_history)
  doctype/
    hl_purchased_block/       controller: validates dates/hours, blocks edits/deletes after use
    hl_consumption_entry/     controller: validates zero/negative-hours rules, blocks deletes
    hl_allocation/            controller: refuses writes from anywhere but the engine
    hl_monthly_statement/     controller: refuses writes from anywhere but the engine
    hl_monthly_statement_line/
  tests/test_engine.py        one test class per edge case, end to end
  examples/
    generate_sample.py         regenerates sample_input.json / sample_output.json from a live run
    sample_input.json           the exact source data behind the example
    sample_output.json          the statements/allocations that data produces
```

**Why append-only.** Recomputing history only works if the inputs to the
recompute never change out from under it. If a Purchased Block's hours could be
edited after allocations already exist against it, "replay from source data"
would replay something that no longer matches what actually happened. Both
`HL Purchased Block` and `HL Consumption Entry` therefore refuse to be edited
once the engine has used them (checked in `validate()`), and refuse to be
deleted at all (checked in `on_trash()`, and backed by `delete: 0` in each
DocType's permissions as defense in depth). To reduce hours, you add a negative
correction entry (Edge Case 4) — you never edit or delete the original.

**Why the derived tables are locked down.** `HL Allocation` and
`HL Monthly Statement` both refuse to be written to by anything except the
engine itself (checked via a `frappe.flags.in_hl_recompute` marker the engine
sets around its own writes). This is what makes "the allocation history" and
"the statement" trustworthy artifacts instead of just another table someone
could hand-edit.

---

## 2. The one algorithm

Every hour that ever moves between a Consumption Entry and a Purchased Block
moves through exactly one function pair in `engine.py`: `_allocate_forward`
(spend) and `_allocate_reversal` (undo). Both are called from `_replay`, which
is the only place that ever reads Purchased Block / Consumption Entry data to
decide an allocation:

```
_replay(client, as_of_date=None):
    blocks  = Purchased Blocks for client, ordered by (expiry_date, purchase_date, name)
    remaining = {block: block.hours_purchased for block in blocks}
    entries = Consumption Entries for client, ordered by (worked_on, creation, name)

    for entry in entries:
        if entry.hours == 0:      continue                       # no-op (Edge Case 4)
        if entry.hours > 0:       _allocate_forward(entry, ...)   # spend  (Edge Case 3 / overdraft)
        else:                     _allocate_reversal(entry, ...)  # undo   (Edge Case 4)

    return remaining, allocations   # pure function of blocks + entries, nothing else
```

```
_allocate_forward(entry, hours_needed, blocks, remaining):
    left = hours_needed
    for block in blocks:                         # expiry order, purchase_date only a tie-break
        if left <= 0: break
        if block.expiry_date  <  entry.worked_on: continue        # expired  -> skip (Edge Case 2)
        if block.purchase_date > entry.worked_on: continue        # didn't exist yet -> skip (Edge Case 5)
        if remaining[block] <= 0:                 continue
        draw = min(remaining[block], left)
        remaining[block] -= draw
        left -= draw
        record Allocation(entry, block, +draw)                    # one row per split (Edge Case 3)
    if left > 0:
        record Allocation(entry, block=None, +left, is_overdraft=True)   # never silently dropped
```

```
_allocate_reversal(entry, amount_to_undo, forward_ledger):
    for prior_draw in reversed(forward_ledger[entry.reverses]):   # LIFO: last drawn, first undone
        if amount_to_undo <= 0: break
        restore = min(prior_draw.remaining_reversible, amount_to_undo)
        remaining[prior_draw.block] += restore    # or: reduce the overdraft bucket, if block is None
        amount_to_undo -= restore
        record Allocation(entry, prior_draw.block, -restore)      # exact original block, never arbitrary
```

`recompute_allocations(client)` is `_replay` with `as_of_date=None`, persisted:
it deletes every `HL Allocation` row for the client and re-inserts exactly what
the replay produces, then rewrites every block's cached `hours_remaining`. It
never *patches* — always a full delete-and-rebuild — which is what makes a
single backdated or corrected entry produce exactly the result it would have
produced had it been entered in the right order to begin with, no matter how
many other blocks and entries exist.

`generate_monthly_statement(client, year, month)` calls `recompute_allocations`
first, then reports on the slice of history one period covers — it never reads
a previously generated statement to compute a number (see Edge Case 1).

---

## 3. Edge cases

### Edge Case 1 — Retroactive consumption

**Problem, in plain English.** A consultant forgets to log 2 hours from three
weeks ago. They add the entry today, dated back then. January's statement was
already sent to the client. What happens to it?

**Decision.** The statement is *recomputed*, not patched. Adding the entry
triggers a full replay of the client's entire history (`recompute_allocations`,
then `generate_monthly_statement` for every already-published period on or
after the entry's month — `regenerate_statements_from`). If the replay's
numbers match what's already published, nothing new is created (no needless
revision churn). If they differ, a **new revision** is created — `revision + 1`,
`is_latest = 1` — while the old statement is kept, flagged `is_latest = 0`, and
linked via `superseded_by` / `previous_revision`. Nothing is overwritten in
place.

**Why this is correct.**
- *Why recomputation is necessary:* a backdated entry doesn't just add hours to
  its own month — because replay always proceeds in `worked_on` order
  regardless of insertion order, it can claim capacity from a block a *later*
  entry had been using, reshuffling that later entry's split too. Patching
  "just this month's total" would silently miss that. Only a full replay from
  source data (Purchased Block + Consumption Entry) is guaranteed to match what
  would have happened had the entry been on time. This is also why a new
  Purchased Block cascades the same way (`HL Purchased Block.after_insert`) —
  buying a block after the fact can retroactively change which block earlier
  work should have drawn from.
- *How determinism is preserved:* `_replay` is a pure function of two ordered
  queries (blocks by expiry/purchase/name, entries by worked_on/creation/name).
  Given the same rows, it always produces the same allocations — proven by
  `test_recompute_is_deterministic_and_replayable`, which runs the exact same
  replay twice and diffs the results. Generating a statement twice with no new
  data returns the *same document*, not a new revision — proven by
  `test_regenerating_with_no_new_data_does_not_create_a_redundant_revision`.
  Computing March never depends on having computed February first: nothing in
  `_replay` reads `HL Monthly Statement` — a statement is consulted only to
  decide whether a new revision is needed and to link revision history, never
  to compute a number.

**Code changed.** `engine.py`: `recompute_allocations`, `generate_monthly_statement`,
`regenerate_statements_from`, `_snapshot_key` (the no-op-detector),
`get_statement_history`. `hl_consumption_entry.py`: `after_insert` /
`on_update` call `_recompute_and_cascade`. `hl_purchased_block.py`:
`after_insert` calls `regenerate_statements_from` scoped from the block's own
`purchase_date`.

**Tests.** `TestEdgeCase1RetroactiveConsumption` (revision bump, superseded
flag visible, no-op idempotency, determinism) and
`test_a_late_purchased_early_expiring_block_reopens_an_already_published_statement`
(a new block, not just a new entry, can also reopen a statement).

---

### Edge Case 2 — Expiry boundary

**Problem.** A block expires 31 March. Work is logged *on* 31 March. Usable, or
not?

**Decision: inclusive.** `is_block_usable(expiry_date, worked_on)` returns
`worked_on <= expiry_date`. A block purchased 1 Jan expiring 31 Mar is usable
for work logged on 31 Mar itself; work logged 1 Apr cannot use it.

**Why this rule.** "Expires on 31 March" reads naturally as "valid *through*
31 March" — the same convention as a credit card's printed expiry date. The
alternative (exclusive) would make a hard-copy client statement look wrong to a
human reading it ("it says the block covers through the 31st, but my
March-31st entry got rejected"). Inclusive is also the safer commercial
default: it never rejects a client's legitimately-timed request; exclusive
would sometimes make a client feel short-changed by exactly one day.

**How consistency is maintained.** `is_block_usable` is the *only* place this
comparison is written. `_allocate_forward` (spend), `_closing_balance` /
`_expired_unused_in_period` (statement balances) — every one of them calls this
one function instead of repeating `<=` vs `<` inline. There is exactly one
place to get this decision wrong, and it's documented right there.

**Code changed.** `engine.py`: `is_block_usable`.

**Tests.** `TestEdgeCase2ExpiryBoundary`:
`test_work_on_the_expiry_date_itself_can_use_the_block` and
`test_work_the_day_after_expiry_cannot_use_the_block_and_goes_to_overdraft`
(the day after, the block is skipped entirely — and per the overdraft decision
below, the shortfall is recorded rather than raising, and the expired block is
proven untouched).

---

### Edge Case 3 — Consumption across multiple blocks (and the overdraft bucket)

**Problem.** One consumption entry needs more hours than any single block
holds — it might need three or more blocks. And if it needs *more than every
unexpired block combined* can supply, what then? Reject it? Silently under-
allocate? Draw from an expired block anyway?

**Decision.** `_allocate_forward` walks blocks in priority order, drawing as
much as each has available until the entry's hours are covered or the blocks
run out. Every non-zero draw — however many blocks it takes — becomes its own
`HL Allocation` row, numbered by `sequence`. If every unexpired block is
exhausted and hours are still left over, the remainder becomes a **single
overdraft row**: `purchased_block = None`, `is_overdraft = 1`. It is never
rejected (no `frappe.throw`), never silently dropped, and never satisfied by
drawing from a block whose expiry has already passed — the eligibility check
(`is_block_usable`) is evaluated identically whether or not overdraft ends up
being needed.

**Why the allocation algorithm is correct.** It's a straightforward greedy
walk over an already-correctly-ordered block list (see Edge Case 5 for the
order itself): draw the minimum of "what's needed" and "what this block has,"
move to the next block only once the current one is exhausted. Greedy is
optimal here because blocks are homogeneous once ordered — there's no
scenario where holding back hours from an earlier-priority block helps satisfy
the entry, since a later-priority block never expires *sooner*.

**Why every split is recorded.** The statement's promised "consumption detail
— for each entry, which block(s) it drew from and how many hours from each" is
only answerable if every partial draw is its own row. It also generalizes
overdraft for free: overdraft is just "one more destination in the same loop,"
so it participates in the exact same forward-ledger bookkeeping a block does —
which is what lets Edge Case 4's reversal treat it identically to a block.

**Why overdraft, not rejection.** The hours were genuinely worked; a
`frappe.throw` would make that work vanish from the record entirely, which is
worse than reporting a shortfall. Overdraft is scoped to the reporting period
(`overdraft_hours` on the statement — see the field table below) so it's
visible on the exact statement where it happened, ready to be settled by a
future purchase or a separate invoice.

**Code changed.** `engine.py`: `_allocate_forward` (overdraft row on shortfall,
in place of the earlier `frappe.throw`), `_allocate_reversal` (handles
`block is None`), `recompute_allocations` (persists `is_overdraft`),
`_overdraft_in_period` (statement-level rollup). `hl_allocation.json`:
`purchased_block` no longer required; new `is_overdraft` field.
`hl_monthly_statement.json` / `hl_monthly_statement_line.json`: new
`overdraft_hours` / `is_overdraft` fields.

**Tests.** `TestEdgeCase3MultiBlockSplit`
(`test_one_entry_splits_across_three_blocks_in_expiry_order`, asserting all
three rows and the exact per-block draw). `TestOverdraft`
(`test_shortfall_becomes_a_recorded_overdraft_row_not_an_error`,
`test_overdraft_never_draws_from_an_already_expired_block`,
`test_statement_reports_overdraft_for_the_period`,
`test_correcting_an_overdrafted_entry_reduces_the_overdraft_first`).

---

### Edge Case 4 — Zero and negative hours

**Problem.** What does a 0-hour entry mean? And a correction of −2 hours —
which block gets those 2 hours back? Whichever block happens to have room
right now is the wrong answer: it can quietly credit a block the original
entry never touched.

**Decision.**
- **Zero hours** is a valid, deliberate no-op: the entry is recorded (e.g. a
  logged-but-non-billable call) but produces zero `HL Allocation` rows.
  Checked first in `_replay`'s loop — `if hours == 0: continue`.
- **Negative hours** is a correction and *must* name the entry it corrects
  (`reverses`, enforced in `HLConsumptionEntry.validate`). It can never undo
  more than that entry (net of any earlier corrections against it) actually
  granted — also enforced in `validate`, by summing prior corrections against
  the same `reverses` target.
- **Reversal is LIFO**: it undoes the *last* thing the original entry's forward
  split touched, first. Concretely: the forward split for one entry is
  recorded, in order, as a list of `{block, amount}` rows (`forward_ledger`);
  a correction walks that list **in reverse**, restoring `min(what's left to
  reverse, what this row still holds)` from each row until the correction
  amount is exhausted. Because overdraft (Edge Case 3) can only ever be the
  *last* row of a forward split (blocks are always tried first), LIFO also
  means **a correction pays down overdraft before it ever touches a block** —
  which matches how you'd want a shortfall settled in practice, for free,
  with no special-case code.

**Why this is correct.**
- *Why allocation history is required:* without a per-entry, per-block record
  of exactly what was drawn (`forward_ledger`, built while replaying the
  *forward* direction), a reversal has no way to know which block(s) the
  original entry actually used — it would have to guess, and "whichever block
  has room" is exactly the arbitrary behavior the brief rules out.
- *How the reversal algorithm finds the correct blocks:* it never queries "all
  blocks with room" — it only ever iterates the specific list recorded for
  `entry.reverses`. That list is scoped to one consumption entry, so a
  correction is structurally incapable of touching a block that entry didn't
  draw from, regardless of what else exists with spare capacity.
- LIFO vs FIFO for *which* of several touched blocks unwinds first when a
  correction is smaller than the full split is a free choice — both satisfy
  "only ever the exact blocks used." LIFO was chosen because it reads as a
  standard undo (last action first) and, as above, resolves overdraft first
  with no extra logic.

**Code changed.** `hl_consumption_entry.py`: `validate` →
`_validate_zero_or_positive`, `_validate_negative_correction` (caps the
correction against `original.hours + already_corrected`).
`engine.py`: `_allocate_reversal` (walks `reversed(forward_ledger[...])`).

**Tests.** `TestEdgeCase4ZeroAndNegativeHours`
(`test_correction_restores_hours_to_the_exact_original_blocks_only`,
`test_multiple_partial_corrections_stack_up_to_the_original_amount`,
`test_zero_hour_entry_is_a_no_op`) plus the validation-layer tests in
`test_hl_consumption_entry.py` (`test_zero_hours_entry_is_allowed`,
`test_positive_entry_cannot_set_reverses`,
`test_negative_entry_requires_reverses`,
`test_negative_entry_cannot_reverse_a_correction`,
`test_correction_cannot_exceed_original_hours`,
`test_two_partial_corrections_cannot_together_exceed_original`) and
`TestOverdraft.test_correcting_an_overdrafted_entry_reduces_the_overdraft_first`.

---

### Edge Case 5 — A block bought after work was done

**Problem.** Block A is purchased first but expires last. Block B is purchased
later but expires sooner. Which one gets used for work that both could cover?

**Decision.** Blocks are ordered `expiry_date asc, purchase_date asc, name asc`
(`_get_ordered_blocks`) and always walked in that fixed order — **purchase date
only breaks a tie when two blocks share the exact same expiry date**; it never
outranks expiry. On top of that ordering, a block is only a *candidate* at all
for a given entry if it had already been purchased by the work date
(`block.purchase_date <= entry.worked_on`, checked in `_allocate_forward`) —
a block cannot retroactively cover work performed before it existed, even
though (per the paragraph above) its purchase date never lets it jump the
queue once it *is* a candidate.

**Why ordering by expiry differs from ordering by purchase date.** Purchase
order reflects nothing about urgency; expiry order reflects exactly how much
time is left to use a block before it's forfeited. Sorting by purchase date
would strand hours in whichever block happens to expire soonest, guaranteeing
some of it goes to waste — the opposite of what a client wants from a prepaid
balance. Expiry order is FEFO (first-expired-first-out), the same principle
ERPNext already uses for perishable stock batches, applied here to hours
instead of units.

**Why a purchase-date floor, separately from ordering.** Without it, a block
bought this afternoon could "cover" a job logged last week, before the block
existed — which is not what "purchase date is only a tie breaker" is meant to
license. The floor and the tie-break are two different rules answering two
different questions ("is this block eligible at all" vs "given several
eligible blocks, which one first") and both are needed.

**Code changed.** `engine.py`: `_get_ordered_blocks` (the sort), the
purchase-date floor added inside `_allocate_forward`.

**Tests.** `TestEdgeCase5ExpiryBeatsPurchaseOrder`:
`test_the_earlier_expiring_block_is_used_first_even_though_purchased_later`,
`test_purchase_date_only_breaks_ties_when_expiry_dates_are_equal`,
`test_a_block_cannot_retroactively_cover_work_performed_before_it_was_purchased`,
and `test_a_late_purchased_early_expiring_block_reopens_an_already_published_statement`
(a late-purchased, early-expiring block also demonstrates Edge Case 1's
regeneration cascade).

---

### Edge Case 6 — Floating-point precision

**Problem.** `0.1 + 0.2` in a Python (or JS, or almost any language's) binary
float is `0.30000000000000004`. Ten small allocations of 0.1h against a 1.0h
block can leave `9.999999999999998e-17` instead of exactly `0`. A client
statement showing `7.999999999999999` hours remaining is unacceptable.

**Decision: `decimal.Decimal`, entered and exited only through strings.**
- `D(value)` — `Decimal(str(flt(value)))` — converts any Frappe field value
  into an exact Decimal. Going through `str()` first is the whole trick:
  `Decimal(0.1)` still carries `0.1`'s binary-float error baked in
  (`Decimal('0.1000000000000000055511151231257827021181583404541015625')`),
  but `Decimal(str(0.1))` is the clean `Decimal('0.1')`, because `str()` on a
  Python float already renders the shortest decimal that round-trips to it.
- Every arithmetic step inside the engine — every `+=`, `-=`, comparison —
  happens on these `Decimal` values. Floats never touch each other directly.
- `to_float(value)` — quantize to `HOURS_PRECISION = Decimal("0.01")` with
  `ROUND_HALF_UP`, then `float(...)` — is the *only* place a value crosses back
  into a Frappe `Float` field, right before a `frappe.db.set_value` /
  `doc.insert()`. Hours are kept to hundredths (0.01h = 36 seconds), which
  comfortably covers the usual quarter/half-hour billing granularity.

**Why Decimal, not "round more carefully" or a fixed-point integer.**
Rounding after the fact doesn't fix the problem — the error is already baked
into every intermediate `+=`, so repeated rounding just moves *where* the drift
shows up. An integer-cents-style fixed-point (store hundredths as an `int`)
would also work, but `Decimal` needs no scaling/unscaling at every arithmetic
site and reads directly as the number it represents, which is less code to get
wrong for the same guarantee.

**Which code was changed.** `engine.py`: `D`, `to_float`, `HOURS_PRECISION`, and
every arithmetic site in `_replay` / `_allocate_forward` / `_allocate_reversal`
/ `_closing_balance` / `_expired_unused_in_period` / `_purchased_in_period` /
`_overdraft_in_period` — none of them do float arithmetic directly.

**Tests.** `TestEdgeCase6DecimalPrecision`:
`test_decimal_helper_avoids_the_binary_float_error` (the literal `0.1 + 0.2`
case), `test_ten_small_allocations_leave_an_exact_remaining_balance` (ten ×
0.1h against a 1.0h block lands on exactly `0.0`), and
`test_a_third_of_an_hour_three_times_sums_exactly` (three × 0.3h against a
0.9h block — the "not `7.999999999999999`" family of bug, reproduced and
proven fixed).

---

## 4. Statement fields, in full

| Field | Meaning |
|---|---|
| `opening_balance_hours` | Total usable (unexpired) hours across all blocks, an instant before this period began. |
| `hours_purchased_in_month` | Hours added (new blocks) during this period, regardless of when consumed. |
| `total_consumed_hours` | Net hours consumed this period: positive entries minus corrections, both dated in this period. |
| `hours_expired_unused_in_month` | Hours forfeited this period: whatever was left in a block the instant it expired. |
| `closing_balance_hours` | Total usable (unexpired) hours across all blocks, as of this period's last day. |
| `overdraft_hours` | Hours worked this period beyond what any unexpired block could cover, net of corrections. |
| `revision` / `is_latest` / `previous_revision` / `superseded_by` | Edge Case 1's revision history. |
| `lines` (`HL Monthly Statement Line`) | One row per allocation touching this period — the full consumption detail behind the totals above. |

`opening_balance_hours` / `closing_balance_hours` / `hours_expired_unused_in_month`
are all computed by replaying history up to a specific historical date
(`_replay(client, as_of_date=...)`) rather than by a second, hand-written
formula — so a statement's balances can never drift from what the allocation
algorithm itself would say happened.

---

## 5. Sample data

`hour_log_for_cleints/examples/sample_input.json` and `sample_output.json` are
not hand-typed — they're captured from a real run of the engine
(`hour_log_for_cleints/examples/generate_sample.py`, runnable again with
`bench --site <site> execute hour_log.hour_log_for_cleints.examples.generate_sample.run`,
which rolls back everything it creates). The scenario purchases four blocks and
seven consumption entries for one client across January–March 2026 and, in one
pass, exercises every edge case above: a multi-block split, a boundary-dated
entry, a correction, a retroactive entry that reshuffles an already-published
statement (twice, for two independent reasons), a late-purchased early-expiring
block, and a shortfall that lands in overdraft. `sample_output.json` includes
the full revision history for each month, so the January section alone shows
one statement's numbers changing (and the earlier revision staying, marked
superseded) once the retroactive entry lands.

---

## 6. Determinism and replayability

- `_replay` is a pure function of two ordered database reads — it has no
  hidden state, no reliance on wall-clock time, and no dependency on
  previously generated output.
- `recompute_allocations` is always a full delete-and-rebuild of
  `HL Allocation`, never an incremental patch.
- `generate_monthly_statement` always starts from a fresh
  `recompute_allocations` call; an existing `HL Monthly Statement` is read only
  to decide whether a new revision is needed (bookkeeping), never to compute a
  number.
- Computing March never depends on having computed February first — nothing in
  the algorithm reads another period's statement.
- Proven directly by `test_recompute_is_deterministic_and_replayable` (same
  inputs, run twice, byte-identical `HL Allocation` rows) and
  `test_regenerating_with_no_new_data_does_not_create_a_redundant_revision`
  (same inputs, generated twice, identical document — no spurious revision).

---

## 7. Assumptions

- **"Client" is the core ERPNext `Customer` doctype** (`required_apps =
  ["erpnext"]` in `hooks.py`), not a new master data doctype — no existing
  Customer data is touched, only linked to.
- **Purchased Block and Consumption Entry are append-only.** Corrections are
  the only way to reduce hours; deletion is blocked at both the permission
  level (`delete: 0`) and in each controller's `on_trash`.
- **A negative entry may only reverse a positive, original entry** — not
  another correction — keeping the reversal graph one level deep.
- **`overdraft_hours` is a per-period figure**, not a running unpaid balance
  carried across months. Tracking a cumulative "total unpaid overdraft" would
  be a natural next feature (e.g. an `opening_overdraft_hours` field) but
  wasn't asked for and isn't implemented.
- **Hours are tracked to two decimal places** (`HOURS_PRECISION`). This covers
  the usual quarter/half-hour billing granularity; a business that bills to
  the minute would change one constant.
- **A future per-block billing rate is not implemented** — recording every
  split as its own `HL Allocation` row (block + hours) already carries
  everything a rate-aware invoice would need per block; no `rate` field exists
  on `HL Purchased Block` because nothing in the brief asked for a cost
  calculation.

---

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app hour_log
```

### Running the tests

```bash
bench --site <site> set-config allow_tests true   # once, on a dev/test site
bench --site <site> run-tests --app hour_log --skip-test-records
```

`--skip-test-records` is recommended: this app's tests build their own fixtures
via `hl_test_helpers.py` and don't rely on Frappe's global test-record
bootstrapping (which, on a site without ERPNext's full demo fixtures, cascades
into unrelated failures unrelated to this app).

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/hour_log
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
