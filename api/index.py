"""Vercel entry point: runs the same request handler as server.py as a Python serverless function.

vercel.json rewrites every URL to /api/index?__p=<original path>, so the console, its static files and the
/api/* routes are all served by server.Handler exactly as they are locally.
"""
import os
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

server.ensure_db()  # fresh demo data in /tmp on each cold start (about 0.2 s)


class handler(server.Handler):  # Vercel looks for a BaseHTTPRequestHandler subclass named `handler`
    def _restore_path(self):
        parts = urlsplit(self.path)
        original = parse_qs(parts.query).get("__p")
        if original:
            self.path = "/" + original[0].lstrip("/")
        server.ensure_db()  # /tmp can be cleared between invocations

    def do_GET(self):
        self._restore_path()
        super().do_GET()

    def do_POST(self):
        self._restore_path()
        super().do_POST()
