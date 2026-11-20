"""Audit replay: rebuild the seed dataset in a scratch database, re-apply every committed event from the append-only audit
log in order, and check after each step that the state hash equals the hash recorded at commit time. Proves the audit log is a
complete, faithful record of how the live state came to be (and that rollbacks really left no trace)."""
import json
import os
import tempfile
from contextlib import closing

import seed
import services as S

STATE_EVENTS = ("REBOOK_COMMIT", "CHANGE_APPROVED", "MAINTENANCE_SIGNED", "TASK_DONE")


def _kp(con, fid):
    k = S._kpis(con, fid)
    out = S._get(con, "flights", S._inbound(con, fid)["next_leg"], "flight")
    return {"at_risk": k["at_risk"], "vulnerable_at_risk": k["vulnerable_at_risk"], "crew_illegal": k["crew_illegal"],
            "open_nogo": k["open_nogo"], "outbound_gate": out["gate"], "outbound_status": out["status"],
            "outbound_delay": out["delay_min"], **S._integrity(con)}


KEYS = {"seats": ("flight_id", "seat_no"), "tasks": ("flight_id", "id")}  # every other state table is keyed by id


def _snapshot(con):
    snap = {}
    for t in S.STATE_TABLES:
        cols = KEYS.get(t, ("id",))
        snap[t] = {" / ".join(str(r[c]) for c in cols): r for r in S.rows(con, f"SELECT * FROM {t}")}
    return snap


def _diff(a, b, limit=150):
    """Field-level changes between two snapshots: exactly which records the event touched."""
    out, total = [], 0
    for t in S.STATE_TABLES:
        for k in sorted(a[t].keys() | b[t].keys()):
            ra, rb = a[t].get(k), b[t].get(k)
            if ra == rb:
                continue
            for f in sorted((ra or rb).keys()):
                va, vb = (ra or {}).get(f), (rb or {}).get(f)
                if va != vb:
                    total += 1
                    if len(out) < limit:
                        out.append({"table": t, "key": k, "field": f, "before": va, "after": vb})
    return out, total


def _redo(con, ev, d):
    con.execute("BEGIN IMMEDIATE")
    try:
        if ev == "REBOOK_COMMIT":
            inb = S._inbound(con, d["flight"])
            S._rebook(con, inb, S._plan_rebooking(con, inb) if d.get("assignments") is None else d["assignments"])
        elif ev == "CHANGE_APPROVED":
            for a in d.get("actions", []):
                S._apply(con, d["flight"], a, d.get("chaos", False))
        elif ev == "MAINTENANCE_SIGNED":
            item = S._get(con, "maintenance", d["item"], "maintenance item")
            S._sign(con, item, S.one(con, "SELECT * FROM engineers WHERE id = ?", (item["engineer_id"],)))
        elif ev == "TASK_DONE":
            con.execute("UPDATE tasks SET status = 'DONE' WHERE flight_id = ? AND id = ?", (d["flight"], d["task"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def run(fid="EK812"):
    with closing(S.connect()) as live:
        events = S.rows(live, "SELECT * FROM audit ORDER BY id")
        live_hash = S.state_hash(live)
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="aeroturn-replay-")
    os.close(fd)
    try:
        seed.build(path)
        steps, mismatches = [], 0
        with closing(S.connect(path)) as con:
            initial = S.state_hash(con)
            steps.append({"seq": 0, "ts": "", "actor": "seed", "action": "SEED", "detail": {}, "state_change": True,
                          "hash": initial, "recorded_hash": None, "match": None, "records": [], "records_total": 0,
                          "kpis": _kp(con, fid)})
            for e in events:
                d = json.loads(e["detail"] or "{}")
                change = e["action"] in STATE_EVENTS
                err, records, n_records = None, [], 0
                if change:
                    before = _snapshot(con)
                    try:
                        _redo(con, e["action"], d)
                    except Exception as ex:  # noqa: BLE001
                        err = f"{type(ex).__name__}: {ex}"
                    records, n_records = _diff(before, _snapshot(con))
                h = S.state_hash(con)
                rec = d.get("state_hash")
                match = None if rec is None else rec == h
                mismatches += match is False or err is not None
                steps.append({"seq": e["id"], "ts": e["ts"], "actor": e["actor"], "action": e["action"],
                              "detail": {k: v for k, v in d.items() if k not in ("state_hash", "assignments")},
                              "state_change": change, "hash": h, "recorded_hash": rec, "match": match, "error": err,
                              "records": records, "records_total": n_records,
                              "kpis": _kp(con, fid)})
            final = S.state_hash(con)
    finally:
        os.remove(path)
    return {"events": len(events), "steps": steps, "initial_hash": initial, "replayed_hash": final, "live_hash": live_hash,
            "verified": final == live_hash and mismatches == 0, "mismatches": mismatches}
