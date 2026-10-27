"""
A local server for the web app that tells the browser not to cache anything.

    python webapp/serve.py [port]        # from the repo root; then http://localhost:8765/webapp/

`python -m http.server` sends no cache headers, so Chrome may reuse an old copy of one module
next to a new copy of another (a new export in sets.js, the old sets.js cached: app.js fails to
load). With `Cache-Control: no-store` every reload gets the files as they are on disk.
"""

import http.server
import sys
from functools import partial
from pathlib import Path


class NoStore(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    root = Path(__file__).resolve().parent.parent          # the repo: /webapp/ and /analysis/data/
    print(f"http://localhost:{port}/webapp/ (no cache), serving {root}")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(NoStore, directory=str(root))).serve_forever()
