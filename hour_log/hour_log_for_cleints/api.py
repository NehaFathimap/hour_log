# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from hour_log.hour_log_for_cleints.engine import (
	generate_monthly_statement,
	get_statement_history,
)


@frappe.whitelist()
def generate_statement(client: str, year, month):
	"""Public endpoint: (re)generate a client's statement for one period. Always
	safe to call repeatedly — it is a no-op revision-wise unless recomputed source
	data actually changed the numbers. See engine.generate_monthly_statement.
	"""
	if not frappe.has_permission("HL Monthly Statement", "read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	doc = generate_monthly_statement(client, year, month)
	return doc.as_dict()


@frappe.whitelist()
def statement_history(client: str, year, month):
	"""Public endpoint: every revision generated for one client/period, so a caller
	can show "this statement changed" instead of silently swapping numbers.
	"""
	if not frappe.has_permission("HL Monthly Statement", "read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	return get_statement_history(client, year, month)
