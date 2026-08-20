"""cPanel / Passenger entry point.

cPanel's "Setup Python App" looks for this file and imports the name given as
the Application Entry Point (default: `application`).

Nothing here should ever raise: if the app fails to import, Passenger shows a
bare 500 with no explanation, so an import failure is turned into a page that
says what went wrong instead.
"""

import logging
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Writable state lives beside the app unless told otherwise. On cPanel the
# home directory is writable; the app directory usually is too.
os.environ.setdefault("GT_STATE_DIR", os.path.join(HERE, "state"))

try:
    logging.basicConfig(
        level=os.environ.get("GT_LOG_LEVEL", "INFO"),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")

    from server import app as application                     # noqa: F401

except Exception:                                             # noqa: BLE001
    _tb = traceback.format_exc()
    logging.getLogger("gst-tally").error("startup failed\n%s", _tb)

    def application(environ, start_response):                 # noqa: F811
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Startup error</title>"
            "<body style=\"font:15px/1.6 system-ui;padding:40px;max-width:70ch\">"
            "<h1 style='font-size:20px'>The application did not start</h1>"
            "<p>Most often this means a dependency is missing. In cPanel open "
            "<b>Setup Python App</b>, and run "
            "<code>pip install -r requirements.txt</code> from the "
            "application's virtual environment.</p>"
            "<pre style=\"background:#f5f6f8;padding:14px;border-radius:8px;"
            "overflow:auto;font-size:12px\">%s</pre></body>"
            % _tb.replace("&", "&amp;").replace("<", "&lt;")
        ).encode("utf-8")
        start_response("500 Internal Server Error",
                       [("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(body)))])
        return [body]
