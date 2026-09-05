# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from hour_log.hour_log_for_cleints.engine import recompute_allocations
from hour_log.hour_log_for_cleints.hl_test_helpers import make_block, make_client, make_entry


class TestHLPurchasedBlock(FrappeTestCase):
	def setUp(self):
		self.client = make_client(f"_Test HL Client PB {self._testMethodName}")

	def test_expiry_before_purchase_is_rejected(self):
		"""A block that expires before it was purchased is nonsensical input, not an
		edge case the engine should have to reason about."""
		with self.assertRaises(frappe.ValidationError):
			make_block(self.client, "2026-06-01", "2026-01-01", 10)

	def test_zero_or_negative_hours_purchased_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_block(self.client, "2026-01-01", "2026-06-30", 0)

	def test_cannot_edit_a_block_once_the_engine_has_used_it(self):
		"""Purchased Block is append-only source data once allocations exist against
		it: recompute_allocations() replays every block exactly as purchased, so an
		edit after the fact would rewrite history invisibly."""
		block = make_block(self.client, "2026-01-01", "2026-06-30", 10)
		make_entry(self.client, "2026-02-01", 2)
		recompute_allocations(self.client)

		doc = frappe.get_doc("HL Purchased Block", block.name)
		doc.hours_purchased = 999
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_deleting_a_purchased_block_is_blocked(self):
		block = make_block(self.client, "2026-01-01", "2026-06-30", 10)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("HL Purchased Block", block.name)
