# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Prepaid hour allocation engine.

Source of truth (append-only, never mutated in place):
	- HL Purchased Block      one row per block of hours a client bought
	- HL Consumption Entry    one row per unit of work logged against a client,
	                          including negative "correction" entries

Everything else is a *derived*, fully rebuildable projection of the two tables
above:
	- HL Allocation           which block(s) each consumption entry drew from / restored
	- HL Monthly Statement    a versioned, per-period snapshot for reporting

The single algorithm in this module (`_replay`) is the only place hours ever
move between a Consumption Entry and a Purchased Block. `recompute_allocations`
persists a full replay as the current ledger state; `generate_monthly_statement`
calls it, then reports on the slice of history one period covers. Nothing here
ever reads a previously generated HL Monthly Statement to compute a number —
statements are consulted only for revision bookkeeping (see README, Edge Case 1).
"""

from collections import defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import add_days, get_last_day, getdate, now_datetime

# Hours are kept to hundredths (0.01h = 36 seconds). Every arithmetic step below
# happens in Decimal; float only appears at the boundary where a value is written
# into (or read from) a Frappe Float field. This is what keeps 0.1 + 0.2 from ever
# showing up as 0.30000000000000004. See README: Edge Case 6.
HOURS_PRECISION = Decimal("0.01")


def D(value) -> Decimal:
	"""Convert a Frappe field value (float, int, str, None) to an exact Decimal.

	Decimal(str(value)) — never Decimal(value) directly on a float — is what avoids
	importing the binary floating-point error a Python float already carries. flt()
	handles the "value is a string coming off a doc / db row" case first.
	"""
	from frappe.utils import flt

	return Decimal(str(flt(value)))


def to_float(value: Decimal) -> float:
	"""Quantize to HOURS_PRECISION and hand back a float only for storage into a
	Frappe Float field. Internal comparisons and arithmetic should stay in Decimal.
	"""
	return float(D(value).quantize(HOURS_PRECISION, rounding=ROUND_HALF_UP))


def is_block_usable(expiry_date, worked_on) -> bool:
	"""Expiry is inclusive: a block is usable for work logged on its expiry date
	itself. See README: Edge Case 2 for why inclusive was chosen, and use this
	function everywhere a block/date needs to be checked — never repeat the
	comparison inline.
	"""
	return getdate(worked_on) <= getdate(expiry_date)


@contextmanager
def _engine_context():
	"""Marks code as running inside the allocation engine, so HL Allocation / HL
	Monthly Statement controllers can refuse writes from anywhere else. Restores the
	previous flag value so nested engine calls (generate_monthly_statement calls
	recompute_allocations) behave correctly.
	"""
	previous = frappe.flags.get("in_hl_recompute")
	frappe.flags.in_hl_recompute = True
	try:
		yield
	finally:
		frappe.flags.in_hl_recompute = previous


def _get_ordered_blocks(client, as_of_date=None):
	"""Blocks for this client in allocation priority order: expiry date first,
	purchase date only as a tie breaker, and block name as a final, always-unique
	tie breaker so ordering is 100% deterministic even when two blocks share both
	dates. See README: Edge Case 5.
	"""
	filters = {"client": client}
	if as_of_date:
		filters["purchase_date"] = ["<=", getdate(as_of_date)]
	return frappe.get_all(
		"HL Purchased Block",
		filters=filters,
		fields=["name", "expiry_date", "purchase_date", "hours_purchased"],
		order_by="expiry_date asc, purchase_date asc, name asc",
	)


def _get_ordered_entries(client, as_of_date=None):
	"""Every consumption entry (including corrections) for this client, replayed in
	the order the real world produced them: the date work was actually done first,
	then insertion order for same-day entries. Replaying in this fixed order — not
	insertion order alone — is what makes a backdated entry reshuffle history
	exactly the way it would have gone had it been entered on time. See README:
	Edge Case 1.
	"""
	filters = {"client": client}
	if as_of_date:
		filters["worked_on"] = ["<=", getdate(as_of_date)]
	return frappe.get_all(
		"HL Consumption Entry",
		filters=filters,
		fields=["name", "worked_on", "hours", "reverses", "creation"],
		order_by="worked_on asc, creation asc, name asc",
	)


def _replay(client, as_of_date=None):
	"""The one allocation algorithm. Replays every Purchased Block and Consumption
	Entry for `client` from scratch and returns the resulting per-block balances and
	the full list of allocation events. Pure function of its two inputs — it never
	reads HL Allocation or HL Monthly Statement, so the result is always exactly
	reproducible from source data alone (Edge Case 1's determinism guarantee).

	as_of_date=None replays the complete history: this is what
	recompute_allocations() persists as the live ledger.
	as_of_date=<date> replays only what had happened by that date (blocks not yet
	purchased and entries not yet worked are excluded): this is what
	generate_monthly_statement() uses to compute a historical opening/closing
	balance without duplicating the algorithm.
	"""
	blocks = _get_ordered_blocks(client, as_of_date=as_of_date)
	remaining = {b.name: D(b.hours_purchased) for b in blocks}
	entries = _get_ordered_entries(client, as_of_date=as_of_date)

	allocations = []
	# original consumption entry name -> ordered list of {block, expiry, reversible}
	# rows describing what that entry drew and how much of each draw is still
	# available to be undone by a future correction.
	forward_ledger = defaultdict(list)

	for ce in entries:
		hours = D(ce.hours)
		if hours == 0:
			continue
		if hours > 0:
			_allocate_forward(ce, hours, blocks, remaining, forward_ledger, allocations)
		else:
			_allocate_reversal(ce, -hours, remaining, forward_ledger, allocations)

	return {"remaining": remaining, "allocations": allocations}


def _allocate_forward(ce, hours, blocks, remaining, forward_ledger, allocations):
	"""Draw `hours` from blocks in expiry order, splitting across as many blocks as
	needed (Edge Case 3). Every non-zero draw becomes one HL Allocation row, so the
	full split is always visible later, and is recorded in forward_ledger so a
	future correction of this exact entry knows precisely which blocks to restore.

	Whatever cannot be covered once every usable block is exhausted goes into a
	single overdraft row (purchased_block=None, is_overdraft=1) instead of being
	rejected or silently dropped: the hours were genuinely worked, so they must
	show up somewhere, but never by drawing from a block that has already
	expired. See README: Edge Case 3 / overdraft.
	"""
	left = hours
	sequence = 0
	for block in blocks:
		if left <= 0:
			break
		if not is_block_usable(block.expiry_date, ce.worked_on):
			continue
		if getdate(block.purchase_date) > getdate(ce.worked_on):
			# A block cannot retroactively cover work performed before it existed.
			# Purchase date still never outranks expiry as an ordering key (Edge
			# Case 5) — it only has to have happened by the work date to be a
			# candidate at all.
			continue
		available = remaining[block.name]
		if available <= 0:
			continue
		draw = available if available < left else left
		sequence += 1
		remaining[block.name] -= draw
		left -= draw
		allocations.append(
			{
				"consumption_entry": ce.name,
				"purchased_block": block.name,
				"block_expiry_date": block.expiry_date,
				"hours_allocated": draw,
				"sequence": sequence,
				"is_overdraft": 0,
			}
		)
		forward_ledger[ce.name].append({"block": block, "reversible": draw})

	if left > 0:
		sequence += 1
		allocations.append(
			{
				"consumption_entry": ce.name,
				"purchased_block": None,
				"block_expiry_date": None,
				"hours_allocated": left,
				"sequence": sequence,
				"is_overdraft": 1,
			}
		)
		forward_ledger[ce.name].append({"block": None, "reversible": left})


def _allocate_reversal(ce, amount, remaining, forward_ledger, allocations):
	"""Undo `amount` hours from the entry `ce.reverses` corrects, restoring in
	LIFO order — the *last* thing the original draw touched is the first thing a
	partial correction gives hours back to. Concretely: overdraft (which can only
	ever be the last row of a forward split, if it exists at all) is always paid
	down before any block is touched, and among blocks, the last one the split
	reached is unwound first. This guarantees hours only ever go back to where
	the original entry actually took them from — never an arbitrary block — and
	stays well-defined even when the correction is smaller than the full
	original split. See README: Edge Case 4.
	"""
	left = amount
	sequence = 0
	for row in reversed(forward_ledger.get(ce.reverses, [])):
		if left <= 0:
			break
		if row["reversible"] <= 0:
			continue
		restore = row["reversible"] if row["reversible"] < left else left
		sequence += 1
		row["reversible"] -= restore
		left -= restore
		block = row["block"]
		allocations.append(
			{
				"consumption_entry": ce.name,
				"purchased_block": block.name if block else None,
				"block_expiry_date": block.expiry_date if block else None,
				"hours_allocated": -restore,
				"sequence": sequence,
				"is_overdraft": 1 if block is None else 0,
			}
		)
		if block is not None:
			remaining[block.name] += restore

	if left > 0:
		frappe.throw(
			_(
				"Correction {0} could not be fully resolved against {1}: {2} hours have no"
				" matching original allocation to restore. This indicates the original entry"
				" was itself corrected more than its allocations allow, or the data is"
				" inconsistent."
			).format(ce.name, ce.reverses, left)
		)


def recompute_allocations(client):
	"""Rebuild the *entire* current allocation ledger for `client` from source data.

	Always a full delete-and-replay, never an incremental patch: that is the only
	way a single backdated or corrected Consumption Entry is guaranteed to produce
	exactly the result it would have produced had it been entered in the right
	order to begin with — no matter how many other entries and blocks exist.
	See README: Edge Case 1 (why recomputation is necessary).
	"""
	with _engine_context():
		result = _replay(client, as_of_date=None)

		frappe.db.delete("HL Allocation", {"client": client})
		for row in result["allocations"]:
			frappe.get_doc(
				{
					"doctype": "HL Allocation",
					"client": client,
					"consumption_entry": row["consumption_entry"],
					"purchased_block": row["purchased_block"],
					"block_expiry_date": row["block_expiry_date"],
					"hours_allocated": to_float(row["hours_allocated"]),
					"sequence": row["sequence"],
					"is_overdraft": row["is_overdraft"],
				}
			).insert(ignore_permissions=True)

		for block_name, amount in result["remaining"].items():
			frappe.db.set_value(
				"HL Purchased Block",
				block_name,
				"hours_remaining",
				to_float(amount),
				update_modified=False,
			)

	return result


def _period_bounds(year, month):
	start = getdate(f"{int(year):04d}-{int(month):02d}-01")
	end = get_last_day(start)
	return start, end


def _replay_remaining_by_block(client, as_of_date):
	"""(block_doc, remaining) pairs from a point-in-time replay, as of `as_of_date`.
	Shared by every "as of a historical moment" figure on the statement, so the
	closing balance and the expired-unused total can never disagree with each
	other about what the ledger looked like at that moment.
	"""
	as_of_date = getdate(as_of_date)
	replay = _replay(client, as_of_date=as_of_date)
	blocks = {b.name: b for b in _get_ordered_blocks(client, as_of_date=as_of_date)}
	return [(blocks[name], amount) for name, amount in replay["remaining"].items()]


def _closing_balance(client, as_of_date):
	"""Total usable hours across blocks that exist and have not expired as of
	`as_of_date`.
	"""
	as_of_date = getdate(as_of_date)
	total = Decimal("0")
	for block, amount in _replay_remaining_by_block(client, as_of_date):
		if is_block_usable(block.expiry_date, as_of_date):
			total += amount
	return total


def _expired_unused_in_period(client, period_start, period_end):
	"""Hours forfeited this period: for every block whose expiry date falls
	inside [period_start, period_end], whatever it still had left the moment it
	expired. A block's remaining balance as of period_end already reflects
	everything it will ever have been drawn for (nothing can draw from it after
	its own expiry — Edge Case 2), so replaying once as of period_end is enough;
	no separate per-block replay is needed.
	"""
	total = Decimal("0")
	for block, amount in _replay_remaining_by_block(client, period_end):
		if period_start <= getdate(block.expiry_date) <= period_end:
			total += amount
	return total


def _purchased_in_period(client, period_start, period_end):
	"""Hours added to the ledger this period, regardless of when (or whether)
	they end up consumed."""
	rows = frappe.get_all(
		"HL Purchased Block",
		filters={"client": client, "purchase_date": ["between", [period_start, period_end]]},
		fields=["hours_purchased"],
	)
	return sum((D(r.hours_purchased) for r in rows), Decimal("0"))


def _overdraft_in_period(client, entry_names):
	"""Hours worked this period that no unexpired block could cover, net of any
	corrections that have since reduced that shortfall (Edge Case 3 / overdraft).
	"""
	rows = frappe.get_all(
		"HL Allocation",
		filters={"client": client, "consumption_entry": ["in", entry_names], "is_overdraft": 1},
		fields=["hours_allocated"],
	)
	return sum((D(r.hours_allocated) for r in rows), Decimal("0"))


def _snapshot_key(scalars, lines):
	"""A comparable, order-independent fingerprint of a statement's numbers, used
	only to decide whether regenerating actually changed anything (never used to
	compute the numbers themselves).
	"""
	line_tuples = tuple(
		sorted(
			(
				line["consumption_entry"],
				line["purchased_block"] or "",
				str(D(line["hours_allocated"]).quantize(HOURS_PRECISION)),
				line["sequence"],
			)
			for line in lines
		)
	)
	return (
		tuple(str(D(value).quantize(HOURS_PRECISION)) for value in scalars),
		line_tuples,
	)


def generate_monthly_statement(client, year, month):
	"""Produce the statement for one client/period, or hand back the existing one
	unchanged if a fresh recompute proves nothing about it actually changed.

	Always starts from a full recompute of source data (Purchased Block +
	Consumption Entry) — never trusts a previously generated HL Monthly Statement
	for the numbers. An existing statement is read only to decide whether a new
	revision is needed and to link revision history; that is bookkeeping, not a
	computational dependency. See README: Edge Case 1.
	"""
	year, month = int(year), int(month)
	with _engine_context():
		recompute_allocations(client)

		period_start, period_end = _period_bounds(year, month)
		opening = _closing_balance(client, add_days(period_start, -1))
		closing = _closing_balance(client, period_end)
		purchased_in_month = _purchased_in_period(client, period_start, period_end)
		expired_unused_in_month = _expired_unused_in_period(client, period_start, period_end)

		period_entries = frappe.get_all(
			"HL Consumption Entry",
			filters={"client": client, "worked_on": ["between", [period_start, period_end]]},
			fields=["name", "worked_on", "hours"],
		)
		total_consumed = sum((D(e.hours) for e in period_entries), Decimal("0"))
		entry_names = [e.name for e in period_entries] or [""]
		entry_meta = {e.name: e for e in period_entries}
		overdraft = _overdraft_in_period(client, entry_names)

		lines = frappe.get_all(
			"HL Allocation",
			filters={"client": client, "consumption_entry": ["in", entry_names]},
			fields=[
				"consumption_entry",
				"purchased_block",
				"block_expiry_date",
				"hours_allocated",
				"sequence",
				"is_overdraft",
			],
		)
		for row in lines:
			meta = entry_meta.get(row["consumption_entry"])
			row["worked_on"] = meta.worked_on if meta else period_start
			row["is_correction"] = 1 if meta and D(meta.hours) < 0 else 0
		lines.sort(key=lambda r: (r["worked_on"], r["consumption_entry"], r["sequence"]))

		scalars = (opening, purchased_in_month, total_consumed, expired_unused_in_month, closing, overdraft)
		snapshot = _snapshot_key(scalars, lines)

		existing = frappe.get_all(
			"HL Monthly Statement",
			filters={"client": client, "year": year, "month": month, "is_latest": 1},
			fields=["name"],
			limit=1,
		)
		existing_doc = frappe.get_doc("HL Monthly Statement", existing[0].name) if existing else None

		if existing_doc:
			existing_scalars = (
				existing_doc.opening_balance_hours,
				existing_doc.hours_purchased_in_month,
				existing_doc.total_consumed_hours,
				existing_doc.hours_expired_unused_in_month,
				existing_doc.closing_balance_hours,
				existing_doc.overdraft_hours,
			)
			existing_snapshot = _snapshot_key(
				existing_scalars,
				[
					{
						"consumption_entry": line.consumption_entry,
						"purchased_block": line.purchased_block,
						"hours_allocated": D(line.hours_allocated),
						"sequence": line.sequence,
					}
					for line in existing_doc.lines
				],
			)
			if existing_snapshot == snapshot:
				return existing_doc

		new_doc = frappe.get_doc(
			{
				"doctype": "HL Monthly Statement",
				"client": client,
				"year": year,
				"month": month,
				"revision": (existing_doc.revision + 1) if existing_doc else 1,
				"is_latest": 1,
				"generated_on": now_datetime(),
				"previous_revision": existing_doc.name if existing_doc else None,
				"opening_balance_hours": to_float(opening),
				"hours_purchased_in_month": to_float(purchased_in_month),
				"total_consumed_hours": to_float(total_consumed),
				"hours_expired_unused_in_month": to_float(expired_unused_in_month),
				"closing_balance_hours": to_float(closing),
				"overdraft_hours": to_float(overdraft),
				"notes": (
					_(
						"Supersedes revision {0}: recomputed from source data because"
						" underlying consumption changed for this period."
					).format(existing_doc.revision)
					if existing_doc
					else _("Initial statement, generated from source data.")
				),
				"lines": [
					{
						"consumption_entry": row["consumption_entry"],
						"worked_on": row["worked_on"],
						"purchased_block": row["purchased_block"],
						"block_expiry_date": row["block_expiry_date"],
						"hours_allocated": to_float(row["hours_allocated"]),
						"sequence": row["sequence"],
						"is_correction": row["is_correction"],
						"is_overdraft": row["is_overdraft"],
					}
					for row in lines
				],
			}
		)
		new_doc.insert(ignore_permissions=True)

		if existing_doc:
			frappe.db.set_value(
				"HL Monthly Statement",
				existing_doc.name,
				{"is_latest": 0, "superseded_by": new_doc.name},
				update_modified=False,
			)

		return new_doc


def regenerate_statements_from(client, from_date):
	"""Regenerate every already-published (is_latest) statement for `client` whose
	period is on or after the month `from_date` falls in.

	A backdated Consumption Entry doesn't only affect the statement for its own
	month: because recompute_allocations always replays in worked_on order,
	inserting it can change which physical block *later* entries drew from too
	(e.g. it can claim capacity from a block a later entry had been using).
	Every later statement is therefore regenerated — each call is a no-op (no new
	revision) wherever generate_monthly_statement proves the numbers didn't
	actually change. See README: Edge Case 1.
	"""
	from_date = getdate(from_date)
	periods = frappe.get_all(
		"HL Monthly Statement",
		filters={"client": client, "is_latest": 1},
		fields=["year", "month"],
	)
	for period in periods:
		if (period.year, period.month) >= (from_date.year, from_date.month):
			generate_monthly_statement(client, period.year, period.month)


def get_statement_history(client, year, month):
	"""Every revision ever generated for one client/period, oldest first — the
	audit trail that makes it unmistakable a statement changed, and why.
	See README: Edge Case 1.
	"""
	return frappe.get_all(
		"HL Monthly Statement",
		filters={"client": client, "year": int(year), "month": int(month)},
		fields=[
			"name",
			"revision",
			"is_latest",
			"generated_on",
			"previous_revision",
			"superseded_by",
			"opening_balance_hours",
			"hours_purchased_in_month",
			"total_consumed_hours",
			"hours_expired_unused_in_month",
			"closing_balance_hours",
			"overdraft_hours",
			"notes",
		],
		order_by="revision asc",
	)
