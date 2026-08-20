# GSTR-2B ↔ Tally Reconciliation

Matches your purchase register against the GST portal's GSTR-2B, column by
column, and tells you what to do about every difference it finds.

```bash
pip install -r requirements.txt
python wsgi.py                     # http://127.0.0.1:5000
```

| Command | What it does |
|---|---|
| `python wsgi.py` | Production server (Waitress) |
| `python server.py` | Development server |
| `python gst_tally_recon.py FILE.xlsx --sheet 2B` | Command line, writes an Excel report |
| `python test_recon.py` | Regression tests |

---

## The input

One sheet holding both sides, with a **`Source`** column marking each row
`GSTR 2B` or `Tally`, plus GSTIN, trade name, invoice number, invoice type,
invoice date, invoice value, taxable value, IGST, CGST, SGST and cess.

Headers are matched by pattern, so a raw Tally purchase register with columns
like *Party GSTIN / Voucher No / Bill Date / Assessable Value* is understood
without renaming anything. The two sides can also live in separate files:

```bash
python gst_tally_recon.py --gst portal.xlsx --tally register.xlsx
```

---

## How matching works

Every value is normalised first — GSTIN and invoice numbers stripped of
punctuation, dates parsed day-first, supplier names stripped of `PVT`, `LTD`,
`M/S` and the like, amounts rounded to paise.

Then an **18-rung ladder** runs, strongest first. **L1 requires every
comparable column to agree.** Each rung below gives up exactly one more
column, in order of how little that column proves about identity:

| Rung | What still has to agree | What it gives up |
|---|---|---|
| **L1** | everything | nothing |
| **L2** | GSTIN, invoice no, date, all amounts | name spelling |
| **L3** | GSTIN, invoice no, date, taxable, tax heads | invoice value |
| **L4** | GSTIN, invoice no, date, taxable, total tax | tax split |
| **L5** | GSTIN, invoice no, taxable, tax | date |
| **L6** | invoice no, date, taxable, tax, name | **GSTIN** |
| **L7** | invoice no, taxable, tax, name | GSTIN, date |
| **L8** | PAN, invoice no, taxable | GSTIN state code |
| **L9** | name, invoice no, date, taxable | GSTIN, tax split |
| **L10–L13** | invoice no still agrees | the amounts |
| **L14–L16** | GSTIN/name and amounts agree | the invoice number |
| **L17–L18** | party and amount only | invoice no and date |

Matching is **one-to-one and greedy**: a pair fixed at one rung leaves the
pool before the next runs, so stronger agreement always wins and nothing is
counted twice. Document type is required at every rung — an invoice can never
pair with a credit or debit note.

**A different GSTIN does not break a match.** If the invoice number, date,
taxable value and tax all agree, it is the same bill; the GSTIN discrepancy is
reported in its own `GSTIN_Note` column instead of being treated as a
mismatch. Where four columns already agree exactly, the supplier name only has
to be recognisably similar, so a typo such as `LIMTIED` cannot block a certain
match.

Every matched pair carries **`Columns_Agreeing`** (e.g. `10 of 11`) and
**`Columns_Differing`** (e.g. `GSTIN`), computed independently of the rung
that matched it — so the comparison is auditable column by column.

---

## Tally clean-up

Applied to the Tally side only; GSTR-2B is never altered.

**Values forced positive.** Tally exports purchases with a flipped sign.
Disable with `--no-absolute-tally`.

**Split-rate bills merged.** One bill spread across tax slabs arrives as
several ledger lines. Lines merge only when party, invoice number, date and
document type all agree — a row without an invoice number is never merged.

Two shapes occur and both collapse correctly:

| Shape | Source lines | Merged |
|---|---|---|
| Same base repeated per slab (often `+x` and `−x`) | taxable 108,370.40 twice; tax 7,161.30+7,161.30 and 1,728+1,728 | taxable **108,370.40**, tax **17,778.60** |
| Genuinely different line amounts | 98,000 and 63,000 | taxable **161,000** |

Taxable value is the sum of the **distinct** line amounts, so a repeated base
is counted once while real separate lines are added; tax heads are always
summed. Disable with `--no-consolidate-tally`.

---

## What you get

| Sheet | Contents |
|---|---|
| `Not matched` | Every open item, led by **Priority** and **Action** |
| `Parameters compared` | The columns compared and their tolerances |
| `Matched` / `Matched, differs` | Both sides side by side, per-column differences |
| `Only in GSTR-2B` | ITC available, not booked |
| `Only in Tally` | Booked, not reported by the supplier |
| `Tally (cleaned)` | The register as the engine used it, with `SourceRows` |
| `By supplier` | **Status** = MATCHED / MISMATCHED per supplier |
| `Duplicates` | Same invoice booked twice |
| `Data quality` | Blank or invalid GSTINs, missing dates, rate consistency |

**Status** on *By supplier* is MATCHED only when every problem column is zero,
so a clean supplier needs no review at all.

**Priority** on *Not matched* sorts the queue for you:

| Priority | Meaning |
|---|---|
| `P1` | ≥ ₹1 lakh tax, or booked against a different PAN |
| `P2` | ≥ ₹10,000 tax |
| `P3` | Smaller amounts |
| `IGNORE` | Bank, credit-card and cash ledger heads — not purchase bills |

**Action** says what to do: *Book this purchase — ITC is available in
GSTR-2B*, *Chase the supplier to file it*, *Correct in Tally: fix place of
supply*, *Exclude — bank / ledger head*.

---

## Configuration

Defaults live in `config.py`; every one can be overridden by an environment
variable, so the same code runs unchanged on a laptop and a server.

| Variable | Default | Meaning |
|---|---|---|
| `GT_AMOUNT_TOL` | `1.0` | Rupee tolerance |
| `GT_DATE_WINDOW` | `15` | Days for the date-tolerant rungs |
| `GT_NAME_SIM` | `0.86` | Name similarity when the name carries the match |
| `GT_NAME_LOOSE` | `0.55` | Name similarity when other columns already agree |
| `GT_P1_TAX` | `100000` | Tax at or above which an item is P1 |
| `GT_P2_TAX` | `10000` | Tax at or above which an item is P2 |
| `GT_HOST` / `GT_PORT` | `127.0.0.1` / `5000` | Bind address |
| `GT_THREADS` | `8` | Waitress worker threads |
| `GT_MAX_UPLOAD_MB` | `200` | Upload limit |
| `GT_DATA_DIR` | app folder | Where the file picker looks |
| `GT_KEEP_RUNS` | `5` | Results held in memory |
| `GT_LOG_LEVEL` / `GT_LOG_FILE` | `INFO` / console | Logging |

To expose the app on a network, set `GT_HOST=0.0.0.0` and put it behind a
reverse proxy that terminates TLS. It holds no credentials and writes nothing
to disk on its own.

`GET /health` returns `{"status": "ok", ...}` for uptime checks.

---

## Notes and limits

- Uploaded data is held **in memory only**; the last few runs are kept and a
  file lands on disk only when you press a download button.
- `IGNORE` relies on a keyword list of bank names. If your books use a bank
  that is not listed, those rows fall to `P3` rather than `IGNORE` — they are
  never wrongly matched.
- Rungs L17 and L18 rest on party and amount alone. They are labelled
  **LOW - VERIFY** and should be eyeballed before you act on them.

---

## Author

Built by **Manohar Kumar Sah** ([@mmpbrother94](https://github.com/mmpbrother94)).
