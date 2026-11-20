"""Decision Log views: calls panel, transcript drawer, one tab per decision, and a per-decision report.

What CODE decides here (never the LLM): every status and pipeline step, guard reason codes, the smallest legal fix
(verified by a rolled-back dry run), compliance results, impact numbers, call participants and durations, and the
"Requires controller approval" flag. The LLM only writes prose in the report: the problem statement, suggested next
steps and open risks. Nothing here executes anything: approval happens only in the controller approval gate."""
import json
import re
from contextlib import closing
from datetime import datetime

import agent
import services as S
from guards import VULNERABLE

APPROVAL_KINDS = {"DELAY", "GATE_CHANGE", "CREW_SWAP", "CREW_AUTOSWAP", "DEPARTURE_RELEASE"}  # network schedule, gate or crew changes
ACTION_CATEGORY = {"CREW_SWAP": "crew", "CREW_AUTOSWAP": "crew", "ASSIGN_ENGINEER": "maintenance", "REBOOK": "passengers",
                   "REBOOK_AT_RISK": "passengers", "REBOOK_TIERS": "passengers", "GATE_CHANGE": "gate", "DELAY": "network",
                   "DEPARTURE_RELEASE": "network"}
STATUS_LABEL = {"PROPOSED": "Proposed", "GUARD_CHECKED": "Guard-checked", "READY": "Confirmed · ready to send", "AWAITING_APPROVAL": "Awaiting controller approval",
                "EXECUTED": "Approved & executed", "BLOCKED": "Blocked by guards", "REJECTED": "Rejected", "ROLLED_BACK": "Rolled back",
                "RECORDED": "Recorded (no system action)"}


# ---------- guard reason codes (parsed from the guard's own message; guards.py is untouched) ----------
CODES = [
    (r"CREW DUTY: (\S+) would reach (\d+) min of duty; legal ceiling is (\d+)", "CREW_FDP_EXCEEDED",
     lambda m: f"{m[1]} {int(m[2]) - int(m[3])} min over the {m[3]}-min limit"),
    (r"CREW RATING: (\S+) is rated (\S+), aircraft is (\S+)", "CREW_TYPE_RATING", lambda m: f"{m[1]} rated {m[2]}, aircraft {m[3]}"),
    (r"MAINTENANCE: .*NO-GO items \[(.*?)\]", "MX_SIGNOFF_MISSING", lambda m: f"{m[1].replace(chr(39), '')} not signed off by a licensed engineer"),
    (r"PRIORITY: vulnerable passengers \[(.*?)\]", "VULNERABLE_PRIORITY", lambda m: f"{len(m[1].split(','))} vulnerable passengers would be skipped"),
    (r"HOLD: (\S+) may be held at most (\d+) min \(asked (\d+)\)", "ONWARD_HOLD_CAP", lambda m: f"{m[1]}: {m[3]} min asked, {m[2]} min max"),
    (r"BAG MATCH: (\d+)", "BAG_MATCH", lambda m: f"{m[1]} bag(s) would fly without their passenger"),
    (r"Gate (\S+) is occupied by (\S+)", "GATE_OCCUPIED", lambda m: f"{m[1]} is occupied by {m[2]}"),
    (r"No legal standby (\S+)", "NO_LEGAL_STANDBY", lambda m: f"no legal standby {m[1]}"),
    (r"(\S+) is (?:not on standby|\d+ min away|\S+, cannot replace)", "STANDBY_NOT_LEGAL", lambda m: f"{m[1]} cannot be used"),
    (r"applies to the inbound flight (\S+), not (\S+)|applies to (\S+), not (\S+)", "WRONG_FLIGHT", lambda m: "action names the wrong flight"),
    (r"Delay must be", "DELAY_OUT_OF_RANGE", lambda m: "delay must be 1-240 min"),
    (r"Unknown (\w+) '(.*?)'", "UNKNOWN_REFERENCE", lambda m: f"unknown {m[1]} {m[2]}"),
]


def reason_code(reason):
    if not reason:
        return None
    for pat, code, detail in CODES:
        m = re.search(pat, reason)
        if m:
            return {"code": code, "detail": detail(m), "message": reason}
    return {"code": "GUARD_REFUSED", "detail": reason[:120], "message": reason}


def legal_fix(fid, actions):
    """Smallest legal change to a blocked proposal, computed by code: repeatedly apply the minimal fix for the guard that
    refused it, dry-running after each step (rolled back), until the guards pass or no fix exists."""
    d = S.get_turnaround_and_crew_state(fid)
    out_id, cur, steps = d["outbound"]["id"], [dict(a) for a in actions], []
    for _ in range(4):
        sim = S.simulate(fid, cur)
        if sim["ok"]:
            return {"ok": True, "steps": steps, "actions": cur}
        rc = reason_code(sim["reason"])
        code, nxt, why = rc["code"], None, ""
        if code in ("CREW_FDP_EXCEEDED", "CREW_TYPE_RATING") and not any(a["kind"] == "CREW_AUTOSWAP" for a in cur):
            legal = [f"{c['id']} ({c['role']}) → {c['options'][0]['id']} · {c['options'][0]['location']}, {c['options'][0]['walk_min']} min away"
                     for c in d["crew"] if c["margin"] < 0 and c["options"]]
            nxt, why = [{"kind": "CREW_AUTOSWAP", "flight_id": fid}] + cur, "Call out the closest legal reserve crew first: " + "; ".join(legal[:3]) + \
                (f" (+{len(legal) - 3} more)" if len(legal) > 3 else "")
        elif code == "MX_SIGNOFF_MISSING" and any(a["kind"] == "DEPARTURE_RELEASE" for a in cur):
            open_nogo = [m for m in d["maintenance"] if m["severity"] == "NO-GO" and m["status"] != "SIGNED"]
            nxt = [a for a in cur if a["kind"] != "DEPARTURE_RELEASE"]
            gap = max(d["kpis"]["turn_gap"], 30)
            if not any(a["kind"] == "DELAY" and a.get("flight_id") == out_id for a in nxt):
                nxt.append({"kind": "DELAY", "flight_id": out_id, "minutes": min(gap, 240)})
            for m in open_nogo:
                if m["status"] == "OPEN" and not any(a["kind"] == "ASSIGN_ENGINEER" for a in nxt):
                    nxt.append({"kind": "ASSIGN_ENGINEER", "item_id": m["id"], "engineer_id": d["engineers"][0]["id"]})
            why = (f"Do not release yet: retime {out_id} +{min(gap, 240)} min and get {', '.join(m['id'] for m in open_nogo)} signed off by a licensed "
                   f"engineer; release afterwards")
        elif code == "VULNERABLE_PRIORITY":
            nxt = [a for a in cur if a["kind"] not in ("REBOOK_TIERS", "REBOOK")] + [{"kind": "REBOOK_AT_RISK", "flight_id": fid}]
            why = "Rebook every at-risk passenger in one batch; code puts unaccompanied minors, PRM and medical passengers first"
        elif code == "ONWARD_HOLD_CAP":
            cap = d["limits"]["MAX_ONWARD_HOLD_MIN"]
            nxt = [{**a, "minutes": min(a["minutes"], cap)} if a["kind"] == "DELAY" else a for a in cur]
            why = f"Cap the hold at {cap} min; passengers who still miss it are rebooked"
        elif code == "GATE_OCCUPIED":
            free = [g["id"] for g in d["gates"] if g["kind"] == "CONTACT" and not g["occupied_by"]]
            if free:
                nxt = [{**a, "gate": free[0]} if a["kind"] == "GATE_CHANGE" else a for a in cur]
                why = f"Use free contact gate {free[0]} instead"
        elif code == "WRONG_FLIGHT":
            nxt = [{**a, "flight_id": out_id if a["kind"] == "DEPARTURE_RELEASE" else fid} if a["kind"] in
                   ("CREW_AUTOSWAP", "REBOOK_AT_RISK", "REBOOK_TIERS", "DEPARTURE_RELEASE") else a for a in cur]
            why = "Point the action at the right flight of this turnaround"
        if not nxt or nxt == cur:
            return {"ok": False, "steps": steps, "actions": cur, "remaining": rc}
        steps.append({"for": code, "fix": why})
        cur = nxt
    sim = S.simulate(fid, cur)
    return {"ok": sim["ok"], "steps": steps, "actions": cur, "remaining": None if sim["ok"] else reason_code(sim["reason"])}


# ---------- status pipeline ----------
def status_of(i):
    cs = i.get("change_status")
    if i["status"] == "REJECTED":
        return "REJECTED"
    if cs == "APPROVED":
        return "EXECUTED"
    if cs == "PENDING":
        return "AWAITING_APPROVAL"
    if cs in ("REJECTED", "ROLLED_BACK"):
        return cs
    if not i["actions"]:
        return "RECORDED"
    if i["guard_status"] == "BLOCKED":
        return "BLOCKED"
    if i["guard_status"] == "PASS":
        return "READY" if i["status"] == "CONFIRMED" else "GUARD_CHECKED"
    return "PROPOSED"


def pipeline(i, st):
    blocked, rej = st == "BLOCKED", st == "REJECTED" and i.get("change_status") == "REJECTED"
    steps = [("Proposed", "done"),
             ("Guard-checked", "failed" if blocked else "done" if i["guard_status"] == "PASS" or st in ("AWAITING_APPROVAL", "EXECUTED", "ROLLED_BACK") else "todo"),
             ("Awaiting controller approval", "current" if st == "AWAITING_APPROVAL" else "done" if st in ("EXECUTED", "ROLLED_BACK") or rej else "todo"),
             ("Approved", "done" if st in ("EXECUTED", "ROLLED_BACK") else "failed" if rej else "todo"),
             ("Executed", "done" if st == "EXECUTED" else "failed" if st == "ROLLED_BACK" else "todo")]
    if not i["actions"]:
        steps = [("Proposed", "done"), ("Recorded: no system action needed", "done")]
    return [{"step": a, "state": b} for a, b in steps]


def _items(con, where="1=1", args=()):
    rows = S.rows(con, f"""SELECT i.*, ch.status AS change_status, ch.decided_by AS approved_by, ch.ts AS change_ts, c.title AS call_title
        FROM ledger_items i LEFT JOIN changes ch ON ch.id = i.change_id JOIN calls c ON c.id = i.call_id WHERE {where} ORDER BY i.at, i.id""", args)
    for i in rows:
        i["actions"], i["flags"] = json.loads(i["actions"] or "[]"), json.loads(i["flags"] or "[]")
        i["label_id"] = f"D-{i['id']:02d}" if i["kind"] == "DECISION" else f"{i['kind'][0]}-{i['id']:02d}"
        i["call_label"] = f"C-{i['call_id']:02d}"
    return rows


def _refresh_guards(items):
    """Guard results shown in the UI are re-computed by code against the CURRENT state (a conditional release passes once
    the crew swap and sign-off are done) for every decision that is not already with the controller or decided."""
    for i in items:
        if i["kind"] == "DECISION" and i["actions"] and i["status"] != "REJECTED" and i.get("change_status") in (None, "BLOCKED"):
            sim = S.simulate(i["flight_id"], i["actions"])
            g = "PASS" if sim["ok"] else "BLOCKED"
            if (g, sim["reason"]) != (i["guard_status"], i["guard_reason"] or ""):
                with closing(S.connect()) as con:
                    con.execute("UPDATE ledger_items SET guard_status = ?, guard_reason = ? WHERE id = ?", (g, sim["reason"], i["id"]))
                i["guard_status"], i["guard_reason"] = g, sim["reason"]
    return items


def _roles(con, call_id):
    return {r["speaker"]: r["role"] for r in S.rows(con, "SELECT speaker, MAX(role) AS role FROM call_lines WHERE call_id = ? GROUP BY speaker", (call_id,))}


def _decorate(i, roles):
    i["status_code"] = status_of(i)
    i["status_label"] = STATUS_LABEL[i["status_code"]]
    i["guard"] = reason_code(i["guard_reason"]) if i["guard_status"] == "BLOCKED" else None
    i["role"] = roles.get(i["made_by"])
    i["category"] = next((ACTION_CATEGORY[a["kind"]] for a in i["actions"] if a["kind"] in ACTION_CATEGORY), None) or i["category"] or "other"
    i["requires_approval"] = any(a["kind"] in APPROVAL_KINDS for a in i["actions"])
    return i


# ---------- calls panel + transcript drawer ----------
def _secs(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def calls(fid=None):
    with closing(S.connect()) as con:
        cs = S.rows(con, "SELECT * FROM calls" + (" WHERE flight_id = ?" if fid else "") + " ORDER BY id DESC", (fid,) if fid else ())
        out = []
        for c in cs:
            lines = S.rows(con, "SELECT n, at, speaker, role FROM call_lines WHERE call_id = ? ORDER BY n", (c["id"],))
            items = [_decorate(i, _roles(con, c["id"])) for i in _items(con, "i.call_id = ?", (c["id"],))]
            people = {}
            for ln in lines:
                p = people.setdefault(ln["speaker"], {"name": ln["speaker"], "role": ln["role"], "joined": ln["at"], "joined_line": ln["n"]})
                p["last"] = ln["at"]
            end = c["ended"] or (lines[-1]["at"] if lines else c["started"])
            now = datetime.now().strftime("%H:%M:%S")
            dur = ((_secs(now if c["status"] == "LIVE" else end) - _secs(c["started"])) % 86400) // 60 if c["started"] else 0
            kinds = lambda k: [i for i in items if i["kind"] == k]  # noqa: E731
            out.append({"id": c["id"], "label": f"C-{c['id']:02d}", "flight_id": c["flight_id"], "title": c["title"], "status": c["status"],
                        "source": c["source"], "started": c["started"], "ended": c["ended"], "duration_min": dur, "lines": len(lines),
                        "extracted": bool(c["extracted_at"]), "demo_id": c["demo_id"],
                        "participants": [{**p, "left": p["last"] if c["status"] != "LIVE" else None} for p in people.values()],
                        "counts": {"issues": len(kinds("ISSUE")), "facts": len(kinds("FACT")), "decisions": len(kinds("DECISION")),
                                   "actions": len(kinds("ACTION")), "blocked": sum(i["status_code"] == "BLOCKED" for i in kinds("DECISION"))},
                        "issues": [{k: i[k] for k in ("id", "summary", "at", "line_n", "category")} for i in kinds("ISSUE")],
                        "facts": [{k: i[k] for k in ("id", "label", "value", "at", "line_n")} for i in kinds("FACT")],
                        "actions": [{k: i[k] for k in ("id", "summary", "owner", "due", "done", "done_by", "at", "line_n")} for i in kinds("ACTION")],
                        "decisions": [{k: i[k] for k in ("id", "label_id", "summary", "status_code", "status_label", "at", "line_n")} for i in kinds("DECISION")]})
    return out


def transcript(cid):
    with closing(S.connect()) as con:
        c = S.one(con, "SELECT * FROM calls WHERE id = ?", (int(cid),))
        if not c:
            raise ValueError(f"No call #{cid}")
        lines = S.rows(con, "SELECT n, at, speaker, role, text FROM call_lines WHERE call_id = ? ORDER BY n", (c["id"],))
        tags = S.rows(con, "SELECT id, kind, line_n FROM ledger_items WHERE call_id = ? AND status <> 'REJECTED'", (c["id"],))
    for ln in lines:
        ln["tags"] = [{"kind": t["kind"], "id": t["id"]} for t in tags if t["line_n"] == ln["n"]]
    return {"call": {"id": c["id"], "label": f"C-{c['id']:02d}", "title": c["title"], "status": c["status"], "flight_id": c["flight_id"]}, "lines": lines}


def set_action_done(item_id, done, by):
    by = str(by or "").strip()[:40]
    if not by:
        raise PermissionError("Your name is required")
    with closing(S.connect()) as con:
        it = S.one(con, "SELECT * FROM ledger_items WHERE id = ? AND kind = 'ACTION'", (int(item_id),))
        if not it:
            raise ValueError(f"No action item #{item_id}")
        con.execute("UPDATE ledger_items SET done = ?, done_by = ? WHERE id = ?", (1 if done else 0, by if done else None, it["id"]))
        S.audit(con, by, "ACTION_DONE" if done else "ACTION_REOPENED", {"item": it["id"]})
    return {"id": it["id"], "done": bool(done)}


# ---------- decision tabs ----------
def list_decisions(fid=None):
    with closing(S.connect()) as con:
        items = _items(con, "i.kind = 'DECISION'" + (" AND i.flight_id = ?" if fid else ""), (fid,) if fid else ())
        roles = {c: _roles(con, c) for c in {i["call_id"] for i in items}}
    return [_decorate(i, roles[i["call_id"]]) for i in _refresh_guards(items)]


def detail(did):
    with closing(S.connect()) as con:
        found = _items(con, "i.id = ? AND i.kind = 'DECISION'", (int(did),))
        if not found:
            raise ValueError(f"No decision D-{did}")
        i = _refresh_guards(found)[0]
        roles = _roles(con, i["call_id"])
        _decorate(i, roles)
        issues = _items(con, "i.flight_id = ? AND i.kind = 'ISSUE' AND i.problem = ? AND i.status <> 'REJECTED'", (i["flight_id"], i["problem"]))
        facts = _items(con, "i.call_id = ? AND i.kind = 'FACT'", (i["call_id"],))
        rep = S.one(con, "SELECT ts FROM decision_reports WHERE item_id = ?", (i["id"],))
    i["others_on_call"] = [{"name": n, "role": r} for n, r in roles.items() if n != i["made_by"]]
    i["issue_addressed"] = [{k: x[k] for k in ("id", "summary", "at", "line_n", "call_id", "made_by", "quote")} for x in issues]
    i["facts"] = [{k: x[k] for k in ("label", "value", "at", "line_n")} for x in facts]
    i["pipeline"] = pipeline(i, i["status_code"])
    i["fix"] = legal_fix(i["flight_id"], i["actions"]) if i["status_code"] == "BLOCKED" else None
    i["report_cached_at"] = rep["ts"] if rep else None
    i["can_confirm"] = i["status"] == "UNCONFIRMED"
    i["can_send"] = i["status"] == "CONFIRMED" and i["status_code"] == "READY"
    return i


# ---------- per-decision report ----------
REPORT_SYSTEM = """You write the per-decision report for an airline Operations Controller. Deterministic code has already
produced the compliance results and impact numbers you receive; they are authoritative. Never contradict them, never invent
numbers, never suggest bypassing crew duty limits, the maintenance sign-off, vulnerable-passenger priority or controller approval.
If the decision is BLOCKED, the next steps must follow the legal fix computed by code.
Write: problem (max 2 sentences), next_steps (3-6, ordered by priority, each with an owner role, a deadline as clock time HH:MM
after <now>, and optionally one machine action from the allowed list if the step changes the operation), open_risks (max 4 short).
Allowed actions: DELAY{flight_id,minutes} GATE_CHANGE{flight_id,gate} CREW_AUTOSWAP{flight_id} REBOOK_AT_RISK{flight_id}
ASSIGN_ENGINEER{item_id,engineer_id} DEPARTURE_RELEASE{flight_id} (each with "kind").
Return ONLY JSON: {"problem":"...","next_steps":[{"priority":1,"step":"imperative sentence","owner":"role","deadline":"HH:MM",
"action":null}],"open_risks":["..."]}"""


def _compliance_and_impact(i):
    fid = i["flight_id"]
    now = S.get_turnaround_and_crew_state(fid)
    k0, out0 = now["kpis"], now["outbound"]
    use = i["fix"]["actions"] if i["status_code"] == "BLOCKED" and i["fix"] and i["fix"]["ok"] else i["actions"]
    pv = S.preview_passengers(fid, use) if use and i["status_code"] not in ("EXECUTED",) else None
    applied = pv and pv["ok"]
    k1 = pv["kpis"] if applied else None
    ba = lambda key: f"{k0[key]} now → {k1[key]} after" if k1 else f"{k0[key]} now"  # noqa: E731
    holds = [a for a in use if a["kind"] == "DELAY"]
    onward_holds = [a for a in i["actions"] if a["kind"] == "DELAY" and a["flight_id"] != out0["id"]]  # compliance judges the decision itself
    checks = [
        {"check": "Guard dry run of this decision", "result": "N/A" if not i["actions"] else ("FAIL" if i["status_code"] == "BLOCKED" else "PASS"),
         "detail": (i["guard"]["code"] + ": " + i["guard"]["detail"]) if i["guard"] else ("no system action" if not i["actions"] else "all guards pass")},
        {"check": f"Crew legal duty (FDP ≤ {now['limits']['MAX_FDP_MIN']} min)", "result": "PASS" if (k1 or k0)["crew_illegal"] == 0 else "FAIL",
         "detail": f"crew over limit: {ba('crew_illegal')}"},
        {"check": "NO-GO maintenance signed off", "result": "PASS" if (k1 or k0)["open_nogo"] == 0 else "OPEN", "detail": f"open NO-GO items: {ba('open_nogo')}"},
        {"check": "Vulnerable passengers protected", "result": "PASS" if (k1 or k0)["vulnerable_at_risk"] == 0 else "AT RISK",
         "detail": f"vulnerable passengers at risk: {ba('vulnerable_at_risk')}"},
        {"check": f"Onward holds ≤ {now['limits']['MAX_ONWARD_HOLD_MIN']} min", "result": "N/A" if not onward_holds else
         ("PASS" if all(a["minutes"] <= now["limits"]["MAX_ONWARD_HOLD_MIN"] for a in onward_holds) else "FAIL"),
         "detail": ", ".join(f"{a['flight_id']} +{a['minutes']} min" for a in onward_holds) or "no onward holds"},
        {"check": "Controller approval required", "result": "YES" if i["requires_approval"] else "NO",
         "detail": "changes network schedule, gate or crew" if i["requires_approval"] else "in-policy action"},
    ]
    gate = next((a["gate"] for a in use if a["kind"] == "GATE_CHANGE"), None)
    impact = {"applied_in_dry_run": bool(applied), "blocked": i["status_code"] == "BLOCKED",
              "basis": "legal fix computed by code (the decision itself is blocked)" if use is not i["actions"] else "this decision",
              "passengers_at_risk_now": k0["at_risk"], "vulnerable_at_risk_now": k0["vulnerable_at_risk"],
              "passengers_affected": None, "vulnerable_affected": None, "stranded_after": None,
              "crew": f"{k0['crew_illegal']} crew over the legal limit now" + (f", {k1['crew_illegal']} after" if k1 else ""),
              "gate": f"{out0['id']}: {out0['gate']} → {gate}" if gate else f"{out0['id']} stays at {out0['gate']}",
              "downstream_flights": sorted({a["flight_id"] for a in holds}), "network_delay_min": sum(a["minutes"] for a in holds),
              "bags_moving": None, "bags_at_risk": None}
    if applied:
        s = pv["summary"]
        moved = [p for p in pv["passengers"] if p["outcome"] != "STRANDED"]
        impact.update(passengers_affected=s["rebooked"] + s["kept_by_hold"], stranded_after=s["stranded"],
                      vulnerable_affected=sum(p["tier"] in VULNERABLE for p in moved), bags_moving=s["bags_moving"], bags_at_risk=s["bags_short"],
                      downstream_flights=sorted(set(impact["downstream_flights"]) | {p["onward"] for p in pv["passengers"] if p["outcome"] == "REBOOKED"}))
    system_risks = []
    if applied and pv["summary"]["vulnerable_stranded"]:
        system_risks.append(f"{pv['summary']['vulnerable_stranded']} vulnerable passengers still have no onward seat")
    if applied and pv["summary"]["bags_short"]:
        system_risks.append(f"{pv['summary']['bags_short']} bags may not make the transfer (bag minimum connection {pv['bag_mct']} min)")
    no_standby = [c["id"] for c in now["crew"] if c["margin"] < 0 and not c["options"]]
    if no_standby:
        system_risks.append(f"No legal standby for {', '.join(no_standby)}")
    if k0["open_nogo"] and not any(a["kind"] == "DEPARTURE_RELEASE" for a in i["actions"]):
        system_risks.append("NO-GO maintenance is still open: no release until a licensed engineer signs off")
    return checks, impact, system_risks


def report(did, regenerate=False):
    if not regenerate:
        with closing(S.connect()) as con:
            r = S.one(con, "SELECT * FROM decision_reports WHERE item_id = ?", (int(did),))
        if r:
            return {**json.loads(r["data"]), "generated": r["ts"], "cached": True}
    i = detail(did)
    checks, impact, system_risks = _compliance_and_impact(i)
    now = datetime.now().strftime("%H:%M")
    payload = {"now": now, "decision": i["summary"], "status": i["status_label"], "made_by": f"{i['made_by']} ({i['role']})", "call": i["call_title"],
               "quote": i["quote"], "issues": [x["summary"] for x in i["issue_addressed"]], "facts": i["facts"], "compliance": checks, "impact": impact,
               "legal_fix": i["fix"], "system_risks": system_risks}
    out = agent.llm_json(REPORT_SYSTEM, f"<decision>{json.dumps(payload, default=str)}</decision>", max_tokens=1800)
    steps = []
    for s in (out.get("next_steps") if isinstance(out.get("next_steps"), list) else [])[:6]:
        if not isinstance(s, dict) or not s.get("step"):
            continue
        act = None
        if isinstance(s.get("action"), dict):
            try:
                act = agent._action(s["action"])
            except (KeyError, TypeError, ValueError):
                act = None
        sim = S.simulate(i["flight_id"], [act]) if act else None
        dl = re.search(r"([01]?\d|2[0-3]):[0-5]\d", str(s.get("deadline") or ""))
        steps.append({"priority": min(max(int(s["priority"]), 1), 5) if str(s.get("priority", "")).isdigit() else 3,
                      "step": agent.clean(s["step"], 240), "owner": agent.clean(s.get("owner"), 40), "deadline": dl[0] if dl else None,
                      "action": act, "requires_controller_approval": bool(act and act["kind"] in APPROVAL_KINDS),  # code decides the flag
                      "guard": None if not act else ("PASS" if sim["ok"] else reason_code(sim["reason"])["code"])})
    data = {"decision_id": i["id"], "label": i["label_id"], "source": f"LLM · {agent.MODEL}",
            "problem": agent.clean(out.get("problem"), 500),
            "decision": {"statement": i["summary"], "status": i["status_label"], "status_code": i["status_code"], "actions": i["actions"]},
            "who": {"made_by": i["made_by"], "role": i["role"], "at": i["at"], "call": i["call_title"], "call_label": i["call_label"],
                    "call_id": i["call_id"], "others": i["others_on_call"], "confirmed_by": i["confirmed_by"], "approved_by": i["approved_by"]},
            "evidence": list({(e["call_id"], e["line_n"]): e for e in reversed(
                [{"quote": i["quote"], "at": i["at"], "line_n": i["line_n"], "call_id": i["call_id"], "speaker": i["made_by"]}] +
                [{"quote": x["quote"], "at": x["at"], "line_n": x["line_n"], "call_id": x["call_id"], "speaker": x["made_by"]} for x in i["issue_addressed"][:3]])}.values())[::-1],
            "compliance": checks, "guard": i["guard"], "fix": i["fix"], "impact": impact,
            "next_steps": sorted(steps, key=lambda s: s["priority"]),
            "open_risks": [agent.clean(r, 200) for r in (out.get("open_risks") if isinstance(out.get("open_risks"), list) else [])[:4]],
            "system_risks": system_risks}
    with closing(S.connect()) as con:
        con.execute("INSERT OR REPLACE INTO decision_reports(item_id, data) VALUES (?, ?)", (i["id"], json.dumps(data, default=str)))
        ts = S.one(con, "SELECT ts FROM decision_reports WHERE item_id = ?", (i["id"],))["ts"]
    return {**data, "generated": ts, "cached": False}
