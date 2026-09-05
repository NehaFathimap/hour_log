frappe.listview_settings["HL Monthly Statement"] = {
	onload(listview) {
		// This doctype refuses direct creation (see hl_monthly_statement.py) - it can
		// only be produced by the allocation engine. Replace the normal "+ New"
		// button, which would otherwise open a form that just throws on save, with a
		// prompt for the one thing the engine actually needs: which client and
		// period to generate.
		listview.page.set_primary_action(__("New"), () => {
			const today = frappe.datetime.get_today();
			const current_year = frappe.datetime.str_to_obj(today).getFullYear();
			const current_month = frappe.datetime.str_to_obj(today).getMonth() + 1;

			frappe.prompt(
				[
					{
						fieldname: "client",
						label: __("Client"),
						fieldtype: "Link",
						options: "Customer",
						reqd: 1,
					},
					{
						fieldname: "year",
						label: __("Year"),
						fieldtype: "Int",
						default: current_year,
						reqd: 1,
					},
					{
						fieldname: "month",
						label: __("Month"),
						fieldtype: "Select",
						options: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].join("\n"),
						default: String(current_month),
						reqd: 1,
					},
				],
				(values) => {
					frappe.call({
						method: "hour_log.hour_log_for_cleints.api.generate_statement",
						args: { client: values.client, year: values.year, month: values.month },
						freeze: true,
						freeze_message: __("Generating statement..."),
						callback(r) {
							if (!r.message) return;
							frappe.set_route("Form", "HL Monthly Statement", r.message.name);
						},
					});
				},
				__("New Monthly Statement"),
				__("Generate")
			);
		});

		listview.page.add_inner_button(__("Generate All Statements"), () => {
			const today = frappe.datetime.get_today();
			const current_year = frappe.datetime.str_to_obj(today).getFullYear();
			const current_month = frappe.datetime.str_to_obj(today).getMonth() + 1;

			frappe.prompt(
				[
					{
						fieldname: "year",
						label: __("Year"),
						fieldtype: "Int",
						default: current_year,
						reqd: 1,
					},
					{
						fieldname: "month",
						label: __("Month"),
						fieldtype: "Select",
						options: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].join("\n"),
						default: String(current_month),
						reqd: 1,
					},
				],
				(values) => {
					frappe.call({
						method: "hour_log.hour_log_for_cleints.api.generate_all_statements",
						args: { year: values.year, month: values.month },
						freeze: true,
						freeze_message: __("Generating statements for every client..."),
						callback(r) {
							if (!r.message) return;
							const { generated, failed } = r.message;
							frappe.msgprint({
								title: __("Statements Generated"),
								indicator: failed.length ? "orange" : "green",
								message: failed.length
									? __("Generated for {0} client(s). Failed for: {1} (see Error Log for details).", [
											generated.length,
											failed.join(", "),
									  ])
									: __("Generated for all {0} client(s) with hour data.", [generated.length]),
							});
							listview.refresh();
						},
					});
				},
				__("Generate All Statements"),
				__("Generate")
			);
		});
	},
};
