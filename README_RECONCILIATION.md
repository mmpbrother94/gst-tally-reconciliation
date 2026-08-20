# GSTR-2B ↔ Tally Reconciliation System

`gst_tally_recon.py` — a re-usable engine. Point it at any GSTR-2B-style
workbook and it produces a full reconciliation report.

## Run it

```bash
python gst_tally_recon.py "Bill To Come.xlsx" --sheet 2B
```

Two separate files instead of one stacked sheet:

```bash
python gst_tally_recon.py --gst gstr2b.xlsx --tally purchase_register.xlsx
```

Loosen the tolerances (rounding-heavy books, delayed booking):

```bash
python gst_tally_recon.py "Bill To Come.xlsx" --sheet 2B --amount-tol 5 --date-window 45
```

| Option | Default | Meaning |
|---|---|---|
| `--two-sheet` | off | Write only `Parameters_Compared`, `Not_Matched` and `Tally_Consolidated` |
| `--no-absolute-tally` | off | Keep Tally signs as-is (default forces every Tally value positive) |
| `--no-consolidate-tally` | off | Do not merge split-rate lines of the same bill |
| `--amount-tol` | `1.00` | Rupee tolerance; `abs(a-b) <= tol` counts as equal |
| `--date-window` | `15` | Days allowed between the two dates in date-tolerant tiers |
| `--out` | `GST_Tally_Reconciliation.xlsx` | Output workbook |
| `--sheet` / `--gst-sheet` / `--tally-sheet` | first sheet | Sheet name or index |

Column headers are auto-detected by regex, so it also accepts a Tally
purchase register with headers like *Party GSTIN / Voucher No / Bill Date /
Assessable Value*.

---

## 0. Tally-side pre-processing (GSTR-2B is never altered)

Two things happen to the Tally register before any comparison:

**1. Every value is forced positive.** Tally exports purchases with a flipped
sign; the portal never does. Disable with `--no-absolute-tally`.

**2. Lines of the same bill are merged into one.** A bill split across tax
slabs (say a 5% part and an 18% part) arrives as two ledger rows. Rows are
merged only when **party + invoice number + date + document type all agree** —
a row with no invoice number is never merged, because without a bill number
there is nothing to prove two lines belong together.

Two shapes occur in real data and both collapse correctly:

| Shape | Source rows | Merged result |
|---|---|---|
| Same base repeated per slab (often `+x` and `-x`) | taxable 108,370.40 twice; tax 7,161.30+7,161.30 and 1,728+1,728 | taxable **108,370.40**, tax **17,778.60**, amount **126,149** |
| Genuinely different line amounts | taxable 98,000 (tax 17,640) and 63,000 (tax 11,340) | taxable **161,000**, tax **28,980**, amount **189,980** |

So the taxable value is the sum of the **distinct** line amounts — a repeated
base is taken once, real separate lines are added — while tax heads are always
summed, because tax is additive in both shapes. `Amount_Incl_Tax` is then
rebuilt as taxable + total tax, so the merged row is internally consistent.
Disable with `--no-consolidate-tally`.

## 1. Fields compared (the matching parameters)

Every record is reduced to a normalised form before anything is compared:

| Field | Normalisation applied |
|---|---|
| **GSTIN** | uppercase, strip every non-alphanumeric, `NaN`/blank → empty |
| **PAN** | characters 3–12 of the GSTIN — lets the same PAN match across a wrong state code |
| **Invoice number** | uppercase, strip `/ - space .` → `CB/25-26/1488` = `cb 25 26 1488` |
| **Invoice core no.** | last numeric run, leading zeros stripped → `SEAM/25-26/0468` → `468` |
| **Supplier name** | uppercase, drop `M/S PVT LTD LIMITED LLP CO ENTERPRISES INDIA &`, strip punctuation, then `difflib` similarity ≥ **0.86** |
| **Invoice date** | parsed from text or Excel serial; `dd/mm/yyyy` treated as day-first |
| **Document type** | folded to **R / CN / DN** — a credit note is *never* matched against an invoice |
| **Taxable value** | rounded to 2 dp; compared signed **or** absolute (see sign convention below) |
| **IGST / CGST / SGST / Cess** | each rounded to 2 dp and compared head-by-head |
| **Total tax** | IGST + CGST + SGST + Cess |
| **Invoice value** | rounded to 2 dp |

## 2. Checks performed on every matched pair

Each pair is stamped with these flags:

| Flag | What it means |
|---|---|
| `TAXABLE-DIFF` | taxable value differs beyond tolerance |
| `TAX-DIFF` | total tax differs |
| `TAX-HEAD-DIFF` | totals agree but IGST↔CGST/SGST split differs (inter- vs intra-state error) |
| `INV-VALUE-DIFF` | invoice value differs (usually TCS/round-off/freight in books) |
| `DATE-DIFF` | invoice dates differ |
| `DATE-MISSING` | one side has no usable date |
| `INVNO-DIFF` | invoice numbers differ after normalisation |
| `GSTIN-MISSING-IN-TALLY` / `-IN-2B` | one side has no GSTIN at all |
| `GSTIN-STATECODE-DIFF (same PAN)` | same PAN, wrong state code / check digit in one system |
| `GSTIN-DIFF (different PAN)` | **ITC booked against an entirely different GSTIN** |
| `DOCTYPE-DIFF` | invoice vs debit/credit note mismatch |
| `SIGN-CONVENTION-DIFF` | equal in magnitude, opposite sign (Tally export convention) |

A pair with zero flags is reported as **MATCHED**; anything else as
**MATCHED-WITH-DIFF**.

## 3. Matching tiers

Matching is **tiered and one-to-one (greedy)**. A row matched at a stronger
tier is removed from the pool before the next tier runs, so nothing is
double-counted. Within a tier, ties break toward the closest taxable value,
then the closest date. Document type is part of *every* bucket key.

| Tier | Basis | Confidence |
|---|---|---|
| **T1** | GSTIN + Inv No + Date + Taxable + Tax | HIGH |
| **T2** | GSTIN + Inv No + Taxable + Tax (date differs) | HIGH |
| **T3** | GSTIN + Inv No + Taxable (tax split differs) | HIGH |
| **T4** | GSTIN + Inv No (amount differs) | HIGH |
| **T5** | GSTIN + Inv core no + Taxable (invoice-no format differs) | MEDIUM |
| **T6** | GSTIN + Date + Taxable (invoice no differs) | MEDIUM |
| **T7** | GSTIN + Taxable, dates within window | MEDIUM |
| **T7b** | PAN + Inv No + Taxable (GSTIN state code differs) | MEDIUM |
| **T7c** | PAN + Inv No (amount differs too) | MEDIUM |
| **T8** | Supplier name + Inv No + Taxable (GSTIN blank/wrong) | LOW — verify |
| **T9** | Supplier name + Taxable, dates within window | LOW — verify |

Anything surviving all eleven tiers is genuinely unmatched.

## 4. Output workbook

| Sheet | Contents |
|---|---|
| `Summary` | parameters, populations, results, ITC impact, tier and flag breakdown |
| `By_Supplier` | per-GSTIN scorecard sorted by tax at risk — the follow-up list (rows with no GSTIN are grouped by supplier name instead) |
| `Matched_Exact` | pairs agreeing on every parameter |
| `Matched_With_Diff` | pairs with side-by-side values, per-field differences and flags |
| `Only_In_GSTR2B` | in the portal, not in books → **ITC available but not claimed** |
| `Only_In_Tally` | in books, not in the portal → **ITC at risk / bill to come** |
| `Duplicates` | same GSTIN + doc type + invoice no booked more than once |
| `Data_Quality` | blank/invalid GSTINs, blank invoice nos, zero and negative values, rate consistency |

Every unmatched row carries a `Category` column that separates genuine
reconciling items from noise:

- `NOT A SUPPLIER BILL (bank/ledger head) - exclude` — bank charges, credit-card
  and OD accounts, cash/journal/suspense heads with no invoice number. These can
  never appear in GSTR-2B as a purchase bill and should be taken out of the
  reconciliation base.
- `Credit note` / `Debit note`
- `ITC available in 2B, not booked`
- `Booked in Tally, not in 2B (bill to come / risk)`

Every unmatched row also carries a `DataIssues` column (`NO-GSTIN`,
`BAD-GSTIN-FORMAT`, `NO-INVOICE-NO`, `NO/BAD-DATE`, `ZERO-VALUE`, `DOC-CN`,
`DOC-DN`) explaining *why* it could not be matched, and an `ExcelRow`
pointing back to the source file.

## 5. Data-quality checks run on both sides

Total records · blank GSTIN · invalid GSTIN format (15-char statutory
pattern) · blank invoice number · blank or unparseable date · zero taxable
value · negative taxable value · credit notes · debit notes · distinct
GSTINs · tax not consistent with any standard GST rate (0, 0.1, 0.25, 1,
1.5, 3, 5, 6, 7.5, 12, 18, 28 %) on the taxable value.
