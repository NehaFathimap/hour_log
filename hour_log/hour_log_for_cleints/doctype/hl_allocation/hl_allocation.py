# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class HLAllocation(Document):
	"""Pure output of the allocation engine. Nothing outside
	hour_log_for_cleints.engine.recompute_allocations() is allowed to create, edit,
	or delete these rows — that is what makes the allocation history trustworthy.
	"""

	def validate(self):
		if not frappe.flags.get("in_hl_recompute"):
			frappe.throw(
				_(
					"HL Allocation rows can only be produced by the allocation engine"
					" (recompute_allocations), never created or edited directly."
				)
			)

	def on_trash(self):
		if not frappe.flags.get("in_hl_recompute"):
			frappe.throw(
				_("HL Allocation rows can only be removed by the allocation engine, never directly.")
			)
