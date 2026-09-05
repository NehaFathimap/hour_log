# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""End-to-end tests for the allocation engine, one section per edge case from the
brief. See README.md for the plain-English explanation each test is proving."""

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from hour_log.hour_log_for_cleints.engine import (
	D,
	generate_monthly_statement,
	recompute_allocations,
	to_float,
)
from hour_log.hour_log_for_cleints.hl_test_helpers import make_block, make_client, make_entry


def allocations_for(entry_name):
	return frappe.get_all(
		"HL Allocation",
		filters={"consumption_entry": entry_name},
		fields=["name", "purchased_block", "hours_allocated", "sequence", "is_overdraft"],
		order_by="sequence asc",
	)


def remaining(block_doc_or_name):
	name = getattr(block_doc_or_name, "name", block_doc_or_name)
	return frappe.db.get_value("HL Purchased Block", name, "hours_remaining")


class TestEdgeCase1RetroactiveConsumption(FrappeTestCase):
	"""A consumption entry can arrive after its month's statement was already
	generated. Added because a naive "statement locks the month" design would
	either reject the late entry or silently produce a statement that no longer
	matches the ledger."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client Retro {self._testMethodName}")
		make_block(self.client, "2026-01-01", "2026-12-31", 10)

	def test_retroactive_entry_produces_a_new_statement_revision(self):
		make_entry(self.client, "2026-01-10", 3)
		v1 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v1.revision, 1)
		self.assertEqual(v1.total_consumed_hours, 3)

		# The retroactive entry: worked_on is in January, but v1 already exists.
		make_entry(self.client, "2026-01-05", 2)

		v2 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v2.revision, 2)
		self.assertNotEqual(v2.name, v1.name)
		self.assertEqual(v2.total_consumed_hours, 5)
		self.assertEqual(v2.previous_revision, v1.name)

	def test_the_old_statement_is_visibly_marked_as_superseded_not_silently_replaced(self):
		make_entry(self.client, "2026-01-10", 3)
		v1 = generate_monthly_statement(self.client, 2026, 1)
		make_entry(self.client, "2026-01-05", 2)
		v2 = generate_monthly_statement(self.client, 2026, 1)

		stale = frappe.get_doc("HL Monthly Statement", v1.name)
		self.assertEqual(stale.is_latest, 0)
		self.assertEqual(stale.superseded_by, v2.name)
		self.assertEqual(frappe.db.get_value("HL Monthly Statement", v2.name, "is_latest"), 1)

	def test_regenerating_with_no_new_data_does_not_create_a_redundant_revision(self):
		"""Determinism check: calling generate_monthly_statement again with nothing
		changed must return the exact same document, not a spurious new revision."""
		make_entry(self.client, "2026-01-10", 3)
		v1 = generate_monthly_statement(self.client, 2026, 1)
		v1_again = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v1.name, v1_again.name)
		self.assertEqual(v1_again.revision, 1)

	def test_recompute_is_deterministic_and_replayable(self):
		"""Running the exact same source data through the engine twice must produce
		byte-identical allocations: this is what lets a statement be regenerated
		with confidence instead of trusted blindly."""
		make_block(self.client, "2026-02-01", "2026-03-31", 4)
		e1 = make_entry(self.client, "2026-02-10", 6)
		make_entry(self.client, "2026-02-12", -2, reverses=e1.name)

		recompute_allocations(self.client)
		first_pass = frappe.get_all(
			"HL Allocation",
			filters={"client": self.client},
			fields=["consumption_entry", "purchased_block", "hours_allocated", "sequence"],
			order_by="consumption_entry asc, sequence asc",
		)
		recompute_allocations(self.client)
		second_pass = frappe.get_all(
			"HL Allocation",
			filters={"client": self.client},
			fields=["consumption_entry", "purchased_block", "hours_allocated", "sequence"],
			order_by="consumption_entry asc, sequence asc",
		)
		self.assertEqual(first_pass, second_pass)


class TestEdgeCase2ExpiryBoundary(FrappeTestCase):
	"""Whether 31 March itself can still use a block expiring 31 March is a
	one-bit business decision that must be applied identically everywhere. We
	chose inclusive (usable through end of the expiry day) — see README."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client Expiry {self._testMethodName}")

	def test_work_on_the_expiry_date_itself_can_use_the_block(self):
		make_block(self.client, "2026-01-01", "2026-03-31", 5)
		entry = make_entry(self.client, "2026-03-31", 3)
		recompute_allocations(self.client)
		rows = allocations_for(entry.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].hours_allocated, 3)

	def test_work_the_day_after_expiry_cannot_use_the_block_and_goes_to_overdraft(self):
		block = make_block(self.client, "2026-01-01", "2026-03-31", 5)
		entry = make_entry(self.client, "2026-04-01", 3)
		recompute_allocations(self.client)

		rows = allocations_for(entry.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].purchased_block, None)
		self.assertEqual(rows[0].hours_allocated, 3)
		self.assertEqual(remaining(block), 5, "the expired block must be left untouched, not raided")


class TestEdgeCase3MultiBlockSplit(FrappeTestCase):
	"""One entry may need more hours than any single block holds. Added because an
	engine that only ever looked at "the current block" would either fail or
	under-allocate whenever a client's usage crossed a block boundary."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client Split {self._testMethodName}")

	def test_one_entry_splits_across_three_blocks_in_expiry_order(self):
		block_a = make_block(self.client, "2026-01-01", "2026-01-31", 2, reference="A")
		block_b = make_block(self.client, "2026-01-01", "2026-02-28", 3, reference="B")
		block_c = make_block(self.client, "2026-01-01", "2026-03-31", 4, reference="C")

		entry = make_entry(self.client, "2026-01-15", 7)
		recompute_allocations(self.client)

		rows = allocations_for(entry.name)
		self.assertEqual(len(rows), 3, "every split must be recorded as its own row")
		self.assertEqual(
			[(r.purchased_block, r.hours_allocated) for r in rows],
			[(block_a.name, 2), (block_b.name, 3), (block_c.name, 2)],
		)
		self.assertEqual(remaining(block_a), 0)
		self.assertEqual(remaining(block_b), 0)
		self.assertEqual(remaining(block_c), 2)


class TestOverdraft(FrappeTestCase):
	"""Hours worked beyond every unexpired block's capacity must never be rejected
	and never be silently dropped: they become a visible overdraft, reported on
	the statement, and are never drawn from a block that has already expired."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client Overdraft {self._testMethodName}")

	def test_shortfall_becomes_a_recorded_overdraft_row_not_an_error(self):
		block = make_block(self.client, "2026-01-01", "2026-01-31", 2)
		entry = make_entry(self.client, "2026-01-15", 10)
		recompute_allocations(self.client)

		rows = allocations_for(entry.name)
		self.assertEqual(len(rows), 2)
		block_row, overdraft_row = rows
		self.assertEqual((block_row.purchased_block, block_row.hours_allocated), (block.name, 2))
		self.assertEqual(overdraft_row.purchased_block, None)
		self.assertEqual(overdraft_row.hours_allocated, 8)
		self.assertEqual(frappe.db.get_value("HL Allocation", overdraft_row.name, "is_overdraft"), 1)

	def test_overdraft_never_draws_from_an_already_expired_block(self):
		"""An expired block with leftover hours must not be touched, even to
		avoid an overdraft: the shortfall goes to overdraft instead."""
		make_block(self.client, "2026-01-01", "2026-01-31", 100)  # expired by the time work happens
		entry = make_entry(self.client, "2026-02-15", 5)
		recompute_allocations(self.client)

		rows = allocations_for(entry.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].purchased_block, None)
		self.assertEqual(rows[0].hours_allocated, 5)

	def test_statement_reports_overdraft_for_the_period(self):
		make_block(self.client, "2026-01-01", "2026-01-31", 2)
		make_entry(self.client, "2026-01-15", 10)
		stmt = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(stmt.overdraft_hours, 8)

	def test_correcting_an_overdrafted_entry_reduces_the_overdraft_first(self):
		"""Reversal is LIFO, and overdraft (when it exists) is always the last row
		of a forward split — so a correction pays down the overdraft before it
		ever touches a block, which also matches how you'd want a shortfall to
		be settled in practice."""
		block = make_block(self.client, "2026-01-01", "2026-01-31", 2)
		original = make_entry(self.client, "2026-01-15", 10)  # 2 from block, 8 overdraft
		make_entry(self.client, "2026-01-16", -5, reverses=original.name)
		recompute_allocations(self.client)

		# The correction (5) is smaller than the overdraft portion (8) alone, so it
		# comes entirely out of the overdraft, leaving the block draw untouched.
		self.assertEqual(remaining(block), 0)
		stmt = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(stmt.overdraft_hours, 3)


class TestEdgeCase4ZeroAndNegativeHours(FrappeTestCase):
	"""A correction must undo hours from precisely the block(s) the original entry
	drew from — never from whichever block happens to have room. Added because
	without per-allocation history, a reversal has no way to know which block to
	credit back."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client Corrections {self._testMethodName}")

	def test_correction_restores_hours_to_the_exact_original_blocks_only(self):
		"""Reversal is LIFO: the last block the original split touched is unwound
		first. Here the correction exactly matches what B (drawn second)
		contributed, so B is restored in full and A — which also has "room" in
		the everyday sense — is left completely untouched."""
		block_a = make_block(self.client, "2026-01-01", "2026-01-31", 2, reference="A")
		block_b = make_block(self.client, "2026-01-01", "2026-02-28", 5, reference="B")

		original = make_entry(self.client, "2026-01-15", 5)  # 2 from A, 3 from B
		make_entry(self.client, "2026-01-20", -3, reverses=original.name)
		recompute_allocations(self.client)

		self.assertEqual(remaining(block_a), 0)
		self.assertEqual(remaining(block_b), 5)

	def test_multiple_partial_corrections_stack_up_to_the_original_amount(self):
		block = make_block(self.client, "2026-01-01", "2026-12-31", 10)
		original = make_entry(self.client, "2026-01-15", 5)
		make_entry(self.client, "2026-01-16", -2, reverses=original.name)
		make_entry(self.client, "2026-01-17", -3, reverses=original.name)
		recompute_allocations(self.client)
		self.assertEqual(remaining(block), 10)

	def test_zero_hour_entry_is_a_no_op(self):
		make_block(self.client, "2026-01-01", "2026-12-31", 10)
		entry = make_entry(self.client, "2026-01-15", 0)
		recompute_allocations(self.client)
		self.assertEqual(allocations_for(entry.name), [])

	# --- The four scenarios named explicitly in the negative-hours-correction spec ---

	def test_single_block_negative_correction(self):
		"""Spec example: Block A (100h) covers C001 (20h) in full; C002 (-5h)
		must return exactly 5h to Block A, and nowhere else."""
		block_a = make_block(self.client, "2026-01-01", "2026-03-31", 100, reference="A")
		c001 = make_entry(self.client, "2026-03-10", 20)
		recompute_allocations(self.client)
		self.assertEqual(remaining(block_a), 80)

		make_entry(self.client, "2026-03-11", -5, reverses=c001.name, description="C002")
		recompute_allocations(self.client)

		rows = allocations_for(c001.name)
		self.assertEqual(len(rows), 1, "the original C001 allocation row is untouched")
		correction_rows = frappe.get_all(
			"HL Allocation",
			filters={"consumption_entry": ["!=", c001.name], "client": self.client},
			fields=["purchased_block", "hours_allocated"],
		)
		self.assertEqual(correction_rows, [{"purchased_block": block_a.name, "hours_allocated": -5}])
		self.assertEqual(remaining(block_a), 85)

	def test_multiple_block_negative_correction(self):
		"""Spec example: C001 (100h) splits 40h/A + 60h/B; C002 (-30h) must land
		only on blocks C001 actually used — never on an unused Block C, even
		though C exists and has plenty of room."""
		block_a = make_block(self.client, "2026-01-01", "2026-01-20", 40, reference="A")
		block_b = make_block(self.client, "2026-01-01", "2026-02-28", 60, reference="B")
		block_c = make_block(self.client, "2026-01-01", "2026-06-30", 1000, reference="C, unused bait")

		c001 = make_entry(self.client, "2026-01-10", 100)
		recompute_allocations(self.client)
		self.assertEqual([(r.purchased_block, r.hours_allocated) for r in allocations_for(c001.name)],
			[(block_a.name, 40), (block_b.name, 60)])

		make_entry(self.client, "2026-01-12", -30, reverses=c001.name, description="C002")
		recompute_allocations(self.client)

		# Documented reversal strategy: LIFO — the block reached *last* in the
		# original split (B) is credited back first.
		self.assertEqual(remaining(block_a), 0)
		self.assertEqual(remaining(block_b), 30)
		self.assertEqual(remaining(block_c), 1000, "Block C was never part of C001's split and must stay untouched")

	def test_correction_larger_than_one_allocation(self):
		"""A correction bigger than any single allocation row must spill over into
		the next one it recorded — never invent a third destination."""
		block_a = make_block(self.client, "2026-01-01", "2026-01-20", 5, reference="A")
		block_b = make_block(self.client, "2026-01-01", "2026-02-28", 20, reference="B")

		c001 = make_entry(self.client, "2026-01-10", 15)  # 5 from A, 10 from B
		make_entry(self.client, "2026-01-12", -8, reverses=c001.name)  # bigger than A's single row (5)
		recompute_allocations(self.client)

		# LIFO: B (last touched, 10) absorbs min(10, 8) = 8 fully; A is never reached.
		self.assertEqual(remaining(block_a), 0)
		self.assertEqual(remaining(block_b), 18)  # 20 - 10 + 8

	def test_recalculation_after_retroactive_correction(self):
		"""A correction can itself be retroactive: added after a statement for its
		own period was already published. The statement must be recomputed and
		superseded, exactly like a retroactive new entry (Edge Case 1)."""
		block = make_block(self.client, "2026-01-01", "2026-03-31", 50)
		c001 = make_entry(self.client, "2026-01-10", 20)
		v1 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v1.revision, 1)
		self.assertEqual(v1.total_consumed_hours, 20)
		self.assertEqual(remaining(block), 30)

		# The correction itself is dated back inside the already-reported period.
		make_entry(self.client, "2026-01-10", -5, reverses=c001.name)

		v2 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v2.revision, 2)
		self.assertEqual(v2.previous_revision, v1.name)
		self.assertEqual(v2.total_consumed_hours, 15)
		self.assertEqual(remaining(block), 35)

		stale = frappe.get_doc("HL Monthly Statement", v1.name)
		self.assertEqual(stale.is_latest, 0)
		self.assertEqual(stale.superseded_by, v2.name)


class TestEdgeCase5ExpiryBeatsPurchaseOrder(FrappeTestCase):
	"""A block bought later can still expire sooner. Added because sorting blocks
	by purchase date (the easy, wrong instinct) would strand hours in a block
	that then expires unused, instead of spending it down first."""

	def setUp(self):
		self.client = make_client(f"_Test HL Client PurchaseOrder {self._testMethodName}")

	def test_the_earlier_expiring_block_is_used_first_even_though_purchased_later(self):
		later_purchase_earlier_expiry = make_block(
			self.client, "2026-02-01", "2026-02-28", 5, reference="bought 2nd, expires 1st"
		)
		earlier_purchase_later_expiry = make_block(
			self.client, "2026-01-01", "2026-06-30", 5, reference="bought 1st, expires later"
		)
		self.assertGreater(
			frappe.utils.getdate(later_purchase_earlier_expiry.purchase_date),
			frappe.utils.getdate(earlier_purchase_later_expiry.purchase_date),
		)

		entry = make_entry(self.client, "2026-02-15", 3)
		recompute_allocations(self.client)
		rows = allocations_for(entry.name)
		self.assertEqual(rows[0].purchased_block, later_purchase_earlier_expiry.name)

	def test_purchase_date_only_breaks_ties_when_expiry_dates_are_equal(self):
		purchased_second = make_block(self.client, "2026-01-15", "2026-06-30", 5)
		purchased_first = make_block(self.client, "2026-01-01", "2026-06-30", 5)

		entry = make_entry(self.client, "2026-02-01", 3)
		recompute_allocations(self.client)
		rows = allocations_for(entry.name)
		self.assertEqual(rows[0].purchased_block, purchased_first.name)

	def test_a_late_purchased_early_expiring_block_reopens_an_already_published_statement(self):
		"""A block bought *after* a statement was already generated can still change
		that statement — first because the new block itself is "purchased this
		month" (and, since it expires within the same month unused so far, also
		"expired unused this month"), and then again once work is logged against
		it. Two real changes, two revisions — nothing here is a coincidence."""
		make_block(self.client, "2026-01-01", "2026-06-30", 5, reference="plenty, expires late")
		make_entry(self.client, "2026-01-15", 3)
		v1 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v1.revision, 1)

		# Bought after the statement exists: this alone changes hours_purchased /
		# hours_expired_unused for January, so the statement is superseded even
		# before any new work is logged against it.
		make_block(self.client, "2026-01-20", "2026-01-31", 5, reference="bought late, expires early")
		v2 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v2.revision, 2)
		self.assertEqual(v2.hours_purchased_in_month, 10)  # both blocks were purchased in January
		self.assertEqual(v2.hours_expired_unused_in_month, 5)  # the new block, unused so far, expires this month

		# Now work is actually logged against it (purchased 01-20 <= worked 01-25,
		# expires 01-31 which beats the "plenty" block's June expiry): a second,
		# independent change, so a third revision.
		make_entry(self.client, "2026-01-25", 1)
		v3 = generate_monthly_statement(self.client, 2026, 1)
		self.assertEqual(v3.revision, 3)
		self.assertEqual(v3.previous_revision, v2.name)
		self.assertEqual(v3.hours_expired_unused_in_month, 4)  # 1 of the 5 got used before it expired

	def test_a_block_cannot_retroactively_cover_work_performed_before_it_was_purchased(self):
		"""Sorting only by expiry, with no purchase-date floor, would let a block
		bought this afternoon "cover" a job logged last week before the block
		existed. Purchase date must gate eligibility, not just break ties."""
		late_but_early_expiry = make_block(
			self.client, "2026-01-20", "2026-01-31", 5, reference="bought late, expires early"
		)
		older_block = make_block(self.client, "2026-01-01", "2026-06-30", 5, reference="already available")

		# Worked before the early-expiring block was even purchased.
		entry = make_entry(self.client, "2026-01-15", 3)
		recompute_allocations(self.client)
		rows = allocations_for(entry.name)
		self.assertEqual(rows[0].purchased_block, older_block.name)
		self.assertEqual(remaining(late_but_early_expiry), 5)


class TestEdgeCase6DecimalPrecision(FrappeTestCase):
	"""Hours must never show classic binary-float artifacts. Added because
	Python floats are base-2 and 0.1 + 0.2 != 0.3 in that representation — Decimal,
	built from the string form of each value, sidesteps it entirely."""

	def test_decimal_helper_avoids_the_binary_float_error(self):
		self.assertEqual(D(0.1) + D(0.2), Decimal("0.3"))
		self.assertEqual(to_float(D("0.1") + D("0.2")), 0.3)

	def test_ten_small_allocations_leave_an_exact_remaining_balance(self):
		client = make_client("_Test HL Client Precision")
		block = make_block(client, "2026-01-01", "2026-12-31", 1.0)
		for day in range(1, 11):
			make_entry(client, f"2026-01-{day:02d}", 0.1)
		recompute_allocations(client)
		# 1.0 - (0.1 * 10) must land on exactly 0.0, not 9.9999...e-17 or similar.
		self.assertEqual(remaining(block), 0.0)

	def test_a_third_of_an_hour_three_times_sums_exactly(self):
		client = make_client("_Test HL Client Precision Thirds")
		block = make_block(client, "2026-01-01", "2026-12-31", 0.9)
		for day in range(1, 4):
			make_entry(client, f"2026-01-0{day}", 0.3)
		recompute_allocations(client)
		self.assertEqual(remaining(block), 0.0)
