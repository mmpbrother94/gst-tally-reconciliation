"""
GSTR-2B vs Tally - reconciliation web app
=========================================

    python wsgi.py              ->  production (Waitress)
    python server.py            ->  development

Two files go in: the GSTR-2B download and the Tally purchase register. Results
are held in memory per run; a file only lands on disk when you press a
download button.

The Tally side is always cleaned before comparison - values forced positive,
and split-rate lines of one bill merged. Both are inherent to how Tally
exports, so they are applied silently rather than offered as options.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
from flask import Flask, abort, jsonify, render_template, request, send_file

import gst_tally_recon as R
import store

try:
    import config
except Exception:                                          # noqa: BLE001
    config = None

HERE = Path(__file__).parent
KEEP_RUNS = int(getattr(config, "KEEP_RUNS", 5))
MAX_MB = int(getattr(config, "MAX_UPLOAD_MB", 200))

log = logging.getLogger("gst-tally")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


TABLES = {
    "not_matched":     ("Not matched",         "notmatched"),
    "parameters":      ("Parameters compared", "params"),
    "matched_clean":   ("Matched",             "ok"),
    "matched_differs": ("Matched, differs",    "diff"),
    "only_2b":         ("Only in GSTR-2B",     "only_g"),
    "only_tally":      ("Only in Tally",       "only_t"),
    "tally":           ("Tally (cleaned)",     "tally"),
    "by_supplier":     ("By supplier",         "supplier"),
    "data_quality":    ("Data quality",        "dq"),
    "duplicates":      ("Duplicates",          "dups"),
}


# ------------------------------------------------------------------ engine --
def _read_side(data: bytes, sheet, side_label: str) -> pd.DataFrame:
    """Read one uploaded workbook as one side of the reconciliation.

    The whole file is that side, so no Source column is needed. If the file
    happens to carry one (an export from this tool does), rows belonging to
    the other side are dropped rather than silently compared against
    themselves.
    """
    bio = io.BytesIO(data)
    sheet_arg = sheet if sheet else 0
    try:
        raw = pd.read_excel(bio, sheet_name=sheet_arg, header=None, dtype=object)
    except ValueError as exc:
        raise ValueError("Sheet %r was not found in the %s file."
                         % (sheet, side_label)) from exc
    h = R._find_header(raw)
    bio.seek(0)
    df = pd.read_excel(bio, sheet_name=sheet_arg, header=h, dtype=object)
    df = R._map_columns(df)
    df["__row"] = df.index + h + 2

    if df["Source"] is not None and not df["Source"].isna().all():
        s = df["Source"].astype(str).str.upper().str.strip()
        want = (s.str.contains("TALLY") | s.str.contains("BOOK")
                if side_label == "Tally"
                else s.str.contains("2B") | s.str.contains("GSTR")
                | s.str.contains("PORTAL"))
        if want.any():
            df = df[want]

    if not len(df):
        raise ValueError("No usable rows found in the %s file." % side_label)
    return df


def reconcile_files(gst_data, gst_sheet, tal_data, tal_sheet, tol, window,
                    progress):
    progress("Reading the GSTR-2B file")
    gdf = _read_side(gst_data, gst_sheet, "GSTR-2B")

    progress("Reading the Tally file")
    tdf = _read_side(tal_data, tal_sheet, "Tally")

    progress("Normalising and cleaning both sides")
    gst = R.prepare(gdf, "GSTR2B")
    tal = R.prepare(tdf, "Tally", absolute=True, consolidate=True)

    progress("Matching %d GSTR-2B rows against %d Tally rows"
             % (len(gst), len(tal)))
    pairs, rest_g, rest_t = R.reconcile(gst, tal, tol, window)

    progress("Building the report")
    matched = R.pair_rows(pairs, tol)
    only_g = R.side_rows(rest_g, "GSTR-2B only")
    only_t = R.side_rows(rest_t, "Tally only")
    ok = matched[matched["Status"] == "MATCHED"] if len(matched) else matched
    diff = (matched[matched["Status"] == "MATCHED-WITH-DIFF"]
            if len(matched) else matched)
    dups = [d for d in (R.dup_rows(gst, "GSTR-2B"), R.dup_rows(tal, "Tally"))
            if len(d)]

    return {
        "gst": gst, "tal": tal, "matched": matched, "ok": ok, "diff": diff,
        "only_g": only_g, "only_t": only_t,
        "params": R.parameters_sheet(tol, window),
        "notmatched": R.not_matched_sheet(matched, only_g, only_t),
        "tally": R.tally_sheet(tal),
        "supplier": R.supplier_summary(matched, only_g, only_t),
        "dq": R.data_quality(gst, tal),
        "dups": pd.concat(dups, ignore_index=True) if dups else pd.DataFrame(),
        "settings": {"tol": tol, "window": window},
    }


def summarise(r):
    def m(df, col):
        return round(float(df[col].sum()), 2) if len(df) else 0.0

    matched = r["matched"]
    tiers = []
    if len(matched):
        grp = (matched.groupby(["Tier", "MatchBasis", "Confidence"])
               .size().reset_index(name="n"))
        grp["__o"] = grp["Tier"].str.extract(r"(\d+)").astype(int)
        for _, x in grp.sort_values("__o").iterrows():
            tiers.append({"tier": x["Tier"], "basis": x["MatchBasis"],
                          "confidence": x["Confidence"], "count": int(x["n"])})

    flags = {}
    if len(r["diff"]):
        for sflag in r["diff"]["Flags"].dropna():
            for f in str(sflag).split(", "):
                if f:
                    flags[f] = flags.get(f, 0) + 1

    notes = {}
    if len(matched) and "GSTIN_Note" in matched:
        for n in matched["GSTIN_Note"].fillna(""):
            if n:
                notes[n] = notes.get(n, 0) + 1

    return {
        "gst_rows": len(r["gst"]), "tally_rows": len(r["tal"]),
        "matched": len(r["ok"]), "differs": len(r["diff"]),
        "only_2b": len(r["only_g"]), "only_tally": len(r["only_t"]),
        "tax_only_2b": m(r["only_g"], "TotalTax"),
        "tax_only_tally": m(r["only_t"], "TotalTax"),
        "tax_net_diff": m(matched, "Diff_Tax"),
        "gst_taxable": m(r["gst"], "taxablevalue"),
        "tally_taxable": m(r["tal"], "taxablevalue"),
        "merged_bills": int((r["tally"]["LinesMerged"] > 1).sum())
        if len(r["tally"]) else 0,
        "tiers": tiers,
        "flags": sorted(flags.items(), key=lambda kv: -kv[1]),
        "gstin_notes": sorted(notes.items(), key=lambda kv: -kv[1]),
        "settings": r["settings"],
        "tables": [{"id": k, "label": v[0], "rows": len(r[v[1]])}
                   for k, v in TABLES.items()],
    }


def clean(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%d/%m/%Y")
    return d.astype(object).where(pd.notna(d), "")


def _pick(prefix):
    """One side's workbook: a fresh upload, or one uploaded earlier.

    A fresh upload is stored so it can be re-used without uploading twice, and
    so the worker that later reads it need not be the one that received it.
    """
    up = request.files.get(prefix + "_file")
    if up and up.filename:
        data = up.read()
        if not data:
            return None, None
        store.put_upload(up.filename, data)
        return data, up.filename

    uid = (request.form.get(prefix + "_uploaded") or "").strip()
    return store.get_upload(uid)


# ------------------------------------------------------------------ routes --
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/uploads")
def api_uploads():
    """Workbooks uploaded recently, newest first."""
    return jsonify(uploads=[{"id": u["id"], "name": u["name"],
                             "size": u["size"]}
                            for u in store.list_uploads()])


@app.post("/api/run")
def api_run():
    # Fixed by configuration rather than asked of the user: the defaults are
    # right for real data and the only reachable wrong answer (a zero
    # tolerance) silently breaks matching on rounding differences.
    tol = float(getattr(config, "AMOUNT_TOL", 1.0))
    window = int(getattr(config, "DATE_WINDOW", 15))
    gst_sheet = (request.form.get("gst_sheet") or "").strip()
    tal_sheet = (request.form.get("tally_sheet") or "").strip()

    gst_data, gst_label = _pick("gst")
    tal_data, tal_label = _pick("tally")
    if gst_data is None:
        return jsonify(error="Choose the GSTR-2B file."), 400
    if tal_data is None:
        return jsonify(error="Choose the Tally file."), 400

    job = store.new_job(gst_label=gst_label, tally_label=tal_label)

    def work():
        def progress(msg):
            store.set_job(job, state="running", step=msg,
                          gst_label=gst_label, tally_label=tal_label)
            log.info("[%s] %s", job[:8], msg)

        t0 = time.time()
        try:
            r = reconcile_files(gst_data, gst_sheet, tal_data, tal_sheet,
                                tol, window, progress)
            rid = store.put_run(r)
            store.set_job(job, state="done", run=rid, summary=summarise(r),
                          gst_label=gst_label, tally_label=tal_label)
            log.info("[%s] done in %.1fs", job[:8], time.time() - t0)
        except Exception as exc:                                # noqa: BLE001
            log.exception("[%s] failed: %s", job[:8], exc)
            store.set_job(job, state="error", error=str(exc))

    threading.Thread(target=work, daemon=True).start()
    return jsonify(job=job)


@app.get("/api/job/<job>")
def api_job(job):
    j = store.get_job(job)
    return jsonify(j) if j else (jsonify(error="unknown job"), 404)


def _filtered(rid, tid):
    r = store.get_run(rid)
    if not r or tid not in TABLES:
        abort(404)
    df = r[TABLES[tid][1]]
    q = (request.args.get("q") or "").strip()
    if len(df) and q:
        mask = df.astype(str).apply(
            lambda col: col.str.contains(q, case=False, na=False, regex=False))
        df = df[mask.any(axis=1)]
    return df


@app.get("/api/table/<rid>/<tid>")
def api_table(rid, tid):
    df = _filtered(rid, tid)
    page = max(1, int(request.args.get("page") or 1))
    size = min(500, max(10, int(request.args.get("size") or 100)))
    total = len(df)
    view = clean(df.iloc[(page - 1) * size: page * size]) if total else df
    return jsonify(columns=list(df.columns), total=total, page=page,
                   size=size, rows=view.values.tolist() if total else [])


@app.get("/api/download/<rid>/<tid>.csv")
def api_csv(rid, tid):
    df = _filtered(rid, tid)
    buf = io.BytesIO(df.to_csv(index=False).encode("utf-8-sig"))
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name="%s.csv" % tid)


@app.get("/api/download/<rid>/report.xlsx")
def api_xlsx(rid):
    r = store.get_run(rid)
    if not r:
        abort(404)
    buf = io.BytesIO()
    order = ["parameters", "not_matched", "matched_clean", "matched_differs",
             "only_2b", "only_tally", "tally", "by_supplier", "duplicates",
             "data_quality"]
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for tid in order:
            label, key = TABLES[tid]
            d = r[key]
            (d if len(d) else pd.DataFrame({"note": ["none"]})).to_excel(
                xl, sheet_name=label[:31].replace("/", "-"), index=False)
        for ws in xl.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col[:200] if c.value),
                        default=8)
                ws.column_dimensions[col[0].column_letter].width = \
                    min(max(w + 2, 10), 45)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="Reconciliation.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet")


@app.get("/health")
def health():
    d = store.stats()
    d["status"] = "ok"
    return jsonify(d)


@app.errorhandler(413)
def too_big(_):
    return jsonify(error="That file is larger than the %d MB limit." % MAX_MB), 413


@app.errorhandler(500)
def boom(_):
    return jsonify(error="Something went wrong on the server. "
                         "Check the log for details."), 500


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
    log.info("Development server. For production run:  python wsgi.py")
    app.run(host=getattr(config, "HOST", "127.0.0.1"),
            port=getattr(config, "PORT", 5000), debug=False, threaded=True)
