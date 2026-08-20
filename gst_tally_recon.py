#!/usr/bin/env python3
"""
GSTR-2B  <->  Tally (Purchase Register) Reconciliation Engine
=============================================================

Generic, re-usable. Works on any workbook that has the 12 GSTR-2B style
columns, with a "Source" column telling which side each row came from
(GSTR 2B / Tally), OR with the two sides in two separate sheets/files.

Usage
-----
    python gst_tally_recon.py "Bill To Come.xlsx"
    python gst_tally_recon.py in.xlsx --sheet 2B --out Recon.xlsx
    python gst_tally_recon.py in.xlsx --amount-tol 5 --date-window 30
    python gst_tally_recon.py --gst gstr2b.xlsx --tally tally.xlsx

Matching is TIERED and ONE-TO-ONE (greedy): a row matched at a stronger
tier is removed from the pool before the next tier runs, so nothing is
double counted.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from difflib import SequenceMatcher

import pandas as pd

# ----------------------------------------------------------------------------
# Configuration defaults
# ----------------------------------------------------------------------------
try:                                   # settings come from config.py when
    import config as _cfg                # present, else these defaults apply
    AMOUNT_TOL = _cfg.AMOUNT_TOL
    DATE_WINDOW = _cfg.DATE_WINDOW
    NAME_SIM = _cfg.NAME_SIM
    NAME_LOOSE = _cfg.NAME_LOOSE
    P1_TAX, P2_TAX = _cfg.P1_TAX, _cfg.P2_TAX
except Exception:                       # noqa: BLE001 - config is optional
    AMOUNT_TOL = 1.00      # rupees; |a-b| <= tol counts as equal
    DATE_WINDOW = 15       # days, for the date-tolerant rungs
    NAME_SIM = 0.86        # supplier-name similarity when the name carries it
    NAME_LOOSE = 0.55      # when the other columns already agree exactly
    P1_TAX, P2_TAX = 100000.0, 10000.0

COLS = [
    "Source", "GSTIN", "TradeName", "InvoiceNo", "InvoiceType", "InvoiceDate",
    "InvoiceValue", "TaxableValue", "IGST", "CGST", "SGST", "Cess",
]
NUM_COLS = ["InvoiceValue", "TaxableValue", "IGST", "CGST", "SGST", "Cess"]

RUPEE_SIGN = chr(0x20B9)

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$")

# Ledger heads that are NOT purchase invoices and can never appear in GSTR-2B
# as a supplier bill (bank charges, card accounts, cash/journal heads).
NON_SUPPLIER_RE = re.compile(
    r"\b(BANK|CC\s*A/?C|OD\s*A/?C|A/?C\s*NO|CASH|JOURNAL|SUSPENSE|"
    r"ROUND\s*OFF|KOTAK|AXIS|CANARA|BARODA|INDUS[LI]ND|UNION\s+BANK|"
    r"HDFC|ICICI|SBI|YES\s+BANK|IDFC|BANDHAN|PNB|RBL|AU\s+SMALL)\b", re.I)


# ----------------------------------------------------------------------------
# Normalisation helpers  (these ARE the matching parameters)
# ----------------------------------------------------------------------------
def _s(v) -> str:
    """Safe string: None / NaN / NaT / 'nan' all collapse to ''.
    Without this, pandas' NaN would normalise to the literal 'NAN' and
    blank GSTINs would silently match each other."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none", "-", "na") else s


def norm_gstin(v) -> str:
    return re.sub(r"[^0-9A-Z]", "", _s(v).upper())


def norm_inv(v) -> str:
    """Strict invoice key: uppercase, drop every non-alphanumeric char.
    'CB/25-26/1488' -> 'CB25261488' ; 'cb 25/26 1488' -> same."""
    return re.sub(r"[^0-9A-Z]", "", _s(v).upper())


def core_inv(v) -> str:
    """Loose invoice key: the LAST numeric run with leading zeros stripped.
    'INV/0045' -> '45' ; 'SEAM/25-26/0468' -> '468'.
    Catches prefix/serial-format differences between the portal and Tally."""
    s = norm_inv(v)
    runs = re.findall(r"\d+", s)
    if not runs:
        return s
    return runs[-1].lstrip("0") or "0"


def norm_name(v) -> str:
    s = _s(v).upper()
    s = re.sub(r"\b(M/S|MS|PVT|PRIVATE|LTD|LIMITED|LLP|CO|COMPANY|THE|AND|&|"
               r"ENTERPRISES?|INDIA|INC|CORP|CORPORATION)\b", " ", s)
    return re.sub(r"[^0-9A-Z]", "", s)


def name_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def norm_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _s(v)
    if not s:
        return None
    for f in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%b-%Y",
              "%d-%b-%y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s, dayfirst=True).date()
    except Exception:
        return None


def num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)) and not pd.isna(v):
        return round(float(v), 2)
    try:
        return round(float(re.sub(r"[^0-9.\-]", "", str(v))), 2)
    except ValueError:
        return 0.0


def norm_type(v) -> str:
    """Fold every doc-type spelling into R / CN / DN.
    Credit notes are NEVER matched against invoices."""
    s = _s(v).upper()
    if "CREDIT" in s or s.strip() in ("CN", "C"):
        return "CN"
    if "DEBIT" in s or s.strip() in ("DN", "D"):
        return "DN"
    return "R"


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def _map_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Fuzzy-map whatever headers the file has onto our canonical names."""
    patterns = {
        "Source":       [r"^source$", r"^from$", r"^system$"],
        "GSTIN":        [r"gstin", r"gst\s*no", r"party\s*gst"],
        "TradeName":    [r"trade", r"legal\s*name", r"supplier", r"party",
                         r"^name$", r"vendor"],
        "InvoiceNo":    [r"invoice\s*(number|no|#)", r"^bill\s*(no|number)",
                         r"^doc.*(no|number)", r"voucher\s*(no|number)"],
        "InvoiceType":  [r"invoice\s*type", r"doc.*type", r"^type$",
                         r"^nature", r"voucher\s*type"],
        "InvoiceDate":  [r"invoice\s*date", r"bill\s*date", r"doc.*date",
                         r"^date$"],
        "InvoiceValue": [r"invoice\s*value", r"total.*(value|amount|amt)",
                         r"gross", r"bill\s*(value|amount)"],
        "TaxableValue": [r"taxable", r"assessable", r"net\s*(value|amount)"],
        "IGST":         [r"integrated", r"\bigst\b"],
        "CGST":         [r"central", r"\bcgst\b"],
        "SGST":         [r"state", r"\bsgst\b", r"\butgst\b"],
        "Cess":         [r"cess"],
    }
    out, used = {}, set()
    lower = {c: str(c).strip().lower() for c in df.columns}
    for canon, pats in patterns.items():
        for pat in pats:
            hit = next((c for c in df.columns
                        if c not in used and re.search(pat, lower[c])), None)
            if hit is not None:
                out[canon] = df[hit]
                used.add(hit)
                break
        if canon not in out:
            out[canon] = None
    return pd.DataFrame(out)


def _find_header(raw: pd.DataFrame) -> int:
    """Locate the real header row (files often carry a totals/banner row)."""
    for i in range(min(15, len(raw))):
        cells = " ".join(str(x).lower() for x in raw.iloc[i].tolist())
        if "gstin" in cells and ("invoice" in cells or "taxable" in cells):
            return i
    return 0


def load_sheet(path: str, sheet=0) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
    h = _find_header(raw)
    df = pd.read_excel(path, sheet_name=sheet, header=h, dtype=object)
    df = _map_columns(df)
    df["__file"] = f"{path}[{sheet}]"
    df["__row"] = df.index + h + 2          # 1-based Excel row of the record
    return df


def prepare(df: pd.DataFrame, side: str, absolute=False,
            consolidate=False) -> pd.DataFrame:
    d = df.copy()
    d["side"] = side
    d["gstin"] = d["GSTIN"].map(norm_gstin)
    d["pan"] = d["gstin"].map(lambda g: g[2:12] if len(g) == 15 else "")
    d["inv"] = d["InvoiceNo"].map(norm_inv)
    d["invcore"] = d["InvoiceNo"].map(core_inv)
    d["name"] = d["TradeName"].map(norm_name)
    d["dt"] = d["InvoiceDate"].map(norm_date)
    d["dtype"] = d["InvoiceType"].map(norm_type)
    for c in NUM_COLS:
        d[c.lower()] = d[c].map(num)
    if absolute:
        # Tally exports purchases with a flipped sign; drop the sign entirely
        for c in NUM_COLS:
            d[c.lower()] = d[c.lower()].abs()
    d["tax"] = (d["igst"] + d["cgst"] + d["sgst"] + d["cess"]).round(2)
    if consolidate:
        d = consolidate_bills(d)
    # a row is empty if it carries no identity and no money at all
    d = d[~((d["gstin"] == "") & (d["inv"] == "") & (d["name"] == "")
            & (d["taxablevalue"] == 0) & (d["invoicevalue"] == 0))]
    d = d.reset_index(drop=True)
    d["uid"] = [f"{side[0]}{i:06d}" for i in range(len(d))]
    return d



def consolidate_bills(d: pd.DataFrame) -> pd.DataFrame:
    """Merge multiple ledger lines of ONE bill into a single row.

    A bill split across tax slabs (say a 5% part and an 18% part) arrives as
    two rows. Two shapes occur in the data and both must collapse to one line:

      a) the SAME taxable base repeated once per slab (often +x and -x),
      b) genuinely different line amounts, one per slab.

    So the taxable value is the sum of the DISTINCT line amounts - which takes
    a repeated base once (a) and adds real separate lines (b) - while the tax
    heads are always summed, because tax is additive in both shapes. The
    invoice value is then rebuilt as taxable + total tax so the merged row is
    internally consistent and carries the amount inclusive of tax.

    Rows with no invoice number are never merged: without a bill number there
    is nothing to prove two lines belong together.
    """
    if not len(d):
        return d

    d = d.copy()
    d["__key"] = list(zip(d["gstin"], d["name"], d["inv"], d["dt"], d["dtype"]))
    counts = d["__key"].value_counts()
    mergeable = {k for k, n in counts.items() if n > 1} - {None}
    mergeable = {k for k in mergeable if k[2]}          # invoice no required

    if not mergeable:
        return d.drop(columns="__key")

    keep, merged = [], []
    for key, grp in d.groupby("__key", sort=False):
        if key not in mergeable:
            keep.append(grp)
            continue
        row = grp.iloc[0].copy()
        taxable = round(sum(sorted(set(grp["taxablevalue"].round(2)))), 2)
        for h in ("igst", "cgst", "sgst", "cess"):
            row[h] = round(grp[h].sum(), 2)
        row["taxablevalue"] = taxable
        row["tax"] = round(row["igst"] + row["cgst"] + row["sgst"]
                           + row["cess"], 2)
        row["invoicevalue"] = round(taxable + row["tax"], 2)
        row["__row"] = ", ".join(str(v) for v in grp["__row"])
        row["__merged"] = len(grp)
        merged.append(row.to_frame().T)

    out = pd.concat(keep + merged, ignore_index=True)
    if "__merged" not in out.columns:
        out["__merged"] = 1
    out["__merged"] = out["__merged"].fillna(1).astype(int)
    for c in ("taxablevalue", "invoicevalue", "igst", "cgst", "sgst", "cess",
              "tax"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out.drop(columns="__key").reset_index(drop=True)


def tally_sheet(tal: pd.DataFrame) -> pd.DataFrame:
    """The Tally register as the engine actually used it: all values positive
    and split bills merged into one line inclusive of tax."""
    out = pd.DataFrame({
        "GSTIN": tal["gstin"],
        "Supplier": tal["TradeName"],
        "InvoiceNo": tal["InvoiceNo"],
        "DocType": tal["dtype"],
        "Date": tal["dt"],
        "Taxable": tal["taxablevalue"],
        "IGST": tal["igst"], "CGST": tal["cgst"], "SGST": tal["sgst"],
        "Cess": tal["cess"],
        "TotalTax": tal["tax"],
        "Amount_Incl_Tax": (tal["taxablevalue"] + tal["tax"]).round(2),
        "LinesMerged": tal.get("__merged", 1),
        "SourceRows": tal["__row"],
    })
    return out.sort_values("Amount_Incl_Tax", ascending=False).reset_index(drop=True)



def write_tally_workbook(tal, path):
    return write_layout_workbook(tally_export(tal), path, "Tally")


def write_combined_workbook(gst, tal, path):
    """GSTR-2B as-is + cleaned Tally, stacked, ready to be re-run."""
    both = pd.concat([side_export(gst, "GSTR 2B"), tally_export(tal)],
                     ignore_index=True)
    return write_layout_workbook(both, path, "2B")


SOURCE_COLS = [
    "Source", "GSTIN of supplier", "Trade/Legal name", "Invoice number",
    "Invoice type", "Invoice Date", "Invoice Value(RS)", "Taxable Value (RS)",
    "Integrated Tax(RS)", "Central Tax(RS)", "State/UT Tax(RS)", "Cess(RS)",
]


def side_export(d: pd.DataFrame, source_label: str) -> pd.DataFrame:
    """One side of the reconciliation in the original 12-column layout."""
    out = pd.DataFrame({
        SOURCE_COLS[0]: source_label,
        SOURCE_COLS[1]: d["GSTIN"].map(_s),
        SOURCE_COLS[2]: d["TradeName"].map(_s),
        SOURCE_COLS[3]: d["InvoiceNo"].map(_s),
        SOURCE_COLS[4]: d["InvoiceType"].map(_s),
        SOURCE_COLS[5]: d["dt"],
        SOURCE_COLS[6]: d["invoicevalue"].round(2),
        SOURCE_COLS[7]: d["taxablevalue"].round(2),
        SOURCE_COLS[8]: d["igst"].round(2),
        SOURCE_COLS[9]: d["cgst"].round(2),
        SOURCE_COLS[10]: d["sgst"].round(2),
        SOURCE_COLS[11]: d["cess"].round(2),
    })
    return out.sort_values(
        [SOURCE_COLS[5], SOURCE_COLS[2]]).reset_index(drop=True)


def tally_export(tal: pd.DataFrame) -> pd.DataFrame:
    """The cleaned Tally register: all values positive, split-rate lines of one
    bill merged into a single row."""
    return side_export(tal, "Tally")


def write_layout_workbook(df: pd.DataFrame, path: str,
                          sheet_name: str = "Tally") -> int:
    df = df.copy()
    df.columns = [c.replace("RS", RUPEE_SIGN) for c in df.columns]
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name=sheet_name, index=False)
        ws = xl.book[sheet_name]
        ws.freeze_panes = "A2"
        for col in ws.columns:
            letter = col[0].column_letter
            w = max((len(str(c.value)) for c in col[:300] if c.value), default=8)
            ws.column_dimensions[letter].width = min(max(w + 2, 10), 42)
            if letter == "F":
                for c in col[1:]:
                    c.number_format = "DD/MM/YYYY"
            elif letter in ("G", "H", "I", "J", "K", "L"):
                for c in col[1:]:
                    c.number_format = "#,##0.00"
    return len(df)


# ----------------------------------------------------------------------------
# Matching engine
# ----------------------------------------------------------------------------
class Tier:
    """One rung of the ladder.

    key_fn   -> bucket key (None = this row cannot be matched at this rung)
    ok_fn    -> accept test applied to a candidate pair
    relaxed  -> the columns this rung stops requiring, for the report
    name_min -> supplier-name similarity required, or None to not test it
    """

    def __init__(self, code, label, key_fn, ok_fn, relaxed="",
                 name_min=None, confidence="MEDIUM"):
        self.code, self.label = code, label
        self.key_fn, self.ok_fn = key_fn, ok_fn
        self.relaxed, self.name_min = relaxed, name_min
        self.confidence = confidence


def build_tiers(tol: float, window: int):
    """The ladder, top rung first.

    L1 requires every comparable column to agree. Each rung below relaxes one
    more column. Matching is one-to-one and greedy: a pair fixed at one rung is
    removed before the next runs, so a stronger agreement always wins and
    nothing is counted twice. Document type is required at every rung, so an
    invoice can never pair with a credit or debit note.
    """

    # ---- bucket keys -------------------------------------------------------
    def k_gi(r):  return (r["dtype"], r["gstin"], r["inv"]) if r["gstin"] and r["inv"] else None
    def k_id(r):  return (r["dtype"], r["inv"], r["dt"]) if r["inv"] and r["dt"] else None
    def k_i(r):   return (r["dtype"], r["inv"]) if r["inv"] else None
    def k_pi(r):  return (r["dtype"], r["pan"], r["inv"]) if r["pan"] and r["inv"] else None
    def k_ni(r):  return (r["dtype"], r["name"], r["inv"]) if r["name"] and r["inv"] else None
    def k_gic(r): return (r["dtype"], r["gstin"], r["invcore"]) if r["gstin"] and r["invcore"] else None
    def k_nic(r): return (r["dtype"], r["name"], r["invcore"]) if r["name"] and r["invcore"] else None
    def k_gd(r):  return (r["dtype"], r["gstin"], r["dt"]) if r["gstin"] and r["dt"] else None
    def k_g(r):   return (r["dtype"], r["gstin"]) if r["gstin"] else None
    def k_n(r):   return (r["dtype"], r["name"]) if r["name"] else None

    # ---- column tests ------------------------------------------------------
    def eq(a, b, f):
        return close(a[f], b[f], tol) or close(abs(a[f]), abs(b[f]), tol)

    def taxable(a, b):  return eq(a, b, "taxablevalue")
    def totaltax(a, b): return eq(a, b, "tax")
    def invval(a, b):   return eq(a, b, "invoicevalue")
    def heads(a, b):
        return all(eq(a, b, f) for f in ("igst", "cgst", "sgst", "cess"))
    def samedate(a, b): return a["dt"] is not None and a["dt"] == b["dt"]
    def samename(a, b): return bool(a["name"]) and a["name"] == b["name"]
    def neardate(a, b):
        return (a["dt"] is not None and b["dt"] is not None
                and abs((a["dt"] - b["dt"]).days) <= window)
    def always(a, b):   return True

    def all_of(*tests):
        return lambda a, b: all(t(a, b) for t in tests)

    T = Tier
    return [
        # ---- every column agrees -------------------------------------------
        T("L1", "Every column agrees: GSTIN, name, invoice no, type, date, "
                "invoice value, taxable, IGST, CGST, SGST, cess",
          k_gi, all_of(samename, samedate, invval, taxable, heads),
          relaxed="nothing", confidence="HIGH"),

        # ---- relax one column at a time ------------------------------------
        T("L2", "All columns agree except the spelling of the supplier name",
          k_gi, all_of(samedate, invval, taxable, heads),
          relaxed="supplier name", confidence="HIGH"),

        T("L3", "All agree except invoice value (TCS / freight / round-off)",
          k_gi, all_of(samedate, taxable, heads),
          relaxed="invoice value", confidence="HIGH"),

        T("L4", "All agree except how the tax splits across IGST / CGST / SGST",
          k_gi, all_of(samedate, taxable, totaltax),
          relaxed="invoice value, tax split", confidence="HIGH"),

        T("L5", "All agree except the invoice date",
          k_gi, all_of(taxable, totaltax),
          relaxed="invoice value, date", confidence="HIGH"),

        # ---- GSTIN itself is the odd one out --------------------------------
        T("L6", "Invoice no, date, taxable and tax all agree - only the GSTIN "
                "does not",
          k_id, all_of(taxable, totaltax),
          relaxed="GSTIN", name_min=NAME_LOOSE, confidence="HIGH"),

        T("L7", "Invoice no, taxable and tax agree - GSTIN and date do not",
          k_i, all_of(taxable, totaltax),
          relaxed="GSTIN, date", name_min=NAME_LOOSE, confidence="HIGH"),

        T("L8", "Same PAN and invoice no - only the GSTIN state code differs",
          k_pi, taxable,
          relaxed="GSTIN state code", confidence="HIGH"),

        T("L9", "Name, invoice no and date agree - GSTIN missing or wrong",
          k_ni, all_of(samedate, taxable),
          relaxed="GSTIN, tax split", name_min=NAME_LOOSE, confidence="HIGH"),

        # ---- the money stops agreeing ---------------------------------------
        T("L10", "GSTIN, invoice no and date agree - the tax amount does not",
          k_gi, all_of(samedate, taxable),
          relaxed="tax amount", confidence="HIGH"),

        T("L11", "GSTIN and invoice no agree - the amounts do not",
          k_gi, always,
          relaxed="taxable, tax, date", confidence="HIGH"),

        T("L12", "Same PAN and invoice no - the amounts do not agree",
          k_pi, always,
          relaxed="GSTIN, amounts", confidence="MEDIUM"),

        T("L13", "Name, invoice no and date agree - the amounts do not",
          k_ni, samedate,
          relaxed="GSTIN, amounts", name_min=NAME_SIM, confidence="MEDIUM"),

        # ---- the invoice number stops agreeing -------------------------------
        T("L14", "GSTIN and amounts agree - the invoice no is formatted "
                 "differently",
          k_gic, all_of(taxable, neardate),
          relaxed="invoice no format", confidence="MEDIUM"),

        T("L15", "Name and amounts agree - the invoice no is formatted "
                 "differently",
          k_nic, all_of(taxable, neardate),
          relaxed="GSTIN, invoice no format", name_min=NAME_SIM,
          confidence="MEDIUM"),

        T("L16", "GSTIN, date, taxable and tax agree - the invoice no does not",
          k_gd, all_of(taxable, totaltax),
          relaxed="invoice no", confidence="MEDIUM"),

        # ---- last resort ------------------------------------------------------
        T("L17", "Only GSTIN and the amount agree, within %d days" % window,
          k_g, all_of(taxable, neardate),
          relaxed="invoice no, date", confidence="LOW - VERIFY"),

        T("L18", "Only the supplier name and the amount agree, within %d days"
                 % window,
          k_n, all_of(taxable, neardate),
          relaxed="GSTIN, invoice no, date", name_min=NAME_SIM,
          confidence="LOW - VERIFY"),
    ]


def reconcile(gst: pd.DataFrame, tal: pd.DataFrame, tol: float, window: int):
    G = gst.to_dict("records")
    T = tal.to_dict("records")
    open_g = {r["uid"]: r for r in G}
    open_t = {r["uid"]: r for r in T}
    pairs = []

    for tier in build_tiers(tol, window):
        buckets = {}
        for r in open_t.values():
            k = tier.key_fn(r)
            if k is not None:
                buckets.setdefault(k, []).append(r)
        if not buckets:
            continue
        for guid in list(open_g):
            g = open_g.get(guid)
            if g is None:
                continue
            k = tier.key_fn(g)
            if k is None:
                continue
            cands = [c for c in buckets.get(k, []) if c["uid"] in open_t]
            if not cands:
                continue
            if tier.name_min is not None:
                cands = [c for c in cands
                         if name_sim(g["name"], c["name"]) >= tier.name_min]
            hits = [c for c in cands if tier.ok_fn(g, c)]
            if not hits:
                continue
            # prefer the closest on taxable value, then on date
            def dist(c):
                dd = abs((g["dt"] - c["dt"]).days) if g["dt"] and c["dt"] else 9999
                dv = min(abs(g["taxablevalue"] - c["taxablevalue"]),
                         abs(abs(g["taxablevalue"]) - abs(c["taxablevalue"])))
                return (dv, dd)
            best = min(hits, key=dist)
            pairs.append((tier, g, best))
            open_g.pop(guid, None)
            open_t.pop(best["uid"], None)

    return pairs, list(open_g.values()), list(open_t.values())


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def pair_rows(pairs, tol):
    out = []
    for tier, g, t in pairs:
        dv = round(g["taxablevalue"] - t["taxablevalue"], 2)
        dt_ = round(g["tax"] - t["tax"], 2)
        di = round(g["invoicevalue"] - t["invoicevalue"], 2)
        dd = ((g["dt"] - t["dt"]).days if g["dt"] and t["dt"] else None)
        flags = []
        sign_flip = (g["taxablevalue"] * t["taxablevalue"] < 0
                     and close(abs(g["taxablevalue"]), abs(t["taxablevalue"]), tol))
        if sign_flip:
            flags.append("SIGN-CONVENTION-DIFF")
        elif not close(g["taxablevalue"], t["taxablevalue"], tol):
            flags.append("TAXABLE-DIFF")
        if not close(g["tax"], t["tax"], tol) and not close(abs(g["tax"]), abs(t["tax"]), tol):
            flags.append("TAX-DIFF")
        if not close(g["igst"], t["igst"], tol) or not close(g["cgst"], t["cgst"], tol) \
           or not close(g["sgst"], t["sgst"], tol):              flags.append("TAX-HEAD-DIFF")
        if not close(g["invoicevalue"], t["invoicevalue"], tol): flags.append("INV-VALUE-DIFF")
        if dd not in (0, None):                                  flags.append("DATE-DIFF")
        if dd is None:                                           flags.append("DATE-MISSING")
        if g["inv"] != t["inv"]:                                 flags.append("INVNO-DIFF")
        # A GSTIN discrepancy is reported in its own column rather than as a
        # value difference: the bill still matched on every parameter compared,
        # so it counts as MATCHED, while the GSTIN problem stays visible.
        gstin_note = ""
        if not t["gstin"] and g["gstin"]:
            gstin_note = "GSTIN missing in Tally"
        elif not g["gstin"] and t["gstin"]:
            gstin_note = "GSTIN missing in GSTR-2B"
        elif g["gstin"] != t["gstin"]:
            same_pan = (len(g["gstin"]) == 15 and len(t["gstin"]) == 15
                        and g["gstin"][2:12] == t["gstin"][2:12])
            gstin_note = ("GSTIN state code differs (same PAN)" if same_pan
                          else "GSTIN differs - DIFFERENT PAN")
        if g["dtype"] != t["dtype"]:                             flags.append("DOCTYPE-DIFF")
        conf = tier.confidence

        # Column-by-column audit: exactly which of the comparable columns
        # agree, and which do not. Independent of the tier that matched them.
        agree, differ = [], []
        for col, a, b in (
                ("GSTIN", g["gstin"], t["gstin"]),
                ("Supplier name", g["name"], t["name"]),
                ("Invoice no", g["inv"], t["inv"]),
                ("Doc type", g["dtype"], t["dtype"]),
                ("Date", g["dt"], t["dt"])):
            (agree if a == b and a not in ("", None) else differ).append(col)
        for col, f in (("Invoice value", "invoicevalue"),
                       ("Taxable value", "taxablevalue"),
                       ("IGST", "igst"), ("CGST", "cgst"),
                       ("SGST", "sgst"), ("Cess", "cess")):
            same = (close(g[f], t[f], tol)
                    or close(abs(g[f]), abs(t[f]), tol))
            (agree if same else differ).append(col)
        out.append({
            "Tier": tier.code, "Confidence": conf, "MatchBasis": tier.label,
            "GSTIN_Note": gstin_note,
            "Columns_Agreeing": "%d of 11" % len(agree),
            "Columns_Differing": ", ".join(differ) or "none",
            "Relaxed_At_Tier": tier.relaxed,
            "Status": "MATCHED" if not flags else "MATCHED-WITH-DIFF",
            "Flags": ", ".join(flags),
            "GSTIN_2B": g["gstin"], "GSTIN_Tally": t["gstin"],
            "Supplier_2B": g["TradeName"], "Supplier_Tally": t["TradeName"],
            "InvNo_2B": g["InvoiceNo"], "InvNo_Tally": t["InvoiceNo"],
            "DocType_2B": g["dtype"], "DocType_Tally": t["dtype"],
            "Date_2B": g["dt"], "Date_Tally": t["dt"], "DateDiffDays": dd,
            "Taxable_2B": g["taxablevalue"], "Taxable_Tally": t["taxablevalue"],
            "Diff_Taxable": dv,
            "IGST_2B": g["igst"], "IGST_Tally": t["igst"],
            "CGST_2B": g["cgst"], "CGST_Tally": t["cgst"],
            "SGST_2B": g["sgst"], "SGST_Tally": t["sgst"],
            "Cess_2B": g["cess"], "Cess_Tally": t["cess"],
            "TotalTax_2B": g["tax"], "TotalTax_Tally": t["tax"],
            "Diff_Tax": dt_,
            "InvValue_2B": g["invoicevalue"], "InvValue_Tally": t["invoicevalue"],
            "Diff_InvValue": di,
            "Row_2B": g.get("__row"), "Row_Tally": t.get("__row"),
        })
    return pd.DataFrame(out)


def side_rows(rows, label):
    out = []
    for r in rows:
        reasons = []
        if not r["gstin"]:
            reasons.append("NO-GSTIN")
        elif not GSTIN_RE.match(r["gstin"]):
            reasons.append("BAD-GSTIN-FORMAT")
        if not r["inv"]:
            reasons.append("NO-INVOICE-NO")
        if r["dt"] is None:
            reasons.append("NO/BAD-DATE")
        if r["taxablevalue"] == 0 and r["tax"] == 0:
            reasons.append("ZERO-VALUE")
        if r["dtype"] != "R":
            reasons.append(f"DOC-{r['dtype']}")
        name_txt = _s(r["TradeName"])
        if NON_SUPPLIER_RE.search(name_txt) and not r["inv"]:
            category = "NOT A SUPPLIER BILL (bank/ledger head) - exclude"
        elif r["dtype"] == "CN":
            category = "Credit note"
        elif r["dtype"] == "DN":
            category = "Debit note"
        elif label.startswith("GSTR-2B"):
            category = "ITC available in 2B, not booked"
        else:
            category = "Booked in Tally, not in 2B (bill to come / risk)"
        out.append({
            "Side": label, "Category": category,
            "GSTIN": r["gstin"], "Supplier": r["TradeName"],
            "InvoiceNo": r["InvoiceNo"], "DocType": r["dtype"], "Date": r["dt"],
            "InvoiceValue": r["invoicevalue"], "Taxable": r["taxablevalue"],
            "IGST": r["igst"], "CGST": r["cgst"], "SGST": r["sgst"],
            "Cess": r["cess"], "TotalTax": r["tax"],
            "DataIssues": ", ".join(reasons),
            "ExcelRow": r.get("__row"),
        })
    df = pd.DataFrame(out)
    if len(df):
        df = df.sort_values("TotalTax", ascending=False).reset_index(drop=True)
    return df


def dup_rows(df: pd.DataFrame, label: str) -> pd.DataFrame:
    d = df[(df["gstin"] != "") & (df["inv"] != "")]
    key = ["gstin", "dtype", "inv"]
    g = d.groupby(key).size().reset_index(name="n")
    g = g[g["n"] > 1]
    if g.empty:
        return pd.DataFrame()
    dd = d.merge(g[key], on=key)
    return pd.DataFrame({
        "Side": label, "GSTIN": dd["gstin"], "Supplier": dd["TradeName"],
        "InvoiceNo": dd["InvoiceNo"], "DocType": dd["dtype"], "Date": dd["dt"],
        "Taxable": dd["taxablevalue"], "TotalTax": dd["tax"],
        "ExcelRow": dd["__row"],
    }).sort_values(["GSTIN", "InvoiceNo"]).reset_index(drop=True)


def money(x):
    return round(float(x or 0), 2)


def build_summary(gst, tal, matched, only_g, only_t, tol, window):
    ok = matched[matched["Status"] == "MATCHED"] if len(matched) else matched
    diff = matched[matched["Status"] == "MATCHED-WITH-DIFF"] if len(matched) else matched
    rows = [
        ("PARAMETERS", "", "", ""),
        ("Amount tolerance (Rs)", tol, "", ""),
        ("Date window (days)", window, "", ""),
        ("Supplier-name similarity threshold", NAME_SIM, "", ""),
        ("Relaxed name threshold (other fields exact)", NAME_LOOSE, "", ""),
        ("", "", "", ""),
        ("POPULATION", "Count", "Taxable", "Total Tax"),
        ("GSTR-2B records", len(gst), money(gst["taxablevalue"].sum()), money(gst["tax"].sum())),
        ("Tally records", len(tal), money(tal["taxablevalue"].sum()), money(tal["tax"].sum())),
        ("Difference (2B - Tally)", len(gst) - len(tal),
         money(gst["taxablevalue"].sum() - tal["taxablevalue"].sum()),
         money(gst["tax"].sum() - tal["tax"].sum())),
        ("", "", "", ""),
        ("RESULT", "Count", "Taxable (2B side)", "Tax (2B side)"),
        ("Matched - fully agreeing", len(ok),
         money(ok["Taxable_2B"].sum()) if len(ok) else 0,
         money(ok["TotalTax_2B"].sum()) if len(ok) else 0),
        ("Matched - with differences", len(diff),
         money(diff["Taxable_2B"].sum()) if len(diff) else 0,
         money(diff["TotalTax_2B"].sum()) if len(diff) else 0),
        ("Only in GSTR-2B (not in Tally)", len(only_g),
         money(only_g["Taxable"].sum()) if len(only_g) else 0,
         money(only_g["TotalTax"].sum()) if len(only_g) else 0),
        ("Only in Tally (not in GSTR-2B)", len(only_t),
         money(only_t["Taxable"].sum()) if len(only_t) else 0,
         money(only_t["TotalTax"].sum()) if len(only_t) else 0),
        ("", "", "", ""),
        ("ITC IMPACT", "", "", ""),
        ("ITC available in 2B but not booked", len(only_g), "",
         money(only_g["TotalTax"].sum()) if len(only_g) else 0),
        ("ITC claimed in books but not in 2B (at risk / bill to come)",
         len(only_t), "", money(only_t["TotalTax"].sum()) if len(only_t) else 0),
        ("Net tax difference on matched rows", "", "",
         money(matched["Diff_Tax"].sum()) if len(matched) else 0),
    ]
    if len(matched):
        rows += [("", "", "", ""), ("MATCH TIER BREAKDOWN", "Count", "", "")]
        for code, n in matched.groupby(["Tier", "MatchBasis"]).size().items():
            rows.append((f"{code[0]} - {code[1]}", int(n), "", ""))
        rows += [("", "", "", ""), ("DIFFERENCE TYPES", "Count", "", "")]
        allf = [f for s in matched["Flags"] if s for f in s.split(", ")]
        for f in sorted(set(allf)):
            rows.append((f, allf.count(f), "", ""))
    return pd.DataFrame(rows, columns=["Metric", "Count/Value", "Taxable", "Tax"])


def data_quality(gst, tal):
    rows = []
    for lbl, d in (("GSTR-2B", gst), ("Tally", tal)):
        bad = d[(d["gstin"] != "") & (~d["gstin"].str.match(GSTIN_RE))]
        rows += [
            (lbl, "Total records", len(d)),
            (lbl, "Blank GSTIN", int((d["gstin"] == "").sum())),
            (lbl, "Invalid GSTIN format", len(bad)),
            (lbl, "Blank invoice number", int((d["inv"] == "").sum())),
            (lbl, "Blank/unparseable date", int(d["dt"].isna().sum())),
            (lbl, "Zero taxable value", int((d["taxablevalue"] == 0).sum())),
            (lbl, "Negative taxable value", int((d["taxablevalue"] < 0).sum())),
            (lbl, "Credit notes", int((d["dtype"] == "CN").sum())),
            (lbl, "Debit notes", int((d["dtype"] == "DN").sum())),
            (lbl, "Distinct GSTINs", int(d.loc[d["gstin"] != "", "gstin"].nunique())),
            (lbl, "Tax != rate-consistent with taxable (>1 Rs off any std rate)",
             int(sum(1 for _, r in d.iterrows() if not _rate_ok(r)))),
        ]
    return pd.DataFrame(rows, columns=["Side", "Check", "Count"])


def _rate_ok(r) -> bool:
    """Is total tax consistent with some standard GST rate on the taxable value?"""
    tv = r["taxablevalue"]
    if tv == 0:
        return True
    for rate in (0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28):
        if abs(r["tax"] - round(tv * rate / 100, 2)) <= 1:
            return True
    return False


def supplier_summary(matched, only_g, only_t):
    rec = {}

    def bucket_key(gstin, name):
        """Group by GSTIN; fall back to the normalised supplier name so that
        every blank-GSTIN row does not collapse into one meaningless bucket."""
        g = _s(gstin)
        return g if g else "(no GSTIN) " + (norm_name(name) or "UNKNOWN")

    def add(key, name, bucket, tax, taxable):
        e = rec.setdefault(key, {"GSTIN/Group": key, "Supplier": name, "Matched": 0,
                                 "MatchedWithDiff": 0, "Only2B": 0, "OnlyTally": 0,
                                 "Tax_Only2B": 0.0, "Tax_OnlyTally": 0.0,
                                 "Tax_MatchDiff": 0.0})
        e[bucket] += 1
        if bucket == "Only2B":
            e["Tax_Only2B"] += tax
        elif bucket == "OnlyTally":
            e["Tax_OnlyTally"] += tax
        elif bucket == "MatchedWithDiff":
            e["Tax_MatchDiff"] += tax

    for _, r in matched.iterrows():
        add(bucket_key(r["GSTIN_2B"] or r["GSTIN_Tally"], r["Supplier_2B"]),
            r["Supplier_2B"],
            "Matched" if r["Status"] == "MATCHED" else "MatchedWithDiff",
            abs(r["Diff_Tax"]), 0)
    for _, r in only_g.iterrows():
        add(bucket_key(r["GSTIN"], r["Supplier"]), r["Supplier"],
            "Only2B", r["TotalTax"], r["Taxable"])
    for _, r in only_t.iterrows():
        add(bucket_key(r["GSTIN"], r["Supplier"]), r["Supplier"],
            "OnlyTally", r["TotalTax"], r["Taxable"])

    df = pd.DataFrame(rec.values())
    if not len(df):
        return df

    df["Tax_AtRisk"] = (df["Tax_Only2B"] + df["Tax_OnlyTally"]
                        + df["Tax_MatchDiff"]).round(2)

    # A supplier is clean only when every problem column is zero.
    problems = ["MatchedWithDiff", "Only2B", "OnlyTally",
                "Tax_Only2B", "Tax_OnlyTally", "Tax_MatchDiff", "Tax_AtRisk"]
    clean = (df[problems].abs().sum(axis=1) == 0)
    df["Status"] = clean.map({True: "MATCHED", False: "MISMATCHED"})

    cols = ["GSTIN/Group", "Supplier", "Status"] + [
        c for c in df.columns if c not in ("GSTIN/Group", "Supplier", "Status")]
    df = df[cols]
    return df.sort_values(["Status", "Tax_AtRisk"],
                          ascending=[True, False]).reset_index(drop=True)



# ----------------------------------------------------------------------------
# Two-sheet output: what was compared, and what did not match
# ----------------------------------------------------------------------------
def parameters_sheet(tol, window):
    """Sheet 1 - every parameter the comparison is built on."""
    rows = [
        (1, "GSTIN", "Uppercase, every non-alphanumeric character removed; "
                     "blank / NaN treated as empty",
         "Exact", "Primary identity of the supplier"),
        (2, "PAN (from GSTIN)", "Characters 3-12 of the GSTIN",
         "Exact", "Identifies the same legal entity even when the state code "
                  "is wrong in one system"),
        (3, "Invoice number", "Uppercase, '/', '-', '.', spaces removed",
         "Exact", "CB/25-26/1488 and cb 25 26 1488 are the same bill"),
        (4, "Invoice core number", "Last run of digits, leading zeros dropped",
         "Exact", "SEAM/25-26/0468 -> 468; absorbs series/prefix differences"),
        (5, "Supplier name", "Uppercase; M/S, PVT, LTD, LIMITED, LLP, CO, "
                             "ENTERPRISES, INDIA, & removed; punctuation stripped",
         "Similarity >= %s normally, >= %s once invoice no, date and both "
         "money figures already agree exactly" % (NAME_SIM, NAME_LOOSE),
         "Identifies the supplier when the GSTIN is blank or wrong. A bill "
         "whose invoice no, date, taxable and tax all agree is treated as "
         "matched even if the GSTIN does not - the GSTIN issue is reported "
         "in the GSTIN_Note column instead"),
        (6, "Invoice date", "Parsed day-first from text or Excel serial",
         "Exact, or within %d days in the date-tolerant steps" % window,
         "GSTR-2B sends text dates, Tally sends real dates"),
        (7, "Document type", "Folded to R (invoice) / CN (credit note) / "
                             "DN (debit note)",
         "Exact - locked at every step",
         "A credit note must never be matched against an invoice"),
        (8, "Taxable value", "Rounded to 2 decimals; compared signed or absolute",
         "+/- Rs %.2f" % tol, "The core money test"),
        (9, "IGST", "Rounded to 2 decimals", "+/- Rs %.2f" % tol,
         "Compared as its own head to catch place-of-supply errors"),
        (10, "CGST", "Rounded to 2 decimals", "+/- Rs %.2f" % tol,
         "Compared as its own head"),
        (11, "SGST / UTGST", "Rounded to 2 decimals", "+/- Rs %.2f" % tol,
         "Compared as its own head"),
        (12, "Cess", "Rounded to 2 decimals", "+/- Rs %.2f" % tol,
         "Compared as its own head"),
        (13, "Total tax", "IGST + CGST + SGST + Cess", "+/- Rs %.2f" % tol,
         "Secondary money test"),
        (14, "Invoice value", "Rounded to 2 decimals", "+/- Rs %.2f" % tol,
         "Gross value including TCS, freight and round-off"),
    ]
    return pd.DataFrame(rows, columns=[
        "#", "Parameter compared", "How it is normalised before comparing",
        "Tolerance", "Why this parameter is used"])


_WHY = {
    "TAXABLE-DIFF": "Taxable value differs - partial booking or short billing",
    "TAX-DIFF": "Total tax differs - rate applied or rounding differs",
    "TAX-HEAD-DIFF": "Tax total agrees but the IGST vs CGST/SGST split differs "
                     "- place of supply treated differently",
    "INV-VALUE-DIFF": "Invoice value differs - TCS, freight or round-off "
                      "booked differently",
    "DATE-DIFF": "Invoice date differs - booked in a different period",
    "DATE-MISSING": "One side has no usable invoice date",
    "INVNO-DIFF": "Invoice number differs - prefix or series entered differently",
    "DOCTYPE-DIFF": "Invoice matched against a debit or credit note",
    "SIGN-CONVENTION-DIFF": "Same amount with the opposite sign - Tally export "
                            "convention",
}



def _action(row):
    """What to do about this row, and how urgently. Turns 'read the flags and
    decide' into a sortable instruction."""
    issue = row["Issue"]
    why = str(row.get("Why", ""))
    what = str(row.get("What did not match", ""))
    note = str(row.get("GSTIN_Note", ""))
    tax = 0.0
    for k in ("Tax_2B", "Tax_Tally"):
        try:
            tax = max(tax, abs(float(row.get(k) or 0)))
        except (TypeError, ValueError):
            pass

    if "NOT A SUPPLIER BILL" in why:
        return ("Exclude - bank / ledger head, never appears in GSTR-2B",
                "IGNORE")
    if issue == "MISSING IN TALLY":
        act = ("Book this purchase - ITC is available in GSTR-2B"
               if "Credit note" not in why and "Debit note" not in why
               else "Book this %s from GSTR-2B" % why.lower())
    elif issue == "MISSING IN GSTR-2B":
        act = ("Chase the supplier to file it - ITC claimed but not reported")
    else:
        bits = []
        if "TAX-HEAD-DIFF" in what:
            bits.append("fix place of supply (IGST vs CGST/SGST)")
        if "TAXABLE-DIFF" in what or "TAX-DIFF" in what:
            bits.append("check the amount booked")
        if "DATE-DIFF" in what:
            bits.append("check the invoice date")
        if "INVNO-DIFF" in what:
            bits.append("check the invoice number")
        if "INV-VALUE-DIFF" in what:
            bits.append("check TCS / freight / round-off")
        act = "Correct in Tally: " + ", ".join(bits) if bits else \
              "Review the differing fields"

    if "DIFFERENT PAN" in note:
        act += " | URGENT: booked against a different PAN"
    elif "state code" in note:
        act += " | fix the GSTIN state code in the Tally master"
    elif "missing in Tally" in note:
        act += " | add the GSTIN to the supplier master"

    if "DIFFERENT PAN" in note or tax >= P1_TAX:
        pri = "P1"
    elif tax >= P2_TAX:
        pri = "P2"
    else:
        pri = "P3"
    return act, pri


def not_matched_sheet(matched, only_g, only_t):
    """Sheet 2 - one row for every item that did not match, and why."""
    out = []

    for _, r in only_g.iterrows():
        out.append({
            "Issue": "MISSING IN TALLY",
            "What did not match": "Whole invoice - present in GSTR-2B, "
                                  "no corresponding entry in the books",
            "Why": r["Category"],
            "GSTIN_Note": "",
            "GSTIN_2B": r["GSTIN"], "GSTIN_Tally": "",
            "Supplier": r["Supplier"], "InvoiceNo": r["InvoiceNo"],
            "DocType": r["DocType"], "Date_2B": r["Date"], "Date_Tally": "",
            "Taxable_2B": r["Taxable"], "Taxable_Tally": "", "Diff_Taxable": "",
            "Tax_2B": r["TotalTax"], "Tax_Tally": "", "Diff_Tax": "",
            "InvValue_2B": r["InvoiceValue"], "InvValue_Tally": "",
            "Confidence": "", "DataIssues": r["DataIssues"],
            "Row_2B": r["ExcelRow"], "Row_Tally": "",
        })

    for _, r in only_t.iterrows():
        out.append({
            "Issue": "MISSING IN GSTR-2B",
            "What did not match": "Whole invoice - booked in Tally, "
                                  "not reported by the supplier in GSTR-2B",
            "Why": r["Category"],
            "GSTIN_Note": "",
            "GSTIN_2B": "", "GSTIN_Tally": r["GSTIN"],
            "Supplier": r["Supplier"], "InvoiceNo": r["InvoiceNo"],
            "DocType": r["DocType"], "Date_2B": "", "Date_Tally": r["Date"],
            "Taxable_2B": "", "Taxable_Tally": r["Taxable"], "Diff_Taxable": "",
            "Tax_2B": "", "Tax_Tally": r["TotalTax"], "Diff_Tax": "",
            "InvValue_2B": "", "InvValue_Tally": r["InvoiceValue"],
            "Confidence": "", "DataIssues": r["DataIssues"],
            "Row_2B": "", "Row_Tally": r["ExcelRow"],
        })

    diff = matched[matched["Status"] == "MATCHED-WITH-DIFF"] if len(matched) else matched
    for _, r in diff.iterrows():
        flags = [f for f in str(r["Flags"]).split(", ") if f]
        out.append({
            "Issue": "VALUE MISMATCH",
            "What did not match": ", ".join(flags),
            "Why": " | ".join(_WHY.get(f, f) for f in flags),
            "GSTIN_Note": r.get("GSTIN_Note", ""),
            "GSTIN_2B": r["GSTIN_2B"], "GSTIN_Tally": r["GSTIN_Tally"],
            "Supplier": r["Supplier_2B"], "InvoiceNo": r["InvNo_2B"],
            "DocType": r["DocType_2B"],
            "Date_2B": r["Date_2B"], "Date_Tally": r["Date_Tally"],
            "Taxable_2B": r["Taxable_2B"], "Taxable_Tally": r["Taxable_Tally"],
            "Diff_Taxable": r["Diff_Taxable"],
            "Tax_2B": r["TotalTax_2B"], "Tax_Tally": r["TotalTax_Tally"],
            "Diff_Tax": r["Diff_Tax"],
            "InvValue_2B": r["InvValue_2B"], "InvValue_Tally": r["InvValue_Tally"],
            "Confidence": r["Confidence"], "DataIssues": "",
            "Row_2B": r["Row_2B"], "Row_Tally": r["Row_Tally"],
        })

    for row in out:
        row["Action"], row["Priority"] = _action(row)

    df = pd.DataFrame(out)
    if len(df):
        cols = ["Priority", "Action"] + [c for c in df.columns
                                          if c not in ("Priority", "Action")]
        df = df[cols]
        order = {"MISSING IN TALLY": 0, "MISSING IN GSTR-2B": 1, "VALUE MISMATCH": 2}
        df["__o"] = df["Issue"].map(order)
        df["__t"] = pd.to_numeric(
            df["Tax_2B"].where(df["Tax_2B"] != "", df["Tax_Tally"]),
            errors="coerce").abs().fillna(0)
        df["__p"] = df["Priority"].map({"P1": 0, "P2": 1, "P3": 2,
                                        "IGNORE": 3}).fillna(9)
        df = (df.sort_values(["__p", "__t", "__o"],
                             ascending=[True, False, True])
                .drop(columns=["__o", "__t", "__p"]).reset_index(drop=True))
    return df


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="GSTR-2B vs Tally reconciliation")
    p.add_argument("workbook", nargs="?", help="single file with a Source column")
    p.add_argument("--sheet", default=0, help="sheet name/index (single-file mode)")
    p.add_argument("--gst", help="separate GSTR-2B file")
    p.add_argument("--tally", help="separate Tally file")
    p.add_argument("--gst-sheet", default=0)
    p.add_argument("--tally-sheet", default=0)
    p.add_argument("--out", default="GST_Tally_Reconciliation.xlsx")
    p.add_argument("--amount-tol", type=float, default=AMOUNT_TOL)
    p.add_argument("--date-window", type=int, default=DATE_WINDOW)
    p.add_argument("--no-absolute-tally", action="store_true",
                   help="keep Tally signs as-is (default: force positive)")
    p.add_argument("--no-consolidate-tally", action="store_true",
                   help="do not merge split-rate lines of the same bill")
    p.add_argument("--tally-out",
                   help="also write the cleaned Tally register to its own "
                        "workbook in the original 12-column layout")
    p.add_argument("--combined-out",
                   help="write GSTR-2B + cleaned Tally stacked into one "
                        "sheet, in the original layout, ready to re-run")
    p.add_argument("--two-sheet", action="store_true",
                   help="write only two sheets: Parameters_Compared and Not_Matched")
    a = p.parse_args(argv)

    if a.gst and a.tally:
        gst = prepare(load_sheet(a.gst, a.gst_sheet), "GSTR2B")
        tal = prepare(load_sheet(a.tally, a.tally_sheet), "Tally",
                      absolute=not a.no_absolute_tally,
                      consolidate=not a.no_consolidate_tally)
    elif a.workbook:
        sheet = a.sheet
        try:
            sheet = int(sheet)
        except (TypeError, ValueError):
            pass
        df = load_sheet(a.workbook, sheet)
        src = df["Source"].astype(str).str.upper().str.strip()
        gmask = src.str.contains("2B") | src.str.contains("GSTR") | src.str.contains("PORTAL")
        tmask = src.str.contains("TALLY") | src.str.contains("BOOK")
        if not gmask.any() or not tmask.any():
            sys.exit("Could not split the file by Source. Use --gst/--tally instead.")
        gst = prepare(df[gmask], "GSTR2B")
        tal = prepare(df[tmask], "Tally",
                      absolute=not a.no_absolute_tally,
                      consolidate=not a.no_consolidate_tally)
    else:
        p.error("give a workbook, or --gst and --tally")

    if a.tally_out:
        n = write_tally_workbook(tal, a.tally_out)
        print("\nTally register written to %s  (%d rows)" % (a.tally_out, n))
    if a.combined_out:
        n = write_combined_workbook(gst, tal, a.combined_out)
        print("Combined sheet written to %s  (%d rows: %d GSTR-2B + %d Tally)"
              % (a.combined_out, n, len(gst), len(tal)))

    pairs, rest_g, rest_t = reconcile(gst, tal, a.amount_tol, a.date_window)
    matched = pair_rows(pairs, a.amount_tol)
    only_g = side_rows(rest_g, "GSTR-2B only")
    only_t = side_rows(rest_t, "Tally only")
    summary = build_summary(gst, tal, matched, only_g, only_t,
                            a.amount_tol, a.date_window)
    dq = data_quality(gst, tal)
    dups = pd.concat([d for d in (dup_rows(gst, "GSTR-2B"), dup_rows(tal, "Tally"))
                      if len(d)], ignore_index=True) if True else pd.DataFrame()
    sup = supplier_summary(matched, only_g, only_t)

    ok = matched[matched["Status"] == "MATCHED"] if len(matched) else matched
    diff = matched[matched["Status"] == "MATCHED-WITH-DIFF"] if len(matched) else matched

    if a.two_sheet:
        params = parameters_sheet(a.amount_tol, a.date_window)
        notmatched = not_matched_sheet(matched, only_g, only_t)
        with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
            params.to_excel(xl, sheet_name="Parameters_Compared", index=False)
            notmatched.to_excel(xl, sheet_name="Not_Matched", index=False)
            tally_sheet(tal).to_excel(xl, sheet_name="Tally_Consolidated",
                                      index=False)
            for ws in xl.book.worksheets:
                ws.freeze_panes = "A2"
                for col in ws.columns:
                    w = max((len(str(c.value)) for c in col[:200] if c.value),
                            default=8)
                    ws.column_dimensions[col[0].column_letter].width = \
                        min(max(w + 2, 10), 55)
        print("\nParameters_Compared : %d parameters" % len(params))
        print("Not_Matched         : %d items" % len(notmatched))
        for k, v in notmatched["Issue"].value_counts().items():
            print("   %-20s %6d" % (k, v))
        print("\nReport written to %s\n" % a.out)
        return 0

    with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary", index=False)
        sup.to_excel(xl, sheet_name="By_Supplier", index=False)
        ok.to_excel(xl, sheet_name="Matched_Exact", index=False)
        diff.to_excel(xl, sheet_name="Matched_With_Diff", index=False)
        only_g.to_excel(xl, sheet_name="Only_In_GSTR2B", index=False)
        only_t.to_excel(xl, sheet_name="Only_In_Tally", index=False)
        (dups if len(dups) else pd.DataFrame({"note": ["none"]})).to_excel(
            xl, sheet_name="Duplicates", index=False)
        tally_sheet(tal).to_excel(xl, sheet_name="Tally_Consolidated",
                                  index=False)
        dq.to_excel(xl, sheet_name="Data_Quality", index=False)
        for ws in xl.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col[:200] if c.value), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 42)

    print(f"\nGSTR-2B rows : {len(gst):>6}   Tally rows : {len(tal):>6}")
    print(f"Matched clean: {len(ok):>6}")
    print(f"Matched w/dif: {len(diff):>6}")
    print(f"Only in 2B   : {len(only_g):>6}   tax {money(only_g['TotalTax'].sum()) if len(only_g) else 0:,.2f}")
    print(f"Only in Tally: {len(only_t):>6}   tax {money(only_t['TotalTax'].sum()) if len(only_t) else 0:,.2f}")
    print(f"\nReport written to {a.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
