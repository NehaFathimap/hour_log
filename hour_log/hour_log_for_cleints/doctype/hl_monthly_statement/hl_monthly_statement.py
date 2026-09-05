# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class HLMonthlyStatement(Document):
	"""Generated snapshot only. Always produced by
	hour_log_for_cleints.engine.generate_monthly_statement() from a fresh recompute of
	Purchased Block + Consumption Entry — never edited by hand and never used as an
	input to any calculation. See README: Edge Case 1.
	"""

	def validate(self):
		if not frappe.flags.get("in_hl_recompute"):
			frappe.throw(
				_(
					"HL Monthly Statement rows can only be produced by"
					" generate_monthly_statement(), never created or edited directly."
				)
			)

	def on_trash(self):
		if not frappe.flags.get("hl_allow_delete"):
			frappe.throw(
				_(
					"HL Monthly Statement records are kept as history and are not meant to be"
					" deleted. A corrected statement is a new revision, not a replacement."
				)
			)
