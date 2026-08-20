# Deploying to cPanel

cPanel runs Python apps under **Passenger**, which starts several worker
processes and recycles them freely. The app is built for that: uploads, job
status and results live on disk in `STATE_DIR`, not in process memory, so a
browser polling a job always gets an answer no matter which worker serves the
request.

---

## Before you start

Check in cPanel that **Setup Python App** exists (under *Software*). If it
does not, the host does not support Python apps and none of this will work —
ask them to enable it, or use a small VPS instead.

You also need **Python 3.9 or newer**. The app uses pandas, which is heavy:
budget roughly **400–600 MB of RAM** while a large workbook is being read. On
a shared plan capped at 256 MB the app will be killed mid-run. If you are not
sure of your limit, ask the host before going further.

---

## 1. Upload the files

Put these in a folder in your home directory — say `gst_tally` (**not** inside
`public_html`):

```
gst_tally/
├── server.py              ← Passenger loads this
├── gst_tally_recon.py
├── store.py
├── config.py
├── requirements.txt
├── templates/index.html
└── static/style.css, app.js
```

cPanel writes its own `passenger_wsgi.py` when you create the application, so you do not upload one.

Leave out `wsgi.py`, `test_recon.py` and any `.xlsx` — they are not needed in
production, and your data should not sit on the server.

Upload via **File Manager → Upload**, or a zip you then extract.

---

## 2. Create the application

**cPanel → Setup Python App → Create Application**

| Field | Value |
|---|---|
| Python version | 3.9 or newer |
| Application root | `gst_tally` |
| Application URL | choose the domain/subdirectory, e.g. `reco` |
| Application startup file | `server.py` |
| Application Entry point | `app` |

> **Do not put `passenger_wsgi.py` in the startup file box.** cPanel *generates*
> `passenger_wsgi.py` itself from whatever you name there. Naming it
> `passenger_wsgi.py` makes that generated file load itself, and the app dies
> with `RecursionError: maximum recursion depth exceeded`. The startup file is
> the module holding the WSGI callable - `server.py` - and the entry point is
> that callable's name - `app`.

Click **Create**. cPanel builds a virtual environment and shows a command like:

```
source /home/USER/virtualenv/gst_tally/3.9/bin/activate && cd /home/USER/gst_tally
```

Copy it — you need it in the next step.

---

## 3. Install the dependencies

**cPanel → Terminal** (or SSH), then paste the activation command from step 2,
followed by:

```bash
pip install -r requirements.txt
```

If Terminal is disabled, the *Setup Python App* screen has a
**Configuration files** box: put `requirements.txt` there and press **Run Pip
Install**.

This takes a few minutes — pandas is a large wheel.

---

## 4. Set the environment variables

Still in *Setup Python App*, add these under **Environment variables**:

| Name | Value | Why |
|---|---|---|
| `GT_STATE_DIR` | `/home/USER/gst_tally_state` | Writable working directory (required) |
| `GT_MAX_UPLOAD_MB` | `50` | Sensible cap for shared hosting |
| `GT_KEEP_RUNS` | `3` | Fewer results kept = less disk |
| `GT_KEEP_UPLOADS` | `6` | Same, for uploaded workbooks |
| `GT_LOG_LEVEL` | `INFO` | Set to `DEBUG` only while diagnosing |

Replace `USER` with your cPanel username. If you skip `GT_STATE_DIR` the app
falls back to the system temp directory, which some hosts wipe periodically —
set it explicitly.

Press **Restart**.

---

## 5. Check it

Open `https://yourdomain.com/reco/health`. You should see:

```json
{"status": "ok", "runs": 0, "jobs": 0, "uploads": 0, "state_dir": "..."}
```

If that works, open `https://yourdomain.com/reco/` and run a reconciliation
with a small file first.

---

## If something goes wrong

**`RecursionError: maximum recursion depth exceeded`**, with the traceback
repeating `wsgi = load_source('wsgi', 'passenger_wsgi.py')` — the Application
startup file is set to `passenger_wsgi.py`, so cPanel's generated file is
loading itself. Set the startup file to `server.py` and the entry point to
`app`, then Restart.

**A traceback naming a missing module** — the pip install did not complete.
Re-run step 3 with the virtualenv activated first; the prompt must show
`(gst_tally:3.x)` before `pip` exists at all.

**500 with no detail** — read `stderr.log` in the application root, or
cPanel → *Errors*.

**"Permission denied" in the log** — `GT_STATE_DIR` is not writable. Create it
in File Manager and set it to `0755`.

**The job never finishes, or the page reports that it stopped unexpectedly** —
the worker was killed, nearly always for exceeding the memory limit. Try a
smaller file. If a normal month's data cannot complete, the plan does not have
enough RAM and no amount of tuning will fix it; you need a bigger plan or a
VPS.

**Uploads rejected** — the file is over `GT_MAX_UPLOAD_MB`, or the host's own
`LimitRequestBody` is lower. Raise both.

**Static files 404** — confirm `static/` and `templates/` were uploaded intact
and sit next to `server.py`.

**Apache's own "Not Found" page** (it mentions *"a 404 was encountered while
trying to use an ErrorDocument"*) — Apache never reached Passenger. Check that
`public_html/<url-path>/.htaccess` exists and names your app root. If the
domain also runs WordPress, its rewrite rules can swallow the path; giving the
app its own subdomain avoids the conflict entirely.

---

## After any code change

Setup Python App → **Restart**. Passenger caches the loaded application, so an
edited file has no effect until you restart. Touching
`tmp/restart.txt` in the application root does the same thing.

---

## A note on privacy

Uploaded workbooks are written to `STATE_DIR` so the workers can share them,
and the newest few are kept (`GT_KEEP_UPLOADS`) so a file can be re-used
without uploading twice. That is real financial data sitting on the server:

- serve the app over **HTTPS only** — enable AutoSSL in cPanel;
- put it behind authentication if the host offers it (cPanel →
  *Directory Privacy*), since the app itself has no login;
- set `GT_KEEP_UPLOADS=0` if you would rather nothing was retained, at the
  cost of re-uploading for every run.
