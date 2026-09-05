# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from hour_log.hour_log_for_cleints.engine import recompute_allocations, regenerate_statements_from


class HLPurchasedBlock(Document):
	def validate(self):
		self._validate_dates()
		self._validate_hours()
		self._prevent_edits_after_use()

	def _validate_dates(self):
		if getdate(self.expiry_date) < getdate(self.purchase_date):
			frappe.throw(_("Expiry Date cannot be before Purchase Date."))

	def _validate_hours(self):
		if self.hours_purchased is None or float(self.hours_purchased) <= 0:
			frappe.throw(_("Hours Purchased must be greater than zero."))

	def _prevent_edits_after_use(self):
		"""Purchased Block is append-only source data once the allocation engine has
		used it: recompute_allocations() replays every block exactly as purchased, so
		silently editing purchase_date/expiry_date/hours_purchased after allocations
		exist would rewrite history without leaving a trace. Corrections belong on the
		Consumption Entry side (negative entries), never here.
		"""
		if self.is_new():
			return
		if not frappe.db.exists("HL Allocation", {"purchased_block": self.name}):
			return
		before = frappe.db.get_value(
			"HL Purchased Block",
			self.name,
			["purchase_date", "expiry_date", "hours_purchased"],
			as_dict=True,
		)
		for field in ("purchase_date", "expiry_date", "hours_purchased"):
			if str(self.get(field)) != str(before.get(field)):
				frappe.throw(
					_(
						"{0} cannot be changed on {1}: it has already been used by the allocation"
						" engine. Purchased Blocks are append-only source data."
					).format(frappe.bold(self.meta.get_label(field)), self.name)
				)

	def after_insert(self):
		# A block purchased after the fact can still expire earlier than blocks already
		# in use (Edge Case 5), which can change which block consumption dated on or
		# after this block's own purchase_date should have drawn from (a block can
		# never cover work performed before it existed). Recomputing here keeps the
		# ledger correct immediately, and regenerating covers any already-published
		# statement the reshuffle affects.
		recompute_allocations(self.client)
		regenerate_statements_from(self.client, self.purchase_date)

	def on_trash(self):
		if not frappe.flags.get("hl_allow_delete"):
			frappe.throw(
				_(
					"HL Purchased Block records cannot be deleted: they are append-only source"
					" data for the allocation engine. If this block was created in error, this"
					" is a data-integrity decision for a developer, not a routine UI action."
				)
			)
