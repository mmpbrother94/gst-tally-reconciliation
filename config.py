"""Runtime configuration. Every value can be overridden with an environment
variable, so the same code runs on a laptop and on cPanel unchanged."""

import os
import tempfile
from pathlib import Path


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


BASE_DIR = Path(__file__).resolve().parent

# --- matching (fixed by configuration, not exposed in the UI) ---------------
# Measured on real data: sweeping the date window 0-90 days moves ~1% of pairs
# and does not change the clean-match count at all, while an amount tolerance
# of zero costs hundreds of clean matches to rounding. Neither is a decision
# worth putting in front of a user, so both are settings rather than controls.
AMOUNT_TOL = _f("GT_AMOUNT_TOL", 1.0)        # rupees
DATE_WINDOW = _i("GT_DATE_WINDOW", 15)       # days
NAME_SIM = _f("GT_NAME_SIM", 0.86)           # name carries the match
NAME_LOOSE = _f("GT_NAME_LOOSE", 0.55)       # other columns already agree

# --- priority thresholds ----------------------------------------------------
P1_TAX = _f("GT_P1_TAX", 100000)             # >= this tax value is P1
P2_TAX = _f("GT_P2_TAX", 10000)

# --- server -----------------------------------------------------------------
HOST = os.environ.get("GT_HOST", "127.0.0.1")
PORT = _i("GT_PORT", 5000)
THREADS = _i("GT_THREADS", 8)
MAX_UPLOAD_MB = _i("GT_MAX_UPLOAD_MB", 200)
LOG_LEVEL = os.environ.get("GT_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("GT_LOG_FILE", "")  # empty = console only

# --- shared state -----------------------------------------------------------
# Under Passenger the app runs as several worker processes that are recycled
# freely, so uploads, job status and results are kept on disk rather than in
# memory. This directory must be writable by the account running the app.
STATE_DIR = os.environ.get(
    "GT_STATE_DIR", str(Path(tempfile.gettempdir()) / "gst_tally_state"))
KEEP_UPLOADS = _i("GT_KEEP_UPLOADS", 12)     # workbooks retained
KEEP_RUNS = _i("GT_KEEP_RUNS", 5)            # results retained
JOB_STALE_SECONDS = _i("GT_JOB_STALE_SECONDS", 900)
