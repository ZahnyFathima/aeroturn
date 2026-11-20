"""AeroTurn OCC web server (standard library only). Run: python server.py  ->  http://127.0.0.1:8000"""
import json
import os
import re
import traceback
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent
import decisions
import ledger
import pax_impact
import redteam
import replay
import seed
import services as S
import whatif
from guards import GuardViolation

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
         "/flight.js": ("flight.js", "text/javascript"), "/flight.css": ("flight.css", "text/css"),
         "/extras.js": ("extras.js", "text/javascript"), "/extras.css": ("extras.css", "text/css"),
         "/decisionlog.js": ("decisionlog.js", "text/javascript"), "/decisionlog.css": ("decisionlog.css", "text/css"),
         "/paximpact.js": ("paximpact.js", "text/javascript"), "/paximpact.css": ("paximpact.css", "text/css"),
         "/surfaces.css": ("surfaces.css", "text/css"), "/retro.css": ("retro.css", "text/css"),
         "/flightmap.js": ("flightmap.js", "text/javascript")}
BIG_POST = {"/api/ledger/transcribe": 8_500_000}  # base64 audio; every other POST stays capped at 64 KB
ASSET = re.compile(r"/(jet|globe)/[a-z0-9-]{1,40}\.(webp|svg)")  # intro + globe images: fixed folders, strict names, no traversal
FID = r"(?P<fid>[A-Z0-9]{2,8})"
CID, DID = r"(?P<cid>\d{1,6})", r"(?P<did>\d{1,6})"
CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
GET = {"/api/flights": lambda: S.list_flights(), f"/api/flight/{FID}": S.get_turnaround_and_crew_state,
       f"/api/report/{FID}": agent.get_report, "/api/decisions": lambda: S.decisions(),
       f"/api/ledger/{FID}": ledger.view, "/api/replay": lambda: replay.run(),
       "/api/maintenance": lambda: S.maintenance_queue(), "/api/redteam/history": lambda: redteam.history(),
       f"/api/pax/{FID}": pax_impact.analyse, "/api/calls": lambda: decisions.calls(), f"/api/calls/{CID}/transcript": decisions.transcript,
       "/api/ledger/decisions": lambda: decisions.list_decisions(), f"/api/ledger/decisions/{DID}": decisions.detail,
       "/api/llm": lambda: {"model": agent.MODEL, "key_configured": bool(os.getenv("OPENROUTER_API_KEY"))}}
POST = {
    "/api/report": lambda b: agent.get_report(b["flight_id"], force=True),
    "/api/comms": lambda b: S.log_call(b["flight_id"], b.get("speaker"), b["text"]),
    "/api/lab": lambda b: agent.scenario_lab(b["flight_id"], b.get("priorities"), bool(b.get("regenerate"))),
    "/api/decide": lambda b: S.decide(b["flight_id"], b.get("title", "Decision"), b["actions"], b.get("controller"), b.get("pin"),
                                      b.get("chaos") if b.get("chaos") in ("seat", "bag") else bool(b.get("chaos"))),
    "/api/preview": lambda b: S.preview_passengers(b["flight_id"], b["actions"]),
    "/api/approve": lambda b: S.approve_change(b["id"], b.get("controller"), b.get("pin"), bool(b.get("approve", True))),
    "/api/signoff": lambda b: S.sign_off_maintenance(b["item_id"], b.get("licence")),
    "/api/task": lambda b: S.mark_task_done(b["flight_id"], b["task_id"]),
    "/api/reset": lambda b: seed.build(S.DB) or {"status": "RESET"},
    # Decision Ledger
    "/api/ledger/call/start": lambda b: ledger.start_call(b["flight_id"], b.get("title"), b.get("source", "typed"), b.get("demo_id")),
    "/api/ledger/call/line": lambda b: ledger.add_line(b["call_id"], b.get("speaker"), b["text"], b.get("role")),
    "/api/ledger/call/end": lambda b: ledger.end_call(b["call_id"]),
    "/api/ledger/transcribe": lambda b: ledger.transcribe(b["call_id"], b["audio"], b.get("format", "wav")),
    "/api/ledger/extract": lambda b: ledger.extract(b["call_id"]),
    "/api/ledger/confirm": lambda b: ledger.confirm(b["item_id"], b.get("by"), bool(b.get("confirm", True))),
    "/api/ledger/send": lambda b: ledger.send_for_approval(b["item_id"]),
    "/api/pax/reveal": lambda b: pax_impact.reveal(b["pax_id"], b.get("controller"), b.get("pin")),
    "/api/pax/recommend": lambda b: pax_impact.recommend(b["flight_id"], b.get("pax_ids"), b.get("by"), b.get("overrides")),
    "/api/ledger/action_done": lambda b: decisions.set_action_done(b["item_id"], bool(b.get("done", True)), b.get("by")),
    "/api/ledger/report": lambda b: ledger.report(b["flight_id"]),
    "/api/ledger/recheck": lambda b: ledger.recheck(b["item_id"]),
    "/api/ledger/decisions/report": lambda b: decisions.report(b["did"], bool(b.get("regenerate"))),
    # Guard Attack Mode, what-if, comparison, passenger notifications, baggage-aware rebooking
    "/api/redteam": lambda b: redteam.run(b["flight_id"], "fixed" if b.get("mode") == "fixed" else "full"),
    "/api/whatif": lambda b: whatif.run(b["flight_id"], b.get("params"), b.get("actions"), bool(b.get("narrate", True))),
    "/api/compare": lambda b: whatif.compare(b["flight_id"], b.get("priorities"), bool(b.get("regenerate")), bool(b.get("narrate", True))),
    "/api/notify/draft": lambda b: agent.draft_passenger_notifications(b["flight_id"]),
    "/api/notify/send": lambda b: S.send_pax_messages(b["flight_id"], b.get("messages"), b.get("controller"), b.get("pin")),
    "/api/rebook": lambda b: S.execute_atomic_passenger_rebook(b["flight_id"], None, b.get("chaos") if b.get("chaos") in ("seat", "bag") else False),
}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        for k, v in (("Content-Type", ctype if ctype.startswith("image/") else f"{ctype}; charset=utf-8"), ("Content-Length", str(len(data))), ("Content-Security-Policy", CSP),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def run(self, fn, *args, **kw):
        try:
            self.reply(200, fn(*args, **kw))
        except PermissionError as e:
            self.reply(403, {"error": str(e)})
        except agent.LLMUnavailable as e:
            self.reply(503, {"error": f"{e}. This feature needs the LLM; nothing was faked."})
        except (GuardViolation, ValueError, KeyError, TypeError) as e:
            self.reply(400, {"error": str(e)})
        except Exception:  # noqa: BLE001 - never leak internals to the client
            traceback.print_exc()
            self.reply(500, {"error": "Internal error"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in FILES:
            return self.reply(200, (STATIC / FILES[path][0]).read_bytes(), FILES[path][1])
        if (m := ASSET.fullmatch(path)) and (STATIC / path[1:]).is_file():
            return self.reply(200, (STATIC / path[1:]).read_bytes(), "image/svg+xml" if m[2] == "svg" else "image/webp")
        for pattern, fn in GET.items():
            if m := re.fullmatch(pattern, path):
                return self.run(fn, **m.groupdict())
        self.reply(404, {"error": "Not found"})

    def do_POST(self):
        origin, size = self.headers.get("Origin"), int(self.headers.get("Content-Length") or 0)
        path, kw = self.path, {}
        if m := re.fullmatch(r"/api/ledger/decisions/(\d{1,6})/report", self.path):
            path, kw = "/api/ledger/decisions/report", {"did": int(m[1])}
        limit = BIG_POST.get(path, 65536)

        def refuse(code, msg):
            # Drain the unread body (bounded) so Windows does not reset the socket before the client sees the error.
            left = min(size, 16_000_000)
            while left > 0 and (chunk := self.rfile.read(min(left, 65536))):
                left -= len(chunk)
            self.close_connection = True
            return self.reply(code, {"error": msg})
        # Same-origin check by host, so it works over https behind Railway/Vercel proxies as well as plain http locally.
        hosts = {h for h in (self.headers.get("Host"), self.headers.get("X-Forwarded-Host")) if h}
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or (origin and urlsplit(origin).netloc not in hosts):
            return refuse(403, "Cross-origin or non-JSON request refused")
        if size > limit or path not in POST:
            return refuse(413 if size > limit else 404, "Refused")
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "Invalid JSON"})
        self.run(POST[path], {**(body if isinstance(body, dict) else {}), **kw})


def ensure_db():
    if not os.path.exists(S.DB) or not seed.has_current_schema(S.DB):
        seed.build(S.DB)


if __name__ == "__main__":
    ensure_db()
    port = int(os.getenv("PORT", "8000"))
    # Localhost only on your machine; on Railway (or with HOST=0.0.0.0) listen on all interfaces so the platform can route to it.
    host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1")
    print(f"AeroTurn OCC on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
