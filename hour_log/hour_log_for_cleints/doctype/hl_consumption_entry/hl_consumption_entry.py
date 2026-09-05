# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from hour_log.hour_log_for_cleints.engine import D, recompute_allocations, regenerate_statements_from


class HLConsumptionEntry(Document):
	def validate(self):
		self._validate_zero_or_positive()
		self._validate_negative_correction()

	def _validate_zero_or_positive(self):
		"""hours >= 0 must never carry a `reverses` link: only a correction (negative
		hours) is allowed to reference another entry. Edge Case 4.
		"""
		if D(self.hours) >= 0:
			if self.reverses:
				frappe.throw(
					_("Only a negative-hours entry (a correction) may set Reverses.")
				)
			self.is_correction = 0

	def _validate_negative_correction(self):
		"""A negative entry is a correction. It must name the exact original entry it
		corrects, and it can never undo more hours than that original entry (net of any
		earlier corrections) actually granted. The allocation engine later uses
		`reverses` to restore hours to the exact blocks the original entry drew from —
		never to an arbitrary block. Edge Case 4.
		"""
		if D(self.hours) >= 0:
			return

		self.is_correction = 1

		if not self.reverses:
			frappe.throw(_("A negative-hours entry must set Reverses to the entry it corrects."))

		original = frappe.db.get_value(
			"HL Consumption Entry", self.reverses, ["client", "hours"], as_dict=True
		)
		if not original:
			frappe.throw(_("Reverses points to an entry that does not exist: {0}").format(self.reverses))

		if original.client != self.client:
			frappe.throw(_("A correction must belong to the same client as the entry it reverses."))

		if D(original.hours) <= 0:
			frappe.throw(
				_("Reverses must point to a positive (original consumption) entry, not another correction.")
			)

		already_corrected = D(
			frappe.db.sql(
				"""
				select coalesce(sum(hours), 0) from `tabHL Consumption Entry`
				where reverses = %s and name != %s and hours < 0
				""",
				(self.reverses, self.name or ""),
			)[0][0]
		)
		# already_corrected is <= 0 (sum of negative numbers); net remaining that can
		# still be reversed is original.hours + already_corrected.
		remaining_correctable = D(original.hours) + already_corrected
		if -D(self.hours) > remaining_correctable:
			frappe.throw(
				_(
					"This correction of {0} hours exceeds what remains to be corrected on {1}"
					" ({2} hours available)."
				).format(-D(self.hours), self.reverses, remaining_correctable)
			)

	def after_insert(self):
		self._recompute_and_cascade()

	def on_update(self):
		if not self.is_new():
			self._recompute_and_cascade()

	def _recompute_and_cascade(self):
		recompute_allocations(self.client)
		regenerate_statements_from(self.client, self.worked_on)

	def on_trash(self):
		if not frappe.flags.get("hl_allow_delete"):
			frappe.throw(
				_(
					"HL Consumption Entry records cannot be deleted: they are append-only source"
					" data for the allocation engine. To remove hours, add a negative-hours"
					" correction entry that Reverses this one instead."
				)
			)
