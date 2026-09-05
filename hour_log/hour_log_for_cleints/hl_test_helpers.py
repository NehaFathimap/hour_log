# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Small, deliberately un-abstracted builders shared by the hour_log_for_cleints
test suite. Named without a `test_` prefix so Frappe's blanket test-file discovery
(which walks every test_*.py in the app) does not try to load it as a test module.
"""

import frappe


def make_client(customer_name):
	if frappe.db.exists("Customer", customer_name):
		return customer_name
	frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_type": "Company",
		}
	).insert(ignore_permissions=True)
	return customer_name


def make_block(client, purchase_date, expiry_date, hours_purchased, reference=None):
	doc = frappe.get_doc(
		{
			"doctype": "HL Purchased Block",
			"client": client,
			"purchase_date": purchase_date,
			"expiry_date": expiry_date,
			"hours_purchased": hours_purchased,
			"reference": reference,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def make_entry(client, worked_on, hours, reverses=None, description=None):
	doc = frappe.get_doc(
		{
			"doctype": "HL Consumption Entry",
			"client": client,
			"worked_on": worked_on,
			"hours": hours,
			"reverses": reverses,
			"description": description,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc
