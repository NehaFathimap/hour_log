# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Regenerates sample_input.json / sample_output.json from a live run of the real
engine (not hand-typed numbers), so the two files always describe an actually
reachable state.

Run with:
	bench --site <site> execute hour_log.hour_log_for_cleints.examples.generate_sample.run

The whole scenario is created and then rolled back — nothing it creates is left
in the database.
"""

import json
import os

import frappe

from hour_log.hour_log_for_cleints.engine import generate_monthly_statement, get_statement_history
from hour_log.hour_log_for_cleints.hl_test_helpers import make_block, make_client, make_entry


def _block_dict(b):
	return {
		"name": b.name,
		"purchase_date": str(b.purchase_date),
		"expiry_date": str(b.expiry_date),
		"hours_purchased": b.hours_purchased,
		"reference": b.reference,
	}


def _entry_dict(e):
	d = {
		"name": e.name,
		"worked_on": str(e.worked_on),
		"hours": e.hours,
		"description": e.description,
	}
	if e.reverses:
		d["reverses"] = e.reverses
	return d


def _stmt_dict(s):
	return {
		"name": s.name,
		"client": s.client,
		"year": s.year,
		"month": s.month,
		"revision": s.revision,
		"is_latest": s.is_latest,
		"previous_revision": s.previous_revision,
		"superseded_by": s.superseded_by,
		"opening_balance_hours": s.opening_balance_hours,
		"hours_purchased_in_month": s.hours_purchased_in_month,
		"total_consumed_hours": s.total_consumed_hours,
		"hours_expired_unused_in_month": s.hours_expired_unused_in_month,
		"closing_balance_hours": s.closing_balance_hours,
		"overdraft_hours": s.overdraft_hours,
		"notes": s.notes,
		"lines": [
			{
				"worked_on": str(line.worked_on),
				"consumption_entry": line.consumption_entry,
				"purchased_block": line.purchased_block,
				"block_expiry_date": str(line.block_expiry_date) if line.block_expiry_date else None,
				"hours_allocated": line.hours_allocated,
				"sequence": line.sequence,
				"is_correction": line.is_correction,
				"is_overdraft": line.is_overdraft,
			}
			for line in s.lines
		],
	}


def run():
	client = make_client("Sample Client Acme Corp")

	pb_a = make_block(client, "2026-01-01", "2026-01-31", 4, reference="PB-A: Jan block, expires end of Jan")
	pb_b = make_block(client, "2026-01-01", "2026-03-31", 6, reference="PB-B: Q1 block, plenty of runway")

	ce1 = make_entry(client, "2026-01-15", 3, description="Week 1 consulting")
	ce2 = make_entry(client, "2026-01-31", 2, description="Month-end work, logged on the expiry date itself")

	generate_monthly_statement(client, 2026, 1)

	pb_c = make_block(
		client,
		"2026-02-10",
		"2026-02-20",
		5,
		reference="PB-C: bought Feb 10th (after PB-A/PB-B) but expires Feb 20th, earlier than PB-B",
	)

	ce3 = make_entry(client, "2026-02-15", 7, description="Big February engagement")
	ce4 = make_entry(client, "2026-02-16", 0, description="Kickoff call, no billable hours")
	generate_monthly_statement(client, 2026, 2)

	ce5 = make_entry(client, "2026-02-18", -3, reverses=ce3.name, description="Correction: over-logged by 3h")
	generate_monthly_statement(client, 2026, 2)

	# Retroactive entry: worked_on is back in January, added after the January
	# statement above already exists.
	ce6 = make_entry(client, "2026-01-20", 1, description="Retroactive: forgot to log this at the time")

	generate_monthly_statement(client, 2026, 1)
	generate_monthly_statement(client, 2026, 2)

	# March: a small block that cannot cover the work logged against it. The
	# shortfall must show up as overdraft, not an error and not a draw from an
	# already-expired block.
	pb_d = make_block(client, "2026-03-01", "2026-03-10", 2, reference="PB-D: small block, expires early March")
	ce7 = make_entry(
		client, "2026-03-05", 10, description="More work than every remaining unexpired block can cover"
	)
	generate_monthly_statement(client, 2026, 3)

	sample_input = {
		"client": client,
		"purchased_blocks": [_block_dict(pb_a), _block_dict(pb_b), _block_dict(pb_c), _block_dict(pb_d)],
		"consumption_entries_in_insertion_order": [
			_entry_dict(ce1),
			_entry_dict(ce2),
			_entry_dict(ce3),
			_entry_dict(ce4),
			_entry_dict(ce5),
			{
				**_entry_dict(ce6),
				"note": "inserted LAST, but worked_on=2026-01-20 lands chronologically between ce1 and ce2",
			},
			_entry_dict(ce7),
		],
	}

	sample_output = {
		"january_statement_history": [
			_stmt_dict(frappe.get_doc("HL Monthly Statement", h.name))
			for h in get_statement_history(client, 2026, 1)
		],
		"february_statement_history": [
			_stmt_dict(frappe.get_doc("HL Monthly Statement", h.name))
			for h in get_statement_history(client, 2026, 2)
		],
		"march_statement_history": [
			_stmt_dict(frappe.get_doc("HL Monthly Statement", h.name))
			for h in get_statement_history(client, 2026, 3)
		],
		"final_purchased_block_balances": [
			{"reference": b.reference, "hours_purchased": b.hours_purchased, "hours_remaining": b.hours_remaining}
			for b in (
				frappe.get_doc("HL Purchased Block", pb_a.name),
				frappe.get_doc("HL Purchased Block", pb_b.name),
				frappe.get_doc("HL Purchased Block", pb_c.name),
				frappe.get_doc("HL Purchased Block", pb_d.name),
			)
		],
	}

	base = os.path.dirname(__file__)
	with open(os.path.join(base, "sample_input.json"), "w") as f:
		json.dump(sample_input, f, indent=2)
	with open(os.path.join(base, "sample_output.json"), "w") as f:
		json.dump(sample_output, f, indent=2)

	print("Wrote sample_input.json / sample_output.json")
	print("January revisions:", [s.revision for s in get_statement_history(client, 2026, 1)])
	print("February revisions:", [s.revision for s in get_statement_history(client, 2026, 2)])
	print("March revisions:", [s.revision for s in get_statement_history(client, 2026, 3)])

	frappe.db.rollback()
