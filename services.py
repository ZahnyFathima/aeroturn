"""Tool layer: deterministic Python services. The LLM never calls these; it only proposes actions.
Every write goes through _apply(), which enforces the guards, inside one transaction."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing

from dotenv import load_dotenv

import guards as G
from guards import GuardViolation

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # project .env only
# Vercel's filesystem is read-only except /tmp, so the demo database lives there unless AEROTURN_DB says otherwise.
DB = os.getenv("AEROTURN_DB") or "aeroturn.sqlite"
if os.getenv("VERCEL") and not DB.startswith("/tmp/"):
    DB = "/tmp/" + os.path.basename(DB)
MCT, MAX = G.LIMITS["MCT_MIN"], G.LIMITS["MAX_FDP_MIN"]
F_SQL = "SELECT f.*, f.t_min + f.delay_min AS eff FROM flights f"
KINDS = ("DELAY", "GATE_CHANGE", "CREW_SWAP", "CREW_AUTOSWAP", "REBOOK", "REBOOK_AT_RISK", "REBOOK_TIERS", "ASSIGN_ENGINEER", "DEPARTURE_RELEASE")
STATE_TABLES = ("flights", "seats", "bags", "crew", "maintenance", "gates", "tasks")  # what audit replay must reproduce
_pin_failures = 0


# ---------- helpers ----------
def connect(db=None):
    con = sqlite3.connect(db or DB, isolation_level=None, timeout=10)  # autocommit; transactions are explicit
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args)]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def audit(con, actor, action, detail):
    con.execute("INSERT INTO audit(actor, action, detail) VALUES (?,?,?)", (str(actor)[:40], action, json.dumps(detail, default=str)[:8000]))


def _seat_map(con):
    return dict(con.execute("SELECT pax_id, flight_id FROM seats WHERE pax_id IS NOT NULL").fetchall())


def _moved(con, before):
    """Passengers whose seat moved to another flight in this transaction (recorded in the audit log as proof of rebooking)."""
    return sorted(p for p, f in _seat_map(con).items() if p in before and before[p] != f)


def state_hash(con):
    """SHA-256 over every operational table, so audit replay can prove it reproduced the exact state."""
    h = hashlib.sha256()
    for t in STATE_TABLES:
        for r in con.execute(f"SELECT * FROM {t} ORDER BY 1, 2"):
            h.update(json.dumps(list(r), default=str).encode())
    return h.hexdigest()[:16]


def _get(con, table, key, label):
    r = one(con, (F_SQL if table == "flights" else f"SELECT * FROM {table}") + " WHERE id = ?", (str(key),))
    if not r:
        raise GuardViolation(f"Unknown {label} {key!r}")
    return r


def _inbound(con, fid):
    f = _get(con, "flights", fid, "flight")
    if f["kind"] != "INBOUND":
        raise GuardViolation(f"{fid} is not an inbound flight")
    return f


def _rotation(con, inb):
    return rows(con, "SELECT * FROM crew WHERE flight_id = ? AND status = 'ON_DUTY' ORDER BY id", (inb["id"],))


def _at_risk(con, inb):
    return G.priority_order(rows(con, """
        SELECT p.id, p.name, p.tier, p.dest, s.flight_id, s.seat_no, f.t_min + f.delay_min AS eff
        FROM passengers p JOIN seats s ON s.pax_id = p.id JOIN flights f ON f.id = s.flight_id
        WHERE p.inbound = ? AND f.t_min + f.delay_min - ? < ?""", (inb["id"], inb["eff"], MCT)))


def _integrity(con):
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {"double_bookings": q("SELECT COUNT(*) FROM (SELECT pax_id FROM seats WHERE pax_id IS NOT NULL GROUP BY pax_id HAVING COUNT(*) > 1)"),
            "orphan_bags": q("SELECT COUNT(*) FROM bags b LEFT JOIN seats s ON s.pax_id = b.pax_id "
                             "WHERE s.flight_id IS NULL OR s.flight_id <> b.flight_id")}


def _standby_options(con, member, out):
    legal = []
    for s in rows(con, "SELECT * FROM crew WHERE status = 'STANDBY' AND role = ? ORDER BY walk_min", (member["role"],)):
        try:
            G.check_standby(s, member, out)
            legal.append(s)
        except GuardViolation:
            pass
    return legal


def _alternatives(con, inb):
    """Onward flights still reachable (>= MCT after on-block) with free seats."""
    return rows(con, f"SELECT id, dest, eff, (SELECT COUNT(*) FROM seats s WHERE s.flight_id = x.id AND s.pax_id IS NULL) AS free "
                     f"FROM ({F_SQL}) x WHERE kind = 'ONWARD' AND eff - ? >= ? AND free > 0 ORDER BY eff", (inb["eff"], MCT))


# ---------- stretch: critical path + EU261 cost ----------
def _critical_path(con, inb):
    tasks = {t["id"]: t for t in rows(con, "SELECT * FROM tasks WHERE flight_id = ?", (inb["id"],))}
    finish, prev = {}, {}

    def ef(t):
        if t not in finish:
            deps = [d for d in tasks[t]["deps"].split(",") if d]
            prev[t] = max(deps, key=ef, default=None)
            finish[t] = (ef(prev[t]) if prev[t] else 0) + (0 if tasks[t]["status"] == "DONE" else tasks[t]["minutes"])
        return finish[t]

    end = max(tasks, key=ef)
    path, node = [], end
    while node:
        path.insert(0, node)
        node = prev[node]
    start = max(inb["eff"], 0)
    return {"tasks": sorted(({**t, "finish": start + finish[k], "critical": k in path} for k, t in tasks.items()), key=lambda t: t["finish"]),
            "path": path, "off_block": start + finish[end]}


def _eu261(km):
    return 250 if km <= 1500 else 400 if km <= 3500 else 600


def _delay_cost(con, inb, care_overnight=180, meal=30):
    """Baseline = do nothing (every tight connection misses). Current = the state now in the database."""
    pax = rows(con, """SELECT o.t_min AS o_dep, o.distance_km AS km, f.t_min + f.delay_min AS now_dep FROM passengers p
        JOIN flights o ON o.id = p.orig_flight JOIN seats s ON s.pax_id = p.id JOIN flights f ON f.id = s.flight_id
        WHERE p.inbound = ? AND o.t_min - ? < ?""", (inb["id"], inb["eff"], MCT))
    base = cur = protected = within_3h = 0
    for p in pax:
        worst = _eu261(p["km"]) + care_overnight
        base += worst
        if p["now_dep"] - inb["eff"] < MCT:
            cur += worst
            continue
        late = p["now_dep"] - p["o_dep"]
        protected, within_3h = protected + 1, within_3h + (late < 180)
        cur += (_eu261(p["km"]) if late >= 180 else 0) + (meal if late >= 120 else 0)
    return {"baseline_at_risk": len(pax), "protected": protected, "within_3h": within_3h, "baseline_eur": base, "pax_cost_eur": cur}


def _kpis(con, fid):
    inb = _inbound(con, fid)
    out, crew, risk = _get(con, "flights", inb["next_leg"], "flight"), _rotation(con, inb), _at_risk(con, inb)
    margins = [MAX - G.projected_fdp(c, out) for c in crew]
    cp = _critical_path(con, inb)
    return {"eta": inb["eff"], "delay_min": inb["delay_min"],
            "connecting": con.execute("SELECT COUNT(*) FROM passengers WHERE inbound = ?", (fid,)).fetchone()[0],
            "at_risk": len(risk), "vulnerable_at_risk": sum(p["tier"] in G.VULNERABLE for p in risk),
            "crew_min_margin": min(margins, default=0), "crew_illegal": sum(m < 0 for m in margins),
            "open_nogo": con.execute("SELECT COUNT(*) FROM maintenance WHERE tail = ? AND severity = 'NO-GO' AND status <> 'SIGNED'",
                                     (inb["tail"],)).fetchone()[0],
            "off_block": cp["off_block"], "outbound_dep": out["eff"], "turn_gap": cp["off_block"] - out["eff"],
            **_bag_kpis(con, inb), **_delay_cost(con, inb)}


def _remote(con, inb):
    out = _get(con, "flights", inb["next_leg"], "flight")
    g = one(con, "SELECT kind FROM gates WHERE id = ?", (out["gate"] or "",))
    return bool(g and g["kind"] == "REMOTE")


def _bag_kpis(con, inb):
    """Bags that will not make their passenger's flight (pax connection >= MCT but < bag MCT), plus rush-tagged bags."""
    need = G.bag_mct(_remote(con, inb))
    r = one(con, """SELECT COUNT(*) AS bags,
        SUM(CASE WHEN f.t_min + f.delay_min - ? < ? THEN 1 ELSE 0 END) AS short,
        SUM(CASE WHEN b.status = 'RUSH' THEN 1 ELSE 0 END) AS rush
        FROM bags b JOIN passengers p ON p.id = b.pax_id JOIN flights f ON f.id = b.flight_id WHERE p.inbound = ?""",
            (inb["eff"], need, inb["id"]))
    return {"bags": r["bags"] or 0, "bags_short_connection": r["short"] or 0, "bags_rush": r["rush"] or 0, "bag_mct": need}


# ---------- required tool 1: read state ----------
def list_flights():
    with closing(connect()) as con:
        return [{**{k: f[k] for k in ("id", "origin", "lat", "lon", "eff", "delay_min", "block_min", "status", "next_leg", "aircraft_type")},
                 **_kpis(con, f["id"])} for f in rows(con, F_SQL + " WHERE kind = 'INBOUND' ORDER BY eff")]


def get_turnaround_and_crew_state(fid):
    with closing(connect()) as con:
        inb = _inbound(con, fid)
        out = _get(con, "flights", inb["next_leg"], "flight")
        crew = [{**c, "projected_fdp": G.projected_fdp(c, out), "margin": MAX - G.projected_fdp(c, out),
                 "options": [{k: s[k] for k in ("id", "name", "location", "walk_min")} for s in _standby_options(con, c, out)]}
                for c in _rotation(con, inb)]
        risk, alts = {p["id"] for p in _at_risk(con, inb)}, _alternatives(con, inb)
        pax = rows(con, """SELECT p.id, p.name, p.tier, p.dest, p.orig_flight, s.flight_id AS onward, s.seat_no, f.t_min + f.delay_min - ? AS conn_min,
            (SELECT COUNT(*) FROM bags b WHERE b.pax_id = p.id) AS bags,
            (SELECT GROUP_CONCAT(DISTINCT b.status) FROM bags b WHERE b.pax_id = p.id) AS bag_status FROM passengers p JOIN seats s ON s.pax_id = p.id
            JOIN flights f ON f.id = s.flight_id WHERE p.inbound = ?""", (inb["eff"], fid))
        need = G.bag_mct(_remote(con, inb))
        pax = [{**p, "at_risk": p["id"] in risk, "bag_ok": not p["bags"] or p["conn_min"] >= need,
                "options": [{**a, "bag_ok": a["eff"] - inb["eff"] >= need} for a in alts if a["dest"] == p["dest"]] if p["id"] in risk else []}
               for p in pax]
        pax.sort(key=lambda p: (not p["at_risk"], G.TIER_RANK.get(p["tier"], 9), p["conn_min"]))
        return {
            "flight": inb, "outbound": out, "kpis": _kpis(con, fid),
            "crew": sorted(crew, key=lambda c: c["margin"]), "passengers": pax,
            "maintenance": rows(con, "SELECT m.*, e.name AS engineer FROM maintenance m LEFT JOIN engineers e ON e.id = m.engineer_id "
                                     "WHERE tail = ?", (inb["tail"],)),
            "engineers": rows(con, "SELECT id, name, location FROM engineers"),
            "gates": rows(con, "SELECT * FROM gates"),
            "turnaround": _critical_path(con, inb),
            "comms": rows(con, "SELECT * FROM comms WHERE flight_id = ? ORDER BY id DESC", (fid,)),
            "decisions": rows(con, "SELECT * FROM changes WHERE flight_id = ? ORDER BY id DESC", (fid,)),
            "notifications": rows(con, "SELECT * FROM notifications WHERE flight_id = ? ORDER BY id DESC", (fid,)),
            "integrity": _integrity(con), "limits": dict(G.LIMITS),
        }


# ---------- atomic rebooking ----------
def _plan_rebooking(con, inb, tiers=None):
    """Greedy, baggage-aware seat allocation, vulnerable flyers first: the earliest same-destination flight that the
    passenger AND their checked bags can both make; if only the passenger can make it, take it and rush-tag the bags."""
    free = rows(con, f"SELECT s.flight_id, s.seat_no, x.dest, x.eff FROM seats s JOIN ({F_SQL}) x ON x.id = s.flight_id "
                     "WHERE s.pax_id IS NULL AND x.kind = 'ONWARD' AND x.eff - ? >= ? ORDER BY x.eff, s.rowid", (inb["eff"], MCT))
    need = G.bag_mct(_remote(con, inb))
    plan = []
    for p in _at_risk(con, inb):
        if tiers is not None and p["tier"] not in tiers:
            continue
        has_bags = con.execute("SELECT COUNT(*) FROM bags WHERE pax_id = ?", (p["id"],)).fetchone()[0] > 0
        same = [s for s in free if s["dest"] == p["dest"]]
        s = next((s for s in same if not has_bags or s["eff"] - inb["eff"] >= need), same[0] if same else None)
        if s:
            free.remove(s)
            plan.append({"pax_id": p["id"], "to": s["flight_id"], "seat": s["seat_no"]})
    return plan


def _rebook(con, inb, assignments, chaos=False):
    """Seat + bag moves. Must run inside a transaction; raising makes the caller roll everything back.
    chaos: True/"seat" = a competing channel grabs our seat mid-batch; "bag" = the baggage system rejects a re-tag mid-batch."""
    chaos = "seat" if chaos is True else chaos
    need = G.bag_mct(_remote(con, inb))
    at_risk = {p["id"]: p for p in _at_risk(con, inb)}
    ids = [a.get("pax_id") for a in assignments]
    if len(set(ids)) != len(ids) or not set(ids) <= at_risk.keys():
        raise GuardViolation("Rebooking may only move distinct, currently at-risk passengers")
    G.check_vulnerable_protection(assignments, list(at_risk.values()))
    batch = G.priority_order([{**at_risk[a["pax_id"]], "to": str(a["to"]), "seat": a.get("seat")} for a in assignments])
    for i, a in enumerate(batch, 1):
        f = _get(con, "flights", a["to"], "flight")
        if f["kind"] != "ONWARD" or f["dest"] != a["dest"] or f["eff"] - inb["eff"] < MCT:
            raise GuardViolation(f"{a['to']} is not a valid connection for {a['id']}")
        con.execute("UPDATE seats SET pax_id = NULL WHERE pax_id = ?", (a["id"],))
        seat = a["seat"] or (one(con, "SELECT seat_no FROM seats WHERE flight_id = ? AND pax_id IS NULL ORDER BY rowid", (a["to"],)) or {}).get("seat_no")
        if chaos == "seat" and i == (len(batch) + 1) // 2:  # demo: a competing channel grabs our seat mid-batch
            con.execute("UPDATE seats SET pax_id = 'P-EXT' WHERE flight_id = ? AND seat_no = ?", (a["to"], seat))
        if not seat or con.execute("UPDATE seats SET pax_id = ? WHERE flight_id = ? AND seat_no = ? AND pax_id IS NULL",
                                   (a["id"], a["to"], seat)).rowcount != 1:
            raise RuntimeError(f"Seat conflict at step {i}/{len(batch)} ({a['to']} {seat}); {i - 1} completed moves rolled back")
        if chaos == "bag" and i >= (len(batch) + 1) // 2 and con.execute("SELECT 1 FROM bags WHERE pax_id = ?", (a["id"],)).fetchone():
            raise RuntimeError(f"Baggage system rejected re-tag at step {i}/{len(batch)} ({a['id']}): seat moved but bag not, "
                               f"so the whole batch is rolled back to avoid an orphaned bag")
        con.execute("UPDATE bags SET flight_id = ?, status = ? WHERE pax_id = ?",
                    (a["to"], "TRANSFER" if f["eff"] - inb["eff"] >= need else "RUSH", a["id"]))
    G.check_bag_match(con)
    if any(_integrity(con).values()):
        raise RuntimeError("Post-batch integrity check failed")
    return [(f"Transfer desk & baggage hall", f"{len(batch)} passenger(s) off {inb['id']} rebooked, bags re-tagged")] if batch else []


def execute_atomic_passenger_rebook(fid, assignments=None, chaos=False, actor="OCC"):
    """Required tool 2: all-or-nothing seat + bag move for a batch (default: the code-allocated plan)."""
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            inb = _inbound(con, fid)
            seats_before = _seat_map(con)
            notes = _rebook(con, inb, _plan_rebooking(con, inb) if assignments is None else assignments, chaos)
            audit(con, actor, "REBOOK_COMMIT", {"flight": fid, "assignments": assignments, "rebooked_pax": _moved(con, seats_before),
                                                "state_hash": state_hash(con)})
            con.execute("DELETE FROM reports WHERE flight_id = ?", (fid,))
            con.execute("COMMIT")
        except Exception as e:  # noqa: BLE001 - any failure must roll back
            con.execute("ROLLBACK")
            audit(con, actor, "REBOOK_ROLLBACK", {"flight": fid, "error": str(e), "state_hash": state_hash(con)})
            return {"ok": False, "error": str(e), "integrity": _integrity(con)}
        return {"ok": True, "notes": notes, "integrity": _integrity(con)}


# ---------- every state change goes through here ----------
def _swap(con, member, standby, inb):
    con.execute("UPDATE crew SET status = 'RELEASED' WHERE id = ?", (member["id"],))
    con.execute("UPDATE crew SET status = 'ON_DUTY', flight_id = ? WHERE id = ?", (inb["id"], standby["id"]))
    return [(f"{member['name']} ({member['role']})", f"Released from {inb['next_leg']}, go to rest"),
            (f"{standby['name']} ({standby['role']})", f"Call-out: report for {inb['next_leg']} at {standby['location']}")]


def _apply(con, fid, a, chaos=False):
    """Apply ONE action with its guards. Returns [(recipient, message)] notifications."""
    inb = _inbound(con, fid)
    out = _get(con, "flights", inb["next_leg"], "flight")
    k = a.get("kind")
    if k in ("CREW_AUTOSWAP", "REBOOK_AT_RISK", "REBOOK_TIERS") and a.get("flight_id") not in (None, fid):
        raise GuardViolation(f"{k} applies to the inbound flight {fid}, not {a.get('flight_id')}")
    if k == "DEPARTURE_RELEASE" and a.get("flight_id") not in (None, out["id"]):
        raise GuardViolation(f"DEPARTURE_RELEASE applies to {out['id']}, not {a.get('flight_id')}")
    if k == "DELAY":
        f, m = _get(con, "flights", a.get("flight_id"), "flight"), a.get("minutes")
        if f["kind"] == "INBOUND":
            raise GuardViolation("An airborne inbound flight cannot be retimed")
        if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 240:
            raise GuardViolation("Delay must be 1-240 minutes")
        G.check_onward_hold(f, m)
        con.execute("UPDATE flights SET delay_min = delay_min + ? WHERE id = ?", (m, f["id"]))
        return [(f"Gate agents & crew {f['id']}", f"{f['id']} retimed +{m} min"), ("Network control", f"{f['id']} delayed {m} min")]
    if k == "GATE_CHANGE":
        f, g = _get(con, "flights", a.get("flight_id"), "flight"), _get(con, "gates", a.get("gate"), "gate")
        if g["occupied_by"] not in (None, f["id"]):
            raise GuardViolation(f"Gate {g['id']} is occupied by {g['occupied_by']}")
        con.execute("UPDATE gates SET occupied_by = NULL WHERE occupied_by = ?", (f["id"],))
        con.execute("UPDATE gates SET occupied_by = ? WHERE id = ?", (f["id"], g["id"]))
        con.execute("UPDATE flights SET gate = ? WHERE id = ?", (g["id"], f["id"]))
        return [("Ramp control", f"{f['id']} moves to {g['id']}"), (f"Gate agents {g['id']}", f"{f['id']} now at {g['id']}"),
                ("PRM assistance", f"{f['id']} boarding from {g['id']} ({g['kind'].lower()})")]
    if k == "CREW_SWAP":
        member, standby = _get(con, "crew", a.get("crew_id"), "crew member"), _get(con, "crew", a.get("standby_id"), "crew member")
        if member["flight_id"] != fid or member["status"] != "ON_DUTY":
            raise GuardViolation(f"{member['id']} is not on duty for {fid}")
        G.check_standby(standby, member, out)
        return _swap(con, member, standby, inb)
    if k == "CREW_AUTOSWAP":  # stretch: standby pairing matcher, closest legal crew first
        notes = []
        for m in _rotation(con, inb):
            if G.projected_fdp(m, out) > MAX or m["type_rating"] != out["aircraft_type"]:
                options = _standby_options(con, m, out)
                if not options:
                    raise GuardViolation(f"No legal standby {m['role']} for {out['id']}")
                notes += _swap(con, m, options[0], inb)
        return notes
    if k == "REBOOK":
        return _rebook(con, inb, [{"pax_id": a.get("pax_id"), "to": a.get("to_flight")}], chaos)
    if k == "REBOOK_AT_RISK":
        return _rebook(con, inb, _plan_rebooking(con, inb), chaos)
    if k == "REBOOK_TIERS":  # rebook only some tiers; the vulnerable-priority guard refuses it if a vulnerable flyer is skipped
        tiers = a.get("tiers")
        if not isinstance(tiers, list) or not tiers or not set(tiers) <= G.TIER_RANK.keys():
            raise GuardViolation(f"REBOOK_TIERS needs a list of tiers from {sorted(G.TIER_RANK)}")
        return _rebook(con, inb, _plan_rebooking(con, inb, set(tiers)), chaos)
    if k == "ASSIGN_ENGINEER":
        item, eng = _get(con, "maintenance", a.get("item_id"), "maintenance item"), _get(con, "engineers", a.get("engineer_id"), "engineer")
        if item["tail"] != inb["tail"] or item["status"] == "SIGNED":
            raise GuardViolation(f"{item['id']} is not an open item on {inb['tail']}")
        con.execute("UPDATE maintenance SET engineer_id = ?, status = 'ASSIGNED' WHERE id = ?", (eng["id"], item["id"]))
        return [(f"Engineer {eng['name']}", f"Assigned {item['id']}: {item['item']} on {inb['tail']}")]
    if k == "DEPARTURE_RELEASE":
        for m in _rotation(con, inb):
            G.check_crew_duty(m, out)
        G.check_maintenance(rows(con, "SELECT * FROM maintenance WHERE tail = ?", (inb["tail"],)))
        con.execute("UPDATE flights SET status = 'RELEASED' WHERE id = ?", (out["id"],))
        return [(f"Dispatch & captain {out['id']}", f"{out['id']} released for departure")]
    raise GuardViolation(f"Unknown action {k!r}")


def _network_delay(con):
    return con.execute("SELECT COALESCE(SUM(delay_min), 0) FROM flights WHERE kind <> 'INBOUND'").fetchone()[0]


def simulate(fid, actions):
    """Dry run: apply the actions inside a transaction, measure KPIs, then ROLLBACK. Nothing persists."""
    actions = _clean_actions(actions)
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            before = _network_delay(con)
            for a in actions:
                _apply(con, fid, a)
            k = _kpis(con, fid)
            k["network_delay_added"] = _network_delay(con) - before
            k["total_cost_eur"] = k["pax_cost_eur"] + 100 * k["network_delay_added"]  # ~EUR 100 per network delay minute
            return {"ok": True, "reason": "", "kpis": k}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e), "kpis": None}
        finally:
            con.execute("ROLLBACK")


def rank_scenarios(scenarios, prio):
    """Code (not the LLM) scores scenarios against the controller's priority toggles."""
    def score(s):
        if not s["ok"]:
            return -1e9
        k = s["kpis"]
        return (-10 * k["at_risk"] * bool(prio.get("connections")) - k["network_delay_added"] * bool(prio.get("on_time"))
                - k["total_cost_eur"] / 100 * bool(prio.get("cost")) + min(k["crew_min_margin"], 300) / 10 * bool(prio.get("crew_rest"))
                - 50 * k["vulnerable_at_risk"]  # vulnerable protection is always on
                - 0.01 * k["network_delay_added"] - 0.0001 * k["total_cost_eur"])  # tie-breakers
    for s in scenarios:
        s["score"] = round(score(s), 1)
    return sorted(scenarios, key=lambda s: -s["score"])


# ---------- required tool 3 + HITL ----------
def _clean_actions(actions):
    if not isinstance(actions, list) or not 0 < len(actions) <= 20 or not all(isinstance(a, dict) for a in actions):
        raise ValueError("actions must be a list of 1-20 objects")
    keep = ("kind", "flight_id", "minutes", "gate", "crew_id", "standby_id", "pax_id", "to_flight", "item_id", "engineer_id", "tiers")
    actions = [{k: v for k, v in a.items() if k in keep} for a in actions]
    return sorted(actions, key=lambda a: KINDS.index(a["kind"]) if a.get("kind") in KINDS else 99)  # holds first, release last


def stage_controller_schedule_change(fid, title, actions, proposed_by="OCC"):
    """Guard-check via dry run, then park as PENDING. Nothing changes until a controller approves."""
    actions = _clean_actions(actions)
    sim = simulate(fid, actions)
    status = "PENDING" if sim["ok"] else "BLOCKED"
    with closing(connect()) as con:
        cid = con.execute("INSERT INTO changes(flight_id, title, actions, status, reason, kpis, proposed_by) VALUES (?,?,?,?,?,?,?)",
                          (fid, str(title)[:120], json.dumps(actions), status, sim["reason"], json.dumps(sim["kpis"]),
                           str(proposed_by)[:40])).lastrowid
        audit(con, proposed_by, f"STAGE_{status}", {"id": cid, "actions": actions, "reason": sim["reason"]})
    return {"id": cid, "status": status, "reason": sim["reason"]}


def _check_pin(controller, pin):
    global _pin_failures
    expected, controller = os.getenv("OCC_CONTROLLER_PIN", ""), str(controller or "").strip()[:40]
    if _pin_failures >= 5:
        raise PermissionError("Approvals locked after 5 failed PIN attempts; restart the server")
    if not expected or not hmac.compare_digest(str(pin).encode(), expected.encode()):
        _pin_failures += 1
        raise PermissionError("Invalid controller PIN (or OCC_CONTROLLER_PIN not configured)")
    if not controller:
        raise PermissionError("Controller name required")
    _pin_failures = 0
    return controller


def approve_change(cid, controller, pin, approve=True, chaos=False):
    """The ONLY path that commits a staged change. All actions apply atomically, or none do."""
    controller = _check_pin(controller, pin)
    with closing(connect()) as con:
        c = one(con, "SELECT * FROM changes WHERE id = ? AND status = 'PENDING'", (int(cid),))
        if not c:
            raise ValueError(f"No pending change #{cid}")
        con.execute("BEGIN IMMEDIATE")
        try:
            seats_before = _seat_map(con)
            notes = [n for a in json.loads(c["actions"]) for n in _apply(con, c["flight_id"], a, chaos)] if approve else []
            con.executemany("INSERT INTO notifications(change_id, flight_id, recipient, message) VALUES (?,?,?,?)",
                            [(cid, c["flight_id"], r, m) for r, m in notes])
            status, reason = ("APPROVED" if approve else "REJECTED"), ""
            con.execute("UPDATE changes SET status = ?, decided_by = ? WHERE id = ?", (status, controller, cid))
            audit(con, controller, f"CHANGE_{status}", {"id": cid, "flight": c["flight_id"], "title": c["title"],
                                                         "actions": json.loads(c["actions"]) if approve else [], "chaos": chaos,
                                                         "rebooked_pax": _moved(con, seats_before), "state_hash": state_hash(con)})
            con.execute("DELETE FROM reports")  # every live report must be regenerated from the new state
            con.execute("COMMIT")
        except Exception as e:  # noqa: BLE001 - any failure rolls back every action in the decision
            con.execute("ROLLBACK")
            status, reason, notes = "ROLLED_BACK", str(e), []
            con.execute("UPDATE changes SET status = ?, reason = ?, decided_by = ? WHERE id = ?", (status, reason, controller, cid))
            audit(con, controller, "CHANGE_ROLLED_BACK", {"id": cid, "flight": c["flight_id"], "title": c["title"], "reason": reason,
                                                          "state_hash": state_hash(con)})
        return {"id": cid, "status": status, "reason": reason, "notified": len(notes), "integrity": _integrity(con)}


def decide(fid, title, actions, controller, pin, chaos=False):
    """Controller picks an option: PIN check, stage (guard dry run), then approve (atomic apply) in one step."""
    controller = _check_pin(controller, pin)
    r = stage_controller_schedule_change(fid, title, actions, controller)
    return approve_change(r["id"], controller, pin, True, chaos) if r["status"] == "PENDING" else r


# ---------- engineer sign-off, calls, ramp updates ----------
def sign_off_maintenance(item_id, licence):
    """Only the assigned, licensed engineer can close an item (licence must match). Not available to the LLM."""
    with closing(connect()) as con:
        item = _get(con, "maintenance", item_id, "maintenance item")
        eng = one(con, "SELECT * FROM engineers WHERE id = ?", (item["engineer_id"] or "",))
        if not eng or item["status"] != "ASSIGNED":
            raise PermissionError(f"{item_id} must be assigned to an engineer first")
        if not re.fullmatch(r"B1-\d{4,6}", str(licence).strip().upper()) or not hmac.compare_digest(str(licence).strip().upper(), eng["licence"]):
            raise PermissionError(f"Licence does not match the assigned engineer {eng['name']}")
        con.execute("BEGIN IMMEDIATE")
        fid = _sign(con, item, eng)
        cid = con.execute("INSERT INTO changes(flight_id, title, actions, status, proposed_by, decided_by) VALUES (?,?,?,?,?,?)",
                          (fid, f"Maintenance sign-off {item_id}", "[]", "APPROVED", eng["name"], eng["name"])).lastrowid
        con.execute("INSERT INTO notifications(change_id, flight_id, recipient, message) VALUES (?,?,?,?)",
                    (cid, fid, "OCC duty manager & captain", f"{item_id} ({item['item']}) signed off by {eng['name']}"))
        con.execute("DELETE FROM reports WHERE flight_id = ?", (fid,))
        audit(con, eng["name"], "MAINTENANCE_SIGNED", {"item": item_id, "state_hash": state_hash(con)})
        con.execute("COMMIT")
        return {"status": "SIGNED", "item": item_id}


# mock walking time from each engineer base to the stands (minutes); a real system would read this from the roster/positioning feed
ENGINEER_WALK = {"Line ops T1": 8, "Line ops T2": 12, "Hangar 3": 25}
MX_WORK_MIN = 40  # default inspection time when the turnaround has no MX_CHECK task


def maintenance_queue():
    """Fleet-wide maintenance queue for the Maintenance page. All timing is deterministic code:
    an engineer can start once they have walked to the aircraft AND it is on block; the fix ends start + inspection time;
    slack = scheduled departure - that finish (negative = the release will slip)."""
    with closing(connect()) as con:
        engineers = rows(con, "SELECT id, name, licence, location FROM engineers ORDER BY id")
        busy = {r["engineer_id"]: r["id"] for r in rows(con, "SELECT id, engineer_id FROM maintenance WHERE status = 'ASSIGNED'")}
        history = {}
        for e in rows(con, "SELECT * FROM audit WHERE action IN ('CHANGE_APPROVED', 'MAINTENANCE_SIGNED') ORDER BY id"):
            d = json.loads(e["detail"] or "{}")
            if e["action"] == "MAINTENANCE_SIGNED":
                history.setdefault(d.get("item"), []).append({"ts": e["ts"], "who": e["actor"], "what": "Signed off"})
            for a in d.get("actions", []):
                if a.get("kind") == "ASSIGN_ENGINEER":
                    name = next((x["name"] for x in engineers if x["id"] == a.get("engineer_id")), a.get("engineer_id"))
                    history.setdefault(a.get("item_id"), []).append({"ts": e["ts"], "who": e["actor"], "what": f"Assigned to {name}"})
        items = []
        for m in rows(con, "SELECT m.*, e.name AS engineer FROM maintenance m LEFT JOIN engineers e ON e.id = m.engineer_id ORDER BY m.id"):
            inb = one(con, F_SQL + " WHERE kind = 'INBOUND' AND tail = ?", (m["tail"],))
            out = _get(con, "flights", inb["next_leg"], "flight") if inb and inb["next_leg"] else None
            task = one(con, "SELECT minutes, status FROM tasks WHERE flight_id = ? AND id = 'MX_CHECK'", (inb["id"],)) if inb else None
            work = task["minutes"] if task else MX_WORK_MIN
            on_block = max(inb["eff"], 0) if inb else 0
            blocks = m["severity"] == "NO-GO" and m["status"] != "SIGNED"

            def plan(e):
                walk = ENGINEER_WALK.get(e["location"], 20)
                start = max(on_block, walk)
                finish = start + work
                return {**e, "walk_min": walk, "busy_with": busy.get(e["id"]) if busy.get(e["id"]) != m["id"] else None,
                        "start": start, "finish": finish, "slack": (out["eff"] - finish) if out else None}
            options = sorted((plan(e) for e in engineers), key=lambda o: (o["busy_with"] is not None, o["finish"], o["walk_min"]))
            cur = next((o for o in options if o["id"] == m["engineer_id"]), None)  # computed before options are hidden for non-open items
            items.append({**m, "inbound": inb["id"] if inb else None, "outbound": out["id"] if out else None,
                          "on_block": on_block, "dep": out["eff"] if out else None, "dep_status": out["status"] if out else None,
                          "minutes_left": out["eff"] if out else None, "work_min": work, "blocks_departure": blocks,
                          "options": options if m["status"] == "OPEN" else [], "assigned": cur, "history": history.get(m["id"], [])})
        sev = {"NO-GO": 0, "DEFERRABLE": 1}
        items.sort(key=lambda i: (i["status"] == "SIGNED", sev.get(i["severity"], 2), i["dep"] if i["dep"] is not None else 9999))
        return {"items": items, "now": 0, "limits": {"work_default": MX_WORK_MIN}}


def _sign(con, item, eng):
    """State effect of a sign-off (shared with audit replay)."""
    fid = one(con, "SELECT id FROM flights WHERE kind = 'INBOUND' AND tail = ?", (item["tail"],))["id"]
    con.execute("UPDATE maintenance SET status = 'SIGNED', signed_by = ? WHERE id = ?", (eng["name"], item["id"]))
    if not con.execute("SELECT 1 FROM maintenance WHERE tail = ? AND severity = 'NO-GO' AND status <> 'SIGNED'", (item["tail"],)).fetchone():
        con.execute("UPDATE tasks SET status = 'DONE' WHERE flight_id = ? AND id = 'MX_CHECK'", (fid,))
    return fid


def log_call(fid, speaker, text):
    text, speaker = str(text or "").strip()[:2000], str(speaker or "OCC call").strip()[:40]
    if not text:
        raise ValueError("Transcript is empty")
    with closing(connect()) as con:
        _inbound(con, fid)
        con.execute("INSERT INTO comms(flight_id, at, speaker, text) VALUES (?, strftime('%H:%M', 'now', 'localtime'), ?, ?)", (fid, speaker, text))
        con.execute("DELETE FROM reports WHERE flight_id = ?", (fid,))
        audit(con, speaker, "CALL_LOGGED", {"flight": fid})
    return {"status": "LOGGED"}


def mark_task_done(fid, task_id):
    if task_id == "MX_CHECK":
        raise PermissionError("The maintenance check closes only via licensed engineer sign-off")
    with closing(connect()) as con:
        if con.execute("UPDATE tasks SET status = 'DONE' WHERE flight_id = ? AND id = ?", (fid, task_id)).rowcount != 1:
            raise ValueError("Unknown task")
        con.execute("DELETE FROM reports WHERE flight_id = ?", (fid,))
        audit(con, "ramp", "TASK_DONE", {"flight": fid, "task": task_id, "state_hash": state_hash(con)})
    return {"status": "DONE"}


def decisions():
    with closing(connect()) as con:
        return {"decisions": rows(con, "SELECT * FROM changes ORDER BY id DESC"),
                "notifications": rows(con, "SELECT * FROM notifications ORDER BY id DESC LIMIT 100")}


def cached_report(fid, data=None):
    with closing(connect()) as con:
        if data is not None:
            con.execute("INSERT OR REPLACE INTO reports(flight_id, data) VALUES (?, ?)", (fid, json.dumps(data)))
        r = one(con, "SELECT * FROM reports WHERE flight_id = ?", (fid,))
        return {**json.loads(r["data"]), "generated": r["ts"]} if r else None


def send_pax_messages(fid, messages, controller, pin):
    """Passenger messages drafted by the LLM go out only after the controller approves them (name + PIN)."""
    controller = _check_pin(controller, pin)
    if not isinstance(messages, list) or not 0 < len(messages) <= 400:
        raise ValueError("messages must be a list of 1-400 {pax_id, message}")
    with closing(connect()) as con:
        _inbound(con, fid)
        known = {r["id"] for r in rows(con, "SELECT id FROM passengers WHERE inbound = ?", (fid,))}
        batch = [(fid, str(m.get("pax_id")), str(m.get("message") or "").strip()[:700], controller) for m in messages
                 if isinstance(m, dict) and m.get("pax_id") in known and str(m.get("message") or "").strip()]
        if not batch:
            raise ValueError("No valid messages for passengers of this flight")
        con.execute("BEGIN IMMEDIATE")
        con.executemany("INSERT INTO pax_messages(flight_id, pax_id, message, approved_by) VALUES (?,?,?,?)", batch)
        audit(con, controller, "PAX_NOTIFIED", {"flight": fid, "count": len(batch)})
        con.execute("COMMIT")
    return {"status": "SENT", "sent": len(batch)}


def preview_passengers(fid, actions):
    """Passengers & bags under one recovery option: apply it inside a transaction that is always rolled back and report
    exactly who would move where, what happens to their bags, and who is still stranded."""
    actions = _clean_actions(actions)
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            inb = _inbound(con, fid)
            before = {p["id"]: p for p in _at_risk(con, inb)}
            try:
                for a in actions:
                    _apply(con, fid, a)
                ok, reason = True, ""
            except Exception as e:  # noqa: BLE001
                ok, reason = False, str(e)
            inb = _inbound(con, fid)
            need = G.bag_mct(_remote(con, inb))
            pax = rows(con, """SELECT p.id, p.name, p.tier, p.dest, p.orig_flight, s.flight_id AS onward, s.seat_no,
                f.t_min + f.delay_min - ? AS conn_min, (SELECT COUNT(*) FROM bags b WHERE b.pax_id = p.id) AS bags,
                (SELECT GROUP_CONCAT(DISTINCT b.status) FROM bags b WHERE b.pax_id = p.id) AS bag_status
                FROM passengers p JOIN seats s ON s.pax_id = p.id JOIN flights f ON f.id = s.flight_id WHERE p.inbound = ?""", (inb["eff"], fid))
            still = {p["id"] for p in _at_risk(con, inb)}
            out = [{**p, "was": before[p["id"]]["flight_id"], "outcome": "STRANDED" if p["id"] in still else
                    "REBOOKED" if p["onward"] != before[p["id"]]["flight_id"] else "MAKES ORIGINAL (hold)",
                    "bag_ok": not p["bags"] or p["conn_min"] >= need} for p in pax if p["id"] in before]
            out = G.priority_order(out)
            k = _kpis(con, fid) if ok else None
            return {"ok": ok, "reason": reason, "bag_mct": need, "passengers": out, "kpis": k,
                    "summary": {"at_risk_before": len(before), "rebooked": sum(p["outcome"] == "REBOOKED" for p in out),
                                "kept_by_hold": sum(p["outcome"].startswith("MAKES") for p in out),
                                "stranded": sum(p["outcome"] == "STRANDED" for p in out),
                                "vulnerable_stranded": sum(p["outcome"] == "STRANDED" and p["tier"] in G.VULNERABLE for p in out),
                                "bags_moving": sum(p["bags"] for p in out if p["outcome"] == "REBOOKED"),
                                "bags_rush": sum(p["bags"] for p in out if "RUSH" in (p["bag_status"] or "")),
                                "bags_short": sum(p["bags"] for p in out if p["bags"] and not p["bag_ok"] and p["outcome"] != "STRANDED")}}
        finally:
            con.execute("ROLLBACK")
