# Screen recording script (target: under 8 minutes)

Record your terminal (and a text editor / `less` for the JSON) doing exactly
this, narrating the bracketed lines as you go. Every command is copy-pasteable
against the `hour_logs` dev site.

## 0:00–0:30 — What this is

> "This is a prepaid hour allocation engine for the `hour_log` Frappe app.
> Clients buy blocks of hours that expire; consultants log work against them;
> the engine produces a monthly statement. I'll run the full test suite, then
> walk through one constructed case that hits a retroactive correction and an
> overdraft."

## 0:30–2:30 — Run the tests

```bash
bench --site hour_logs run-tests --app hour_log --skip-test-records
```

> "36 tests, one class per edge case from the brief — retroactive consumption,
> the expiry boundary, multi-block splits, zero/negative-hour corrections, a
> block bought after work was done, floating-point precision, and overdraft.
> All passing."

Let it finish; point at the `OK` / test count in the output.

## 2:30–3:15 — Reproduce the sample data

```bash
bench --site hour_logs execute hour_log.hour_log_for_cleints.examples.generate_sample.run
```

> "This regenerates sample_input.json and sample_output.json from a live run
> of the real engine — not hand-typed numbers — and rolls back everything it
> creates afterward, so the site is untouched."

Open `apps/hour_log/hour_log/hour_log_for_cleints/examples/sample_input.json`
briefly — point at the four purchased blocks and seven consumption entries,
especially the last entry's `"note"` field explaining it's inserted last but
dated earlier than an existing entry (the retroactive case).

## 3:15–6:00 — Walk the retroactive correction (Edge Case 1)

Open `sample_output.json`, scroll to `"january_statement_history"`.

> "January was reported once — revision 1 — with two entries totaling 5
> hours. Then a third entry, dated January 20th, was added *after* that
> statement already existed."

Point at revision 1: `is_latest: false`, `superseded_by: "STMT-…"`.

> "The old statement isn't edited or deleted — it's kept, and explicitly
> flagged superseded, with a pointer to what replaced it."

Point at revision 2: `is_latest: true`, `previous_revision` pointing back,
`total_consumed_hours` now 6 instead of 5, and the `lines` array — show that
the January 31st entry's split actually changed (it used to split across two
blocks; after the backdated entry claimed the first block's last hour, it now
draws entirely from the second block). Say:

> "That's the subtle part — the backdated entry didn't just add its own
> hours, it reshuffled which block a *later* entry drew from, because the
> engine always replays in the order work actually happened, not the order
> entries were typed in."

## 6:00–7:30 — Walk the overdraft (Edge Case 3 / overdraft)

Scroll to `"march_statement_history"`.

> "In March, a 2-hour block and a partially-used Q1 block together could only
> cover 6 of the 10 hours logged. The engine didn't reject the entry and
> didn't pull from an expired block — the remaining 4 hours show up as their
> own allocation row, `purchased_block: null`, `is_overdraft: true`, and as
> `overdraft_hours: 4.0` on the statement itself."

Point at the `overdraft_hours` field and the `is_overdraft: true` line.

## 7:30–8:00 — Wrap up

> "The README has a one-line decision for every edge case in the brief, plus
> the full reasoning and the exact function each one lives in. That's the
> engine."

Stop recording.
