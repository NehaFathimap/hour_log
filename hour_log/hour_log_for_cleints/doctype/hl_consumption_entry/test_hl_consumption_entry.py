# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from hour_log.hour_log_for_cleints.hl_test_helpers import make_block, make_client, make_entry


class TestHLConsumptionEntry(FrappeTestCase):
	def setUp(self):
		self.client = make_client(f"_Test HL Client CE {self._testMethodName}")
		make_block(self.client, "2026-01-01", "2026-12-31", 10)

	def test_zero_hours_entry_is_allowed(self):
		"""A zero-hour entry (e.g. a logged-but-billable-zero activity) is a valid
		no-op: it must not raise, and must not attempt to draw from any block."""
		entry = make_entry(self.client, "2026-02-01", 0)
		self.assertEqual(frappe.db.count("HL Allocation", {"consumption_entry": entry.name}), 0)

	def test_positive_entry_cannot_set_reverses(self):
		original = make_entry(self.client, "2026-02-01", 2)
		with self.assertRaises(frappe.ValidationError):
			make_entry(self.client, "2026-02-02", 3, reverses=original.name)

	def test_negative_entry_requires_reverses(self):
		with self.assertRaises(frappe.ValidationError):
			make_entry(self.client, "2026-02-01", -1)

	def test_negative_entry_cannot_reverse_a_correction(self):
		original = make_entry(self.client, "2026-02-01", 2)
		correction = make_entry(self.client, "2026-02-02", -1, reverses=original.name)
		with self.assertRaises(frappe.ValidationError):
			make_entry(self.client, "2026-02-03", -0.5, reverses=correction.name)

	def test_correction_cannot_exceed_original_hours(self):
		original = make_entry(self.client, "2026-02-01", 2)
		with self.assertRaises(frappe.ValidationError):
			make_entry(self.client, "2026-02-02", -3, reverses=original.name)

	def test_two_partial_corrections_cannot_together_exceed_original(self):
		original = make_entry(self.client, "2026-02-01", 2)
		make_entry(self.client, "2026-02-02", -1, reverses=original.name)
		with self.assertRaises(frappe.ValidationError):
			make_entry(self.client, "2026-02-03", -1.5, reverses=original.name)

	def test_deleting_a_consumption_entry_is_blocked(self):
		entry = make_entry(self.client, "2026-02-01", 2)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("HL Consumption Entry", entry.name)
