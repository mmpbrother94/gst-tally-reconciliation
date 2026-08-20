"""Disk-backed store for uploads, jobs and results.

Under Passenger (cPanel) the app runs as several worker processes that are
recycled freely. Anything kept in module memory is invisible to the other
workers and lost on recycle, so a browser polling a job can easily hit a
process that never saw it. Everything shared therefore lives on disk.

Layout, all under STATE_DIR:

    uploads/<id>.bin     the workbook bytes
    uploads/<id>.json    {name, size, at}
    jobs/<id>.json       {state, step, at, ...}
    runs/<id>.pkl        the reconciliation result (DataFrames)
"""

from __future__ import annotations

import json
import os
import pickle
import tempfile
import time
import uuid
from pathlib import Path

try:
    import config
except Exception:                                          # noqa: BLE001
    config = None


def _dir() -> Path:
    d = getattr(config, "STATE_DIR", "")
    base = Path(d) if d else Path(tempfile.gettempdir()) / "gst_tally_state"
    base.mkdir(parents=True, exist_ok=True)
    for sub in ("uploads", "jobs", "runs"):
        (base / sub).mkdir(exist_ok=True)
    return base


KEEP_UPLOADS = int(getattr(config, "KEEP_UPLOADS", 12))
KEEP_RUNS = int(getattr(config, "KEEP_RUNS", 5))
JOB_STALE_SECONDS = int(getattr(config, "JOB_STALE_SECONDS", 900))


def _write_atomic(path: Path, data: bytes):
    """Write via a temp file in the same directory, then rename, so a reader in
    another worker never sees a half-written file.

    On POSIX the rename is atomic and always succeeds. On Windows it fails with
    "access denied" if another process happens to have the destination open for
    reading at that instant, which two workers polling the same job hit
    routinely - so retry briefly before giving up.
    """
    tmp = path.with_suffix(path.suffix + ".%s.tmp" % os.getpid())
    tmp.write_bytes(data)
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 + attempt * 0.005)
    # Last resort: write in place. A concurrent reader may see a short read and
    # will simply retry, which is better than losing the update entirely.
    try:
        path.write_bytes(data)
    finally:
        tmp.unlink(missing_ok=True)


def _read_retry(path: Path, binary=False):
    """Read a file that another worker may be replacing right now."""
    for attempt in range(30):
        try:
            return path.read_bytes() if binary else path.read_text()
        except (PermissionError, FileNotFoundError):
            time.sleep(0.02 + attempt * 0.005)
    return None


def _prune(folder: Path, keep: int, *suffixes):
    try:
        files = sorted(folder.glob("*" + suffixes[0]),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            for suf in suffixes:
                try:
                    old.with_suffix(suf).unlink(missing_ok=True)
                except OSError:
                    pass            # another worker may be reading it
    except OSError:
        pass


# ------------------------------------------------------------------ uploads --
def put_upload(name: str, data: bytes) -> str:
    d = _dir() / "uploads"
    uid = uuid.uuid4().hex
    _write_atomic(d / (uid + ".bin"), data)
    _write_atomic(d / (uid + ".json"), json.dumps(
        {"name": name, "size": len(data), "at": time.time()}).encode())
    _prune(d, KEEP_UPLOADS, ".json", ".bin")
    return uid


def get_upload(uid: str):
    if not uid or not uid.isalnum():
        return None, None
    d = _dir() / "uploads"
    meta, blob = d / (uid + ".json"), d / (uid + ".bin")
    if not meta.exists() or not blob.exists():
        return None, None
    raw = _read_retry(meta)
    data = _read_retry(blob, binary=True)
    if raw is None or data is None:
        return None, None
    try:
        return data, json.loads(raw)["name"]
    except Exception:                                      # noqa: BLE001
        return None, None


def list_uploads():
    d = _dir() / "uploads"
    out = []
    for meta in d.glob("*.json"):
        raw = _read_retry(meta)
        if raw is None:
            continue
        try:
            m = json.loads(raw)
        except Exception:                                  # noqa: BLE001
            continue
        if (d / (meta.stem + ".bin")).exists():
            out.append({"id": meta.stem, "name": m.get("name", "sheet.xlsx"),
                        "size": m.get("size", 0), "at": m.get("at", 0)})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


# --------------------------------------------------------------------- jobs --
def new_job(**fields) -> str:
    jid = uuid.uuid4().hex
    set_job(jid, state="running", step="Starting", **fields)
    return jid


def set_job(jid: str, **fields):
    p = _dir() / "jobs" / (jid + ".json")
    cur = get_job(jid) or {}
    cur.update(fields)
    cur["at"] = time.time()
    _write_atomic(p, json.dumps(cur).encode())


def get_job(jid: str):
    if not jid or not jid.isalnum():
        return None
    p = _dir() / "jobs" / (jid + ".json")
    if not p.exists():
        return None
    raw = _read_retry(p)
    if raw is None:
        return None
    try:
        j = json.loads(raw)
    except Exception:                                      # noqa: BLE001
        return None
    # A worker killed mid-run leaves a job "running" forever; time it out so
    # the browser is told plainly instead of polling until the tab is closed.
    if (j.get("state") == "running"
            and time.time() - j.get("at", 0) > JOB_STALE_SECONDS):
        return {"state": "error",
                "error": "The reconciliation stopped unexpectedly on the "
                         "server. Please try again."}
    return j


# --------------------------------------------------------------------- runs --
def put_run(result) -> str:
    d = _dir() / "runs"
    rid = uuid.uuid4().hex
    _write_atomic(d / (rid + ".pkl"), pickle.dumps(result, protocol=4))
    _prune(d, KEEP_RUNS, ".pkl")
    return rid


def get_run(rid: str):
    if not rid or not rid.isalnum():
        return None
    p = _dir() / "runs" / (rid + ".pkl")
    if not p.exists():
        return None
    raw = _read_retry(p, binary=True)
    if raw is None:
        return None
    try:
        return pickle.loads(raw)
    except Exception:                                      # noqa: BLE001
        return None


def stats():
    d = _dir()
    return {"uploads": len(list(( d / "uploads").glob("*.json"))),
            "runs": len(list((d / "runs").glob("*.pkl"))),
            "jobs": len(list((d / "jobs").glob("*.json"))),
            "state_dir": str(d)}
