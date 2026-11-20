"""Passenger Impact Agent: powers the Connecting Passengers tab.

It identifies affected passengers, detects likely missed connections, calculates feasibility against the minimum connection
time (MCT), groups passengers by final destination / connecting flight, and highlights urgent cases.
It ONLY analyses and recommends. It has no booking credentials and no execution pathway: its single write is staging a
rebooking RECOMMENDATION in the controller approval queue, where a human approves or rejects it (with the PIN).

Every label is deterministic and explainable: the rules and thresholds below are shown in the UI next to each label.
Missing or conflicting data is reported, never guessed. Personal data is masked unless a controller reveals it (audited)."""
import json
from contextlib import closing
from datetime import datetime, timedelta

import guards as G
import services as S

RULES = {  # thresholds used for labels; shown to the user, change here
    "MCT_MIN": G.LIMITS["MCT_MIN"],
    "BAG_MCT_MIN": G.LIMITS["BAG_MCT_MIN"],
    "MAX_ONWARD_HOLD_MIN": G.LIMITS["MAX_ONWARD_HOLD_MIN"],
    "VULNERABLE_SLACK_MIN": 30,   # a connection with less spare time than this beyond MCT is vulnerable to any further delay
    "DECISION_WINDOW_MIN": 30,    # a hold that could still save a connection must be decided before the onward flight departs
    "STALE_AFTER_MIN": 30,        # flag data that has not been updated for this long while the disruption is active
}
MCT_SOURCE = (f"FRA hub minimum connection time {G.LIMITS['MCT_MIN']} min (AeroTurn guard rule). No airport- or itinerary-specific MCT "
              f"table (e.g. Schengen / non-Schengen) is configured, so the hub rule is applied to every connection.")
ASSIST = {"UM": "Unaccompanied minor", "PRM": "Reduced mobility assistance", "MEDICAL": "Medical assistance", "INFANT": "Travelling with an infant",
          "ELDERLY": "Elderly passenger assistance"}
PRIO_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Unknown": 4}


def _clock(base, minutes):
    return (base + timedelta(minutes=minutes)).strftime("%H:%M")


def _mask(name):
    return " ".join(w[0] + "•" * max(len(w) - 1, 2) for w in str(name).split()) if name else "—"


def _approved_moves(con):
    """Passenger -> human-approved booking, from the append-only audit log (the only proof a passenger is 'Rebooked')."""
    proof, unapproved = {}, {}
    for r in S.rows(con, "SELECT id, ts, actor, action, detail FROM audit WHERE action IN ('CHANGE_APPROVED', 'REBOOK_COMMIT') ORDER BY id"):
        d = json.loads(r["detail"] or "{}")
        for p in d.get("rebooked_pax", []):
            if r["action"] == "CHANGE_APPROVED":
                proof[p] = {"change_id": d.get("id"), "approved_by": r["actor"], "at": r["ts"], "audit_id": r["id"]}
            else:
                unapproved[p] = {"actor": r["actor"], "at": r["ts"], "audit_id": r["id"]}
    return proof, unapproved


def _pending(con, fid):
    """Rebooking recommendations / decisions waiting in the controller approval queue for this flight."""
    per_pax, all_at_risk = {}, None
    for c in S.rows(con, "SELECT id, title, actions FROM changes WHERE status = 'PENDING' AND flight_id = ?", (fid,)):
        for a in json.loads(c["actions"]):
            if a.get("kind") == "REBOOK":
                per_pax[a.get("pax_id")] = {"change_id": c["id"], "to": a.get("to_flight")}
            elif a.get("kind") in ("REBOOK_AT_RISK", "REBOOK_TIERS"):
                all_at_risk = {"change_id": c["id"], "title": c["title"]}
    return per_pax, all_at_risk


def analyse(fid):
    now = datetime.now()
    base = now  # simulated clock: every time in the dataset is minutes relative to now (T+0)
    d = S.get_turnaround_and_crew_state(fid)
    f = d["flight"]
    with closing(S.connect()) as con:
        info = S.one(con, "SELECT * FROM flight_info WHERE flight_id = ?", (fid,))
        meta = S.one(con, "SELECT value FROM meta WHERE key = 'seeded_at'")
        last = S.one(con, "SELECT MAX(ts) AS ts FROM audit WHERE action IN ('CHANGE_APPROVED','REBOOK_COMMIT','MAINTENANCE_SIGNED','TASK_DONE')")
        flights = {x["id"]: x for x in S.rows(con, S.F_SQL)}
        proof, unapproved = _approved_moves(con)
        pend, pend_all = _pending(con, fid)
        bags = {b["pax_id"]: b for b in S.rows(con, "SELECT pax_id, COUNT(*) AS n, GROUP_CONCAT(DISTINCT flight_id) AS on_flights, "
                                                    "GROUP_CONCAT(DISTINCT status) AS status FROM bags GROUP BY pax_id")}
    inb_eff, mct = f["eff"], RULES["MCT_MIN"]
    need_bag = d["kpis"]["bag_mct"]  # bag minimum connection time incl. remote-stand penalty (code)
    alts_by_dest = {}
    for p in d["passengers"]:
        for o in p["options"]:
            alts_by_dest.setdefault(p["dest"], {})[o["id"]] = o
    conflicts, out = [], []
    for p in d["passengers"]:
        onward = flights.get(p["onward"])
        orig = flights.get(p["orig_flight"]) if p["orig_flight"] else None
        row = {"ref": p["id"], "name_masked": _mask(p["name"]), "final_destination": p["dest"], "connecting_flight": p["onward"],
               "assistance": p["tier"] in G.VULNERABLE, "priority_category": "Protected (care)" if p["tier"] in G.VULNERABLE else "Standard",
               "mct_min": mct, "bags": p["bags"], "missing": [], "factors": []}
        if not onward:
            row.update(status="Unknown", priority="Unknown", risk=None, available_min=None, departs_in_min=None)
            row["missing"].append(f"Connecting flight {p['onward']} not found in flight data")
            out.append(row)
            continue
        avail = onward["eff"] - inb_eff
        dep_in = onward["eff"]
        row.update(available_min=avail, departs_in_min=dep_in, departure_sched=_clock(base, onward["t_min"]),
                   departure_est=_clock(base, onward["eff"]), departure_iso=(base + timedelta(minutes=onward["eff"])).isoformat(timespec="seconds"),
                   onward_delay_min=onward["delay_min"])
        moved = p["orig_flight"] and p["onward"] != p["orig_flight"]
        alts = [a for a in alts_by_dest.get(p["dest"], {}).values() if a["id"] != p["onward"]]
        seats = sum(a["free"] for a in alts)
        # ---- status (deterministic) ----
        if moved and p["id"] in proof:
            status = "Rebooked"
        elif dep_in <= 0 and avail < mct:
            status = "Confirmed missed"
        elif avail < mct:
            status = "Likely missed"
        elif avail - mct < RULES["VULNERABLE_SLACK_MIN"]:
            status = "At risk"
        else:
            status = "Safe connection"
        if moved and p["id"] not in proof:
            conflicts.append(f"{p['id']} is seated on {p['onward']} instead of {p['orig_flight']} but the audit log has no human-approved booking for it")
            row["missing"].append("Seat moved without a human-approved booking in the audit log (status not shown as Rebooked)")
        b = bags.get(p["id"])
        if b and b["on_flights"] and b["on_flights"] != p["onward"]:
            conflicts.append(f"{p['id']}: checked bag is on {b['on_flights']} but the passenger is on {p['onward']}")
        # ---- explanation factors ----
        F = row["factors"]
        F.append(f"{fid} estimated on-block {_clock(base, inb_eff)} (scheduled {_clock(base, f['t_min'])}, {f['delay_min']} min late) · predicted")
        F.append(f"Available connection {avail} min vs {mct} min MCT " + (f"({mct - avail} min short)" if avail < mct else f"({avail - mct} min spare)"))
        F.append(f"{p['onward']} departs {_clock(base, onward['eff'])}" + (" (already departed)" if dep_in <= 0 else f", in {dep_in} min"))
        hold_needed = mct - avail
        holdable = onward["kind"] == "ONWARD" and 0 < hold_needed <= RULES["MAX_ONWARD_HOLD_MIN"] and dep_in > 0
        if status in ("Likely missed", "Confirmed missed"):
            if holdable:
                F.append(f"A hold of {hold_needed} min on {p['onward']} would save it (within the {RULES['MAX_ONWARD_HOLD_MIN']}-min hold cap); decide within {dep_in} min")
            elif hold_needed > RULES["MAX_ONWARD_HOLD_MIN"]:
                F.append(f"A hold would need {hold_needed} min, above the {RULES['MAX_ONWARD_HOLD_MIN']}-min cap: cannot be saved by a hold")
            F.append(f"{len(alts)} alternative flight(s) to {p['dest']} reachable with free seats ({seats} seats, shared with other passengers)" if alts
                     else f"No alternative seats to {p['dest']} in the schedule: overnight stay and compensation likely")
        if row["assistance"]:
            F.append("Special assistance recorded (details restricted): protected-priority passenger")
        if p["bags"]:
            F.append(f"{p['bags']} checked bag(s); bag transfer needs {need_bag} min" + ("" if avail >= need_bag or status == "Rebooked" else " (bags may not make it)"))
        if status == "Rebooked":
            pr = proof[p["id"]]
            F.insert(0, f"Rebooked from {p['orig_flight']} onto {p['onward']}, approved by {pr['approved_by']} (decision #{pr['change_id']}, audit #{pr['audit_id']})")
        # ---- priority (deterministic, explainable) ----
        if status in ("Safe connection", "Rebooked"):
            prio, why = "Low", "connection remains feasible" if status != "Rebooked" else "rebooked with a human-approved booking"
        elif status == "At risk":
            prio, why = ("High", "vulnerable connection for a protected-priority passenger") if row["assistance"] else ("Medium", "connection is vulnerable to any further delay")
        else:
            reasons = []
            if row["assistance"]:
                reasons.append("protected-priority passenger")
            if not alts:
                reasons.append("no alternative seats")
            if holdable and dep_in <= RULES["DECISION_WINDOW_MIN"]:
                reasons.append(f"hold decision needed within {dep_in} min")
            prio, why = ("Critical", " + ".join(["likely missed"] + reasons)) if reasons else ("High", "likely to miss the connection; alternatives exist")
        row.update(status=status, priority=prio, priority_reason=why,
                   risk={"category": {"Safe connection": "Low", "At risk": "Elevated", "Likely missed": "Very high", "Confirmed missed": "Missed",
                                      "Rebooked": "Resolved"}[status], "basis": "MCT rule on predicted times; no statistical model, so no probability is shown"})
        row["pending"] = pend.get(p["id"]) or (pend_all if status in ("Likely missed", "Confirmed missed") and pend_all else None)
        row["alternatives"] = [{"flight": a["id"], "departs": _clock(base, a["eff"]), "departs_in_min": a["eff"], "free_seats": a["free"],
                                "connection_min": a["eff"] - inb_eff, "bags_make_it": a["eff"] - inb_eff >= need_bag} for a in sorted(alts, key=lambda a: a["eff"])]
        row["hold_option"] = {"flight": p["onward"], "minutes": hold_needed} if holdable and status in ("Likely missed", "Confirmed missed") else None
        row["itinerary"] = [{"flight": fid, "route": f"{f['origin']} → FRA", "sched": _clock(base, f["t_min"]), "est": _clock(base, inb_eff), "kind": "inbound (predicted)"},
                            {"flight": p["onward"], "route": f"FRA → {p['dest']}", "sched": _clock(base, onward["t_min"]), "est": _clock(base, onward["eff"]),
                             "kind": "current onward" + (f" (originally {p['orig_flight']})" if moved else "")}]
        row["bag_location"] = b["on_flights"] if b else None
        out.append(row)
    out.sort(key=lambda r: (PRIO_ORDER[r["priority"]], r["departs_in_min"] if r["departs_in_min"] is not None else 9999, r["ref"]))

    affected = [r for r in out if r["status"] not in ("Safe connection",)]
    unresolved = [r for r in affected if r["status"] != "Rebooked"]
    pending_any = any(r["pending"] for r in out)
    status = "resolved" if not unresolved else "recovering" if (pending_any or any(r["status"] == "Rebooked" for r in out)) else "active"
    as_of = max(x for x in [meta and meta["value"], last and last["ts"]] if x) if (meta or last) else None
    age = int((now - datetime.fromisoformat(as_of)).total_seconds() // 60) if as_of else None
    groups = {}
    for key in ("final_destination", "connecting_flight"):
        g = {}
        for r in affected:
            x = g.setdefault(r[key], {"key": r[key], "passengers": 0, "by_priority": {}, "assistance": 0, "alternatives": set()})
            x["passengers"] += 1
            x["by_priority"][r["priority"]] = x["by_priority"].get(r["priority"], 0) + 1
            x["assistance"] += r["assistance"]
            x["alternatives"].update(a["flight"] for a in r.get("alternatives", []))
        groups[key] = sorted(({**x, "alternatives": sorted(x["alternatives"])} for x in g.values()),
                             key=lambda x: (min(PRIO_ORDER[p] for p in x["by_priority"]), -x["passengers"]))
    return {
        "summary": {"flight": fid, "route": f"{f['origin']} → FRA", "scheduled_arrival": _clock(base, f["t_min"]), "estimated_arrival": _clock(base, inb_eff),
                    "delay_min": f["delay_min"], "delay_reason": info["delay_reason"] if info else None, "reason_source": info["reason_source"] if info else None,
                    "connecting_passengers": len(out), "affected_passengers": len(affected), "affected_connections": len({r["connecting_flight"] for r in affected}),
                    "unresolved": len(unresolved), "status": status},
        "passengers": out, "groups": groups, "rules": RULES, "mct_source": MCT_SOURCE,
        "data": {"computed_at": now.strftime("%H:%M:%S"), "as_of": as_of, "age_min": age,
                 "stale": bool(age is not None and age > RULES["STALE_AFTER_MIN"] and status != "resolved"),
                 "source": "SIMULATED dataset (AeroTurn seed): no live airline feed is connected",
                 "predicted": ["estimated arrival", "available connection time", "connection risk", "status"],
                 "clock": "Simulated clock: dataset times are minutes relative to now, shown as local clock times",
                 "conflicts": conflicts},
    }


def reveal(pax_id, controller, pin):
    """Role-based access to personal data: only an authenticated Operations Controller (name + PIN) can unmask it; audited."""
    controller = S._check_pin(controller, pin)
    with closing(S.connect()) as con:
        p = S.one(con, "SELECT id, name, tier FROM passengers WHERE id = ?", (str(pax_id),))
        if not p:
            raise ValueError(f"Unknown passenger {pax_id}")
        S.audit(con, controller, "PII_VIEWED", {"pax": p["id"]})
    return {"ref": p["id"], "name": p["name"], "assistance": ASSIST.get(p["tier"], "None recorded")}


def recommend(fid, pax_ids, by, overrides=None):
    """Hand selected passengers to the rebooking-recommendation workflow. Nothing is booked here: the agent picks the earliest
    flight that the passenger and their bags can make (or the controller's chosen option) and stages it as a PENDING change.
    The guards dry-run it first (e.g. a batch that would leave a vulnerable passenger behind is refused)."""
    by = str(by or "").strip()[:40]
    if not by:
        raise PermissionError("Your name is required to send a recommendation")
    if not isinstance(pax_ids, list) or not 0 < len(pax_ids) <= 100:
        raise ValueError("Select 1-100 passengers")
    rows = {r["ref"]: r for r in analyse(fid)["passengers"]}
    overrides = overrides if isinstance(overrides, dict) else {}
    picked, skipped, taken = [], [], {}
    with closing(S.connect()) as con:  # the guard's own care-tier order (UM, PRM, MEDICAL, ...) decides who gets seats first
        tiers = {r["id"]: r["tier"] for r in S.rows(con, "SELECT id, tier FROM passengers WHERE inbound = ?", (fid,))}
    ordered = G.priority_order([{"id": x, "tier": tiers.get(x, "STANDARD")} for x in set(map(str, pax_ids))])
    for pid in (o["id"] for o in ordered):
        r = rows.get(pid)
        if not r or r["status"] in ("Safe connection", "Rebooked", "Unknown"):
            skipped.append({"ref": pid, "reason": "not at risk / already rebooked / unknown"})
            continue
        if r["pending"]:
            skipped.append({"ref": pid, "reason": f"already in the approval queue (#{r['pending']['change_id']})"})
            continue
        opts = [a for a in r["alternatives"] if a["free_seats"] - taken.get(a["flight"], 0) > 0]
        choice = next((a for a in opts if a["flight"] == overrides.get(pid)), None) or next((a for a in opts if a["bags_make_it"] or not r["bags"]), None) or (opts[0] if opts else None)
        if not choice:
            skipped.append({"ref": pid, "reason": "no alternative seats: needs manual handling (hotel / other carrier)"})
            continue
        taken[choice["flight"]] = taken.get(choice["flight"], 0) + 1
        picked.append({"kind": "REBOOK", "pax_id": pid, "to_flight": choice["flight"]})
    staged = []
    for k in range(0, len(picked), 20):  # the approval workflow takes at most 20 actions per change
        chunk = picked[k:k + 20]
        r = S.stage_controller_schedule_change(fid, f"Rebooking recommendation: {len(chunk)} passenger(s) off {fid}", chunk,
                                               f"Passenger Impact Agent · {by}"[:40])
        staged.append({**r, "passengers": [a["pax_id"] for a in chunk]})
    return {"staged": staged, "skipped": skipped}
