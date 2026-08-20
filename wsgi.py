"""Production entry point.

    python wsgi.py

Serves the Flask app through Waitress, a production WSGI server that runs
natively on Windows. Configure with GT_HOST / GT_PORT / GT_THREADS.
"""

import logging
import sys

import config
from server import app


def _logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    if config.LOG_FILE:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        handlers=handlers,
    )


def main():
    _logging()
    log = logging.getLogger("gst-tally")
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress is not installed - falling back to the Flask "
                    "development server. Install it with: pip install waitress")
        app.run(host=config.HOST, port=config.PORT, threaded=True)
        return

    log.info("Serving on http://%s:%s  (%d threads)",
             config.HOST, config.PORT, config.THREADS)
    serve(app, host=config.HOST, port=config.PORT, threads=config.THREADS,
          max_request_body_size=config.MAX_UPLOAD_MB * 1024 * 1024,
          ident="gst-tally")


if __name__ == "__main__":
    main()
