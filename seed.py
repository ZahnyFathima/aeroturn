"""Builds the synthetic FRA-hub disruption dataset (deterministic). EK812 is the headline scenario.
All times are minutes relative to now (T+0). INBOUND t_min = scheduled arrival, others = scheduled departure."""
import os
import random
import sqlite3
from datetime import datetime, timedelta

from demo_calls import DEMO_CALLS, resolve

SCHEMA = """
CREATE TABLE flights(id TEXT PRIMARY KEY, kind TEXT NOT NULL, origin TEXT, dest TEXT NOT NULL, lat REAL, lon REAL,
  t_min INT NOT NULL, delay_min INT NOT NULL DEFAULT 0, block_min INT NOT NULL, distance_km INT NOT NULL,
  aircraft_type TEXT, tail TEXT, gate TEXT, next_leg TEXT, status TEXT NOT NULL DEFAULT 'SCHEDULED');
CREATE TABLE passengers(id TEXT PRIMARY KEY, name TEXT NOT NULL, tier TEXT NOT NULL, inbound TEXT, orig_flight TEXT, dest TEXT);
CREATE TABLE seats(flight_id TEXT NOT NULL REFERENCES flights(id), seat_no TEXT NOT NULL,
  pax_id TEXT UNIQUE REFERENCES passengers(id),            -- UNIQUE: one passenger can never hold two seats
  PRIMARY KEY(flight_id, seat_no));                         -- PK: one seat can never hold two passengers
CREATE TABLE bags(id TEXT PRIMARY KEY, pax_id TEXT NOT NULL REFERENCES passengers(id),
  flight_id TEXT NOT NULL REFERENCES flights(id), status TEXT NOT NULL DEFAULT 'CHECKED');
CREATE TABLE crew(id TEXT PRIMARY KEY, name TEXT, role TEXT NOT NULL, type_rating TEXT, status TEXT NOT NULL,
  flight_id TEXT, duty_used INT NOT NULL, location TEXT, walk_min INT NOT NULL);
CREATE TABLE engineers(id TEXT PRIMARY KEY, name TEXT, licence TEXT NOT NULL, location TEXT);
CREATE TABLE maintenance(id TEXT PRIMARY KEY, tail TEXT NOT NULL, item TEXT NOT NULL, severity TEXT NOT NULL,
  status TEXT NOT NULL, engineer_id TEXT REFERENCES engineers(id), signed_by TEXT);
CREATE TABLE gates(id TEXT PRIMARY KEY, kind TEXT NOT NULL, occupied_by TEXT);
CREATE TABLE tasks(flight_id TEXT, id TEXT, name TEXT, minutes INT NOT NULL, deps TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'PENDING', PRIMARY KEY(flight_id, id));
CREATE TABLE comms(id INTEGER PRIMARY KEY, flight_id TEXT, at TEXT, speaker TEXT, text TEXT);
CREATE TABLE changes(id INTEGER PRIMARY KEY, flight_id TEXT, title TEXT, actions TEXT, status TEXT, reason TEXT,
  kpis TEXT, proposed_by TEXT, decided_by TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE notifications(id INTEGER PRIMARY KEY, change_id INT, flight_id TEXT, recipient TEXT, message TEXT,
  ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE reports(flight_id TEXT PRIMARY KEY, data TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), actor TEXT, action TEXT, detail TEXT);
CREATE TABLE calls(id INTEGER PRIMARY KEY, flight_id TEXT NOT NULL, title TEXT, source TEXT, status TEXT NOT NULL DEFAULT 'LIVE',
  started TEXT DEFAULT (strftime('%H:%M:%S','now','localtime')), ended TEXT, extracted_at TEXT, demo_id TEXT);
CREATE TABLE call_lines(call_id INT NOT NULL REFERENCES calls(id), n INT NOT NULL, at TEXT NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL,
  role TEXT, PRIMARY KEY(call_id, n));
CREATE TABLE ledger_items(id INTEGER PRIMARY KEY, call_id INT NOT NULL REFERENCES calls(id), flight_id TEXT NOT NULL, kind TEXT NOT NULL,
  problem TEXT, summary TEXT, made_by TEXT, owner TEXT, line_n INT NOT NULL, quote TEXT, at TEXT, confidence REAL, flags TEXT,
  priority INT, actions TEXT, guard_status TEXT, guard_reason TEXT, status TEXT NOT NULL DEFAULT 'UNCONFIRMED',
  confirmed_by TEXT, change_id INT, label TEXT, value TEXT, due TEXT, category TEXT, done INT NOT NULL DEFAULT 0, done_by TEXT,
  ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE ledger_reports(flight_id TEXT PRIMARY KEY, data TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE decision_reports(item_id INTEGER PRIMARY KEY, data TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE flight_info(flight_id TEXT PRIMARY KEY, delay_reason TEXT, reason_source TEXT);   -- reason as stated in the source message
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pax_messages(id INTEGER PRIMARY KEY, flight_id TEXT, pax_id TEXT, message TEXT, approved_by TEXT,
  ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

# id, origin, lat, lon, sched arrival, delay, block, km, type, tail, outbound id, outbound dep, outbound gate, pax
INBOUND = [
    ("EK812", "DXB", 25.25, 55.36, -75, 75, 380, 4800, "B777", "A6-EQB", "EK813", 15, "R4", 0),
    ("QR069", "DOH", 25.27, 51.61, 40, 35, 360, 4500, "B788", "A7-BCA", "QR070", 150, "C4", 26),
    ("AC876", "YYZ", 43.68, -79.63, 95, 20, 480, 6300, "B788", "C-GHPQ", "AC877", 220, "C6", 24),
    ("UA960", "ORD", 41.98, -87.90, 130, 0, 510, 6970, "B777", "N2749U", "UA961", 260, "C8", 30),
    ("ET706", "ADD", 8.98, 38.80, 60, 55, 400, 5300, "A359", "ET-AYA", "ET707", 190, "D2", 22),
    ("AI121", "DEL", 28.56, 77.10, 110, 10, 480, 6100, "B788", "VT-ANA", "AI122", 240, "D4", 25),
    ("TK1589", "IST", 40.98, 28.82, 25, 15, 180, 1870, "A320", "TC-JPA", "TK1590", 100, "D6", 20),
    ("SA260", "JNB", -26.14, 28.25, 170, 40, 590, 8660, "A359", "ZS-SNA", "SA261", 310, "E2", 22),
    ("MS785", "CAI", 30.12, 31.41, 50, 65, 245, 2900, "A320", "SU-GCA", "MS786", 160, "E4", 24),
    ("AA70", "DFW", 32.90, -97.04, 210, 0, 580, 8400, "B777", "N717AN", "AA71", 340, "E6", 26),
    ("AI131", "BOM", 19.09, 72.87, 150, 25, 540, 6600, "B788", "VT-ANB", "AI132", 280, "E8", 24),
]
# id, dest, dep, km, free seats
ONWARD = [("LH900", "LHR", 10, 650, 0), ("LH902", "LHR", 150, 650, 10), ("LH906", "LHR", 330, 650, 6),
          ("LH1120", "MAD", 20, 1420, 0), ("LH1124", "MAD", 210, 1420, 8), ("LH1128", "MAD", 420, 1420, 6),
          ("LH400", "JFK", 30, 6200, 0), ("LH404", "JFK", 300, 6200, 12),
          ("LH1282", "ATH", 120, 1800, 5), ("LH1286", "ATH", 400, 1800, 6), ("LH760", "DEL", 180, 6100, 5),
          ("LH1034", "CDG", 90, 450, 4), ("LH1040", "CDG", 240, 450, 6), ("LH1044", "CDG", 420, 450, 8),
          ("LH1830", "BCN", 160, 1090, 3), ("LH1836", "BCN", 380, 1090, 6), ("LH1500", "VIE", 130, 600, 4),
          ("LH1504", "VIE", 350, 600, 6)]
STANDBY_B777 = [  # hand-built so the EK812 demo has both legal and illegal candidates
    ("SB-01", "CPT", "B777", "STANDBY", 0, "Crew room T1", 12), ("SB-02", "CPT", "B777", "STANDBY", 0, "Airport hotel", 55),
    ("SB-03", "FO", "B777", "STANDBY", 0, "Crew room T1", 15), ("SB-04", "FO", "B777", "STANDBY", 420, "Crew room T2", 18),
    ("SB-05", "PUR", "B777", "STANDBY", 0, "Crew room T1", 10), ("SB-06", "PUR", "A380", "STANDBY", 0, "Crew room T1", 9),
    ("SB-07", "CC", "B777", "STANDBY", 0, "Crew room T1", 8), ("SB-08", "CC", "B777", "STANDBY", 0, "Crew room T2", 20),
    ("SB-09", "CC", "B777", "STANDBY", 0, "Crew room T1", 14), ("SB-10", "CC", "B777", "STANDBY", 0, "Home standby", 40),
    ("SB-11", "CC", "B777", "OFF", 0, "Home", 90)]
ROLES = ["CPT", "FO", "PUR", "CC", "CC", "CC"]
CREW_DUTY = {"EK812": [760, 760, 755, 750, 748, 745],  # duty already used per role; everyone else reports fresh
             "QR069": [0, 0, 0, 300, 0, 0], "ET706": [0, 120, 0, 0, 0, 0], "TK1589": [0, 0, 0, 420, 0, 0], "MS785": [0, 0, 300, 0, 0, 0]}
ENGINEERS = [("E1", "K. Weber", "B1-20417", "Line ops T1"), ("E2", "A. Rossi", "B1-31882", "Line ops T2"),
             ("E3", "J. Okafor", "B1-44120", "Hangar 3")]
MAINTENANCE = [("M-1", "A6-EQB", "Hydraulic system B pressure fluctuation", "NO-GO", "OPEN"),
               ("M-2", "A6-EQB", "Cabin reading light 23C inop (MEL)", "DEFERRABLE", "OPEN"),
               ("M-3", "SU-GCA", "Bleed air valve fault message", "NO-GO", "OPEN"),
               ("M-4", "A7-BCA", "Galley oven 2 inop (MEL)", "DEFERRABLE", "OPEN"),
               ("M-5", "VT-ANA", "Tyre pressure check", "NO-GO", "SIGNED")]
GATES = [("R4", "REMOTE", "EK813"), ("R6", "REMOTE", None), ("A5", "CONTACT", "LH1282"), ("A7", "CONTACT", None),
         ("B12", "CONTACT", None), ("B14", "CONTACT", "LH760")]
TASKS = [("DEBOARD", "Deboarding", 25, ""), ("BAGS_OFF", "Ramp bags unload", 30, ""), ("MX_CHECK", "Maintenance check", 40, ""),
         ("CLEAN", "Cabin cleaning", 25, "DEBOARD"), ("CATERING", "Catering", 30, "DEBOARD"), ("FUEL", "Fuelling", 35, "DEBOARD"),
         ("BAGS_ON", "Ramp bags load", 30, "BAGS_OFF"), ("BOARDING", "Boarding", 35, "CLEAN,CATERING,FUEL,MX_CHECK"),
         ("DOORS", "Doors closed / pushback", 5, "BOARDING,BAGS_ON")]
EK812_CALLS = [
    ("T-40", "Captain EK812", "OCC, EK812. We're about 75 minutes late after holding. Be advised hydraulic system B pressure "
                              "fluctuated on descent. It's back in limits but it's going in the tech log, engineering must clear it before EK813."),
    ("T-30", "Crew control", "Heads-up on the EK812 crew: they'll be at 12 hours 40 of duty on block. They cannot legally take EK813, "
                             "we need a full standby crew."),
    ("T-20", "Transfer desk", "45 tight connections off EK812, two unaccompanied minors for London and three wheelchair passengers. "
                              "LH900 to Heathrow is already boarding."),
    ("T-10", "Ramp control", "EK812 is going onto remote stand R4, no jet bridge. Ambulift is 25 minutes out, that's a problem for the PRMs.")]
FIRST = "Aisha Omar Lena Jonas Priya Diego Mei Noah Sara Yusuf Elena Tom Fatima Luca Hana Ivan".split()
LAST = "Khan Muller Rossi Garcia Chen Patel Novak Silva Haddad Becker Kim Okafor Larsen Dubois".split()


def has_current_schema(path):
    """False when the database predates the Decision Ledger tables (the server then rebuilds the demo dataset)."""
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('ledger_items', 'pax_messages', 'decision_reports', 'flight_info', 'meta')").fetchone()[0] == 5
    finally:
        con.close()


def build(path=None):
    path = path or os.getenv("AEROTURN_DB", "aeroturn.sqlite")
    if os.path.exists(path):
        os.remove(path)
    rng, counter = random.Random(812), {}
    name = lambda: f"{rng.choice(FIRST)} {rng.choice(LAST)}"  # noqa: E731
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    def seat(fid):
        n = counter[fid] = counter.get(fid, 0) + 1
        return f"{(n - 1) // 6 + 10}{'ABCDEF'[(n - 1) % 6]}"

    def pax(pid, tier, inbound, fid, dest):
        con.execute("INSERT INTO passengers VALUES (?,?,?,?,?,?)", (pid, name(), tier, inbound, fid, dest))
        con.execute("INSERT INTO seats VALUES (?,?,?)", (fid, seat(fid), pid))
        for b in range(rng.choice([1, 1, 2])):
            con.execute("INSERT INTO bags(id, pax_id, flight_id) VALUES (?,?,?)", (f"B{pid[1:]}{b}", pid, fid))

    for f in ONWARD:
        con.execute("INSERT INTO flights(id,kind,origin,dest,t_min,block_min,distance_km,aircraft_type,gate) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f[0], "ONWARD", "FRA", f[1], f[2], 120, f[3], "A320", "A1"))
    n = 0
    for i, (fid, org, lat, lon, t, d, blk, km, typ, tail, nxt, nxt_t, gate, npax) in enumerate(INBOUND):
        con.execute("INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, "INBOUND", org, "FRA", lat, lon, t, d, blk, km, typ, tail, None, nxt, "ARRIVED" if t + d <= 0 else "ENROUTE"))
        con.execute("INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nxt, "OUTBOUND", "FRA", org, None, None, nxt_t, 0, blk, km, typ, tail, gate, None, "SCHEDULED"))
        nogo = any(m[1] == tail and m[3] == "NO-GO" and m[4] == "OPEN" for m in MAINTENANCE)
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        [(fid, *tk, "PENDING" if tk[0] != "MX_CHECK" or nogo else "DONE") for tk in TASKS])
        duty = CREW_DUTY.get(fid, [0] * len(ROLES))
        if fid != "EK812":
            reason = rng.choice(["ATC slot restriction at departure.", "Late inbound aircraft.", "Headwinds en route.",
                                 "Catering delay at origin."]) if d else None
            con.execute("INSERT INTO comms(flight_id, at, speaker, text) VALUES (?,?,?,?)",
                        (fid, "T-15", f"Dispatch {fid}", f"{fid} from {org} estimating {d} minutes late. " + reason if d
                         else f"{fid} from {org} on schedule, no issues reported."))
            if reason:
                con.execute("INSERT INTO flight_info VALUES (?,?,?)", (fid, reason.rstrip("."), f"Dispatch {fid} message"))
        else:
            con.execute("INSERT INTO flight_info VALUES (?,?,?)", (fid, "Airborne holding before landing", "Captain EK812 call"))
        for j, (role, used) in enumerate(zip(ROLES, duty)):
            con.execute("INSERT INTO crew VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"{fid}-{j + 1}", name(), role, typ, "ON_DUTY", fid, used, f"On {fid}", 0))
        for _ in range(npax):  # generic connecting passengers: feasible on schedule, delay may break them
            n += 1
            o = rng.choice([f for f in ONWARD if f[2] >= t + 50])
            pax(f"P{n:04d}", rng.choices(["STANDARD", "ELDERLY", "PRM", "INFANT", "UM"], [85, 6, 4, 3, 2])[0], fid, o[0], o[1])

    # EK812 headline: 45 tight connections (12 vulnerable) + 235 comfortable ones
    at_risk = ["LH900"] * 15 + ["LH1120"] * 12 + ["LH400"] * 18
    tiers = ["UM", "UM", "PRM", "PRM", "PRM", "MEDICAL", "MEDICAL", "INFANT", "INFANT", "ELDERLY", "ELDERLY", "ELDERLY"]
    tiers += ["STANDARD"] * (len(at_risk) - len(tiers))
    rng.shuffle(tiers)
    safe = ["LH1282"] * 120 + ["LH760"] * 115
    tiers += rng.choices(["STANDARD", "ELDERLY", "INFANT", "PRM"], [90, 5, 3, 2], k=len(safe))
    dest = {f[0]: f[1] for f in ONWARD}
    for fid, tier in zip(at_risk + safe, tiers):
        n += 1
        pax(f"P{n:04d}", tier, "EK812", fid, dest[fid])
    con.execute("INSERT INTO passengers VALUES ('P-EXT','Booking from another sales channel','STANDARD',NULL,NULL,NULL)")
    for f in ONWARD:
        for _ in range(f[4]):
            con.execute("INSERT INTO seats VALUES (?,?,NULL)", (f[0], seat(f[0])))

    standby = list(STANDBY_B777)
    for typ in ("B788", "A359", "A320"):
        standby += [(f"SB-{typ}-{k}", r, typ, "STANDBY", 0, rng.choice(["Crew room T1", "Crew room T2"]), rng.randint(5, 35))
                    for k, r in enumerate(["CPT", "FO", "PUR", "CC", "CC", "CC"], 1)]
    con.executemany("INSERT INTO crew VALUES (?,?,?,?,?,NULL,?,?,?)", [(s[0], name(), *s[1:]) for s in standby])
    con.executemany("INSERT INTO engineers VALUES (?,?,?,?)", ENGINEERS)
    con.executemany("INSERT INTO maintenance VALUES (?,?,?,?,?,NULL,NULL)", MAINTENANCE)
    con.executemany("INSERT INTO gates VALUES (?,?,?)", GATES)
    con.executemany("INSERT INTO comms(flight_id, at, speaker, text) VALUES ('EK812',?,?,?)", EK812_CALLS)
    con.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    # Decision Ledger: calls that already ended before the demo starts (transcripts only; the LLM extracts them on first view)
    for call in (c for c in DEMO_CALLS if c["preload"]):
        t = datetime.now() - timedelta(minutes=call["starts_min_ago"])
        cid = con.execute("INSERT INTO calls(flight_id, title, source, status, started, demo_id) VALUES ('EK812', ?, 'recorded', 'ENDED', ?, ?)",
                          (call["title"], t.strftime("%H:%M:%S"), call["id"])).lastrowid
        for n, (who, role, text) in enumerate(call["lines"], 1):
            con.execute("INSERT INTO call_lines(call_id, n, at, speaker, role, text) VALUES (?,?,?,?,?,?)",
                        (cid, n, t.strftime("%H:%M:%S"), who, role, resolve(text, t)))
            t += timedelta(seconds=rng.randint(40, 75))
        con.execute("UPDATE calls SET ended = (SELECT MAX(at) FROM call_lines WHERE call_id = ?) WHERE id = ?", (cid, cid))
    con.commit()
    con.close()


if __name__ == "__main__":
    build()
    print("Built aeroturn.sqlite: 11 inbound flights; EK812 +75 min with 280 connecting pax, 45 at risk")
