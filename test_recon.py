"""Self-contained regression tests for the reconciliation engine.

    python test_recon.py

No pytest needed. Every test builds its own tiny dataset, so the suite proves
the rules are general rather than tuned to one workbook.
"""

import sys

import pandas as pd

import gst_tally_recon as R

COLS = ["Source", "GSTIN", "TradeName", "InvoiceNo", "InvoiceType",
        "InvoiceDate", "InvoiceValue", "TaxableValue", "IGST", "CGST",
        "SGST", "Cess"]

FAILED = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILED.append(name)


def run(rows, tol=1.0, window=15, absolute=True, consolidate=True):
    df = pd.DataFrame(rows, columns=COLS)
    df["__row"] = range(2, 2 + len(df))
    s = df["Source"].astype(str).str.upper()
    g = R.prepare(df[s.str.contains("2B")], "GSTR2B")
    t = R.prepare(df[s.str.contains("TALLY")], "Tally",
                  absolute=absolute, consolidate=consolidate)
    pairs, rg, rt = R.reconcile(g, t, tol, window)
    return R.pair_rows(pairs, tol), rg, rt, t


def B(gstin, name, inv, date, val, tax, igst=0, cgst=0, sgst=0, cess=0):
    return ("GSTR 2B", gstin, name, inv, "Regular", date, val, tax,
            igst, cgst, sgst, cess)


def T(gstin, name, inv, date, val, tax, igst=0, cgst=0, sgst=0, cess=0,
      typ="Regular"):
    return ("Tally", gstin, name, inv, typ, date, val, tax,
            igst, cgst, sgst, cess)


# ---------------------------------------------------------------------------
print("\nMATCHING LADDER")

md, rg, rt, _ = run([
    B("27AAAAA1111A1Z5", "ACME STEEL PRIVATE LIMITED", "INV-1", "05/01/2026",
      11800, 10000, 1800),
    T("27AAAAA1111A1Z5", "ACME STEEL PRIVATE LIMITED", "INV-1", "05/01/2026",
      11800, 10000, 1800),
])
check("L1 - every column agrees", len(md) == 1 and md.iloc[0]["Tier"] == "L1",
      md.iloc[0]["Tier"] if len(md) else "no match")
check("L1 reports 11 of 11 columns agreeing",
      len(md) and md.iloc[0]["Columns_Agreeing"] == "11 of 11")
check("L1 is MATCHED", len(md) and md.iloc[0]["Status"] == "MATCHED")

md, *_ = run([
    B("27AAAAA1111A1Z5", "ACME STEEL PRIVATE LIMITED", "INV-2", "05/01/2026",
      11800, 10000, 1800),
    T("09BBBBB2222B1Z9", "Acme Steel Pvt Ltd", "INV-2", "05/01/2026",
      11800, 10000, 1800),
])
check("L6 - only the GSTIN differs, still matched",
      len(md) == 1 and md.iloc[0]["Tier"] == "L6")
check("L6 records the GSTIN in its own note",
      len(md) and "DIFFERENT PAN" in md.iloc[0]["GSTIN_Note"])
check("L6 counts as MATCHED, not a difference",
      len(md) and md.iloc[0]["Status"] == "MATCHED")
check("L6 names GSTIN as the differing column",
      len(md) and md.iloc[0]["Columns_Differing"] == "GSTIN")

md, *_ = run([
    B("27CCCCC3333C1Z1", "ZENITH CABLES LIMTIED", "ZC/77", "09/01/2026",
      23600, 20000, 3600),
    T("", "Zenith Cables Ltd", "ZC/77", "09/01/2026", 23600, 20000, 3600),
])
check("a typo in the supplier name does not block a certain match",
      len(md) == 1, "matched %d" % len(md))

md, rg, rt, _ = run([
    B("27DDDDD4444D1Z7", "ORBIT PLASTICS", "OP/1", "11/01/2026", 5900, 5000, 900),
    T("27EEEEE5555E1Z3", "Delta Rubber", "DR/2", "28/02/2026", 1180, 1000, 180),
])
check("genuinely different bills are left unmatched",
      len(md) == 0 and len(rg) == 1 and len(rt) == 1)

md, *_ = run([
    B("27FFFFF6666F1Z2", "PRIME WIRES", "PW/9", "10/01/2026", 11800, 10000, 1800),
    T("27FFFFF6666F1Z2", "PRIME WIRES", "PW/9", "10/01/2026", 11800, 10000,
      0, 900, 900),
])
check("tax-head split difference is caught, not hidden",
      len(md) == 1 and "TAX-HEAD-DIFF" in str(md.iloc[0]["Flags"]))

md, *_ = run([
    B("27GGGGG7777G1Z8", "NOVA TOOLS", "NT/3", "12/01/2026", 11800, 10000, 1800),
    T("27GGGGG7777G1Z8", "NOVA TOOLS", "NT/3", "12/01/2026", 11800, 10000,
      1800, 0, 0, 0, "Credit Note"),
])
check("an invoice never pairs with a credit note", len(md) == 0)

# ---------------------------------------------------------------------------
print("\nTALLY CLEAN-UP")

md, rg, rt, tal = run([
    B("27HHHHH8888H1Z4", "VERTEX CO", "VX/1", "15/01/2026", 11800, 10000, 1800),
    T("27HHHHH8888H1Z4", "VERTEX CO", "VX/1", "15/01/2026", -11800, -10000,
      -1800),
])
check("negative Tally values are flipped and still match", len(md) == 1)
check("no negative values survive in the cleaned register",
      (tal["taxablevalue"] >= 0).all())

_, _, _, tal = run([
    T("27JJJJJ9999J1Z6", "SPLIT BILL CO", "SB/1", "20/01/2026", 126149,
      108370.40, 0, 7161.30, 7161.30),
    T("27JJJJJ9999J1Z6", "SPLIT BILL CO", "SB/1", "20/01/2026", -126149,
      -108370.40, 0, 1728, 1728),
    B("27JJJJJ9999J1Z6", "SPLIT BILL CO", "SB/1", "20/01/2026", 126149,
      108370.40, 0, 8889.30, 8889.30),
])
row = tal[tal["inv"] == "SB1"].iloc[0]
check("repeated base counted once when merging split-rate lines",
      abs(row["taxablevalue"] - 108370.40) < 0.01,
      "got %s" % row["taxablevalue"])
check("tax summed across both slabs when merging",
      abs(row["tax"] - 17778.60) < 0.01, "got %s" % row["tax"])
check("merged amount includes tax",
      abs(row["invoicevalue"] - 126149) < 0.01, "got %s" % row["invoicevalue"])

_, _, _, tal = run([
    T("27KKKKK1010K1Z1", "TWO LINE CO", "TL/1", "21/01/2026", 115640, 98000,
      0, 8820, 8820),
    T("27KKKKK1010K1Z1", "TWO LINE CO", "TL/1", "21/01/2026", 74340, 63000,
      0, 5670, 5670),
    B("27KKKKK1010K1Z1", "TWO LINE CO", "TL/1", "21/01/2026", 189980, 161000,
      0, 14490, 14490),
])
row = tal[tal["inv"] == "TL1"].iloc[0]
check("genuinely separate lines are added together",
      abs(row["taxablevalue"] - 161000) < 0.01, "got %s" % row["taxablevalue"])

_, _, _, tal = run([
    T("27LLLLL1111L1Z7", "DATE CO", "DC/1", "01/02/2026", 1180, 1000, 180),
    T("27LLLLL1111L1Z7", "DATE CO", "DC/1", "02/02/2026", 1180, 1000, 180),
])
check("same bill number on different dates is never merged", len(tal) == 2)

# ---------------------------------------------------------------------------
print("\nREPORTING")

md, rg, rt, _ = run([
    B("27MMMMM1212M1Z3", "ALPHA LTD", "AL/1", "03/02/2026", 11800, 10000, 1800),
    T("27MMMMM1212M1Z3", "ALPHA LTD", "AL/1", "03/02/2026", 11800, 10000, 1800),
    B("27NNNNN1313N1Z9", "BETA LTD", "BL/1", "04/02/2026", 590000, 500000, 90000),
])
og = R.side_rows(rg, "GSTR-2B only")
ot = R.side_rows(rt, "Tally only")
sup = R.supplier_summary(md, og, ot)
check("By_Supplier has Status right after Supplier",
      list(sup.columns)[:3] == ["GSTIN/Group", "Supplier", "Status"],
      str(list(sup.columns)[:3]))
alpha = sup[sup["Supplier"] == "ALPHA LTD"].iloc[0]
beta = sup[sup["Supplier"] == "BETA LTD"].iloc[0]
check("a clean supplier is MATCHED", alpha["Status"] == "MATCHED")
check("a supplier with an open item is MISMATCHED",
      beta["Status"] == "MISMATCHED")

nm = R.not_matched_sheet(md, og, ot)
check("Not_Matched leads with Priority and Action",
      list(nm.columns)[:2] == ["Priority", "Action"], str(list(nm.columns)[:2]))
check("a 90,000 rupee item is P2", nm.iloc[0]["Priority"] == "P2",
      nm.iloc[0]["Priority"])
check("Action says what to do",
      "Book this purchase" in nm.iloc[0]["Action"], nm.iloc[0]["Action"])

og2 = R.side_rows(
    R.prepare(pd.DataFrame(
        [T("", "KOTAK CC A/C NO-994411", "", "05/02/2026", 1180, 1000, 180)],
        columns=COLS).assign(__row=2), "Tally").to_dict("records"),
    "Tally only")
check("a bank ledger head is tagged for exclusion",
      "NOT A SUPPLIER BILL" in og2.iloc[0]["Category"], og2.iloc[0]["Category"])

nm2 = R.not_matched_sheet(pd.DataFrame(), pd.DataFrame(columns=og.columns), og2)
check("a bank ledger head is priority IGNORE",
      nm2.iloc[0]["Priority"] == "IGNORE", nm2.iloc[0]["Priority"])

# ---------------------------------------------------------------------------
print("\nPARSING")
check("dd/mm/yyyy is read day-first",
      R.norm_date("05/01/2026").month == 1)
check("blank GSTIN never becomes the text 'nan'", R.norm_gstin(float("nan")) == "")
check("invoice numbers ignore punctuation",
      R.norm_inv("CB/25-26/1488") == R.norm_inv("cb 25 26 1488"))
check("credit note wording is folded to CN", R.norm_type("Credit Note") == "CN")

print("\n%d checks run, %d failed" % (CHECKS, len(FAILED)))
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL TESTS PASSED")
