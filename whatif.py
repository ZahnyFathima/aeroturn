"""What-if simulator + recovery option comparison. Code computes every number inside a transaction that is always rolled
back; the LLM only narrates the result."""
from contextlib import closing

import agent
import guards as G
import services as S

KPI_KEYS = ("eta", "at_risk", "vulnerable_at_risk", "crew_illegal", "crew_min_margin", "open_nogo", "off_block", "turn_gap",
            "bags_short_connection", "bags_rush", "protected", "pax_cost_eur", "baseline_eur")


def _clean_params(p):
    p = p if isinstance(p, dict) else {}
    out = {"extra_delay": int(p.get("extra_delay") or 0), "seat_cut_pct": int(p.get("seat_cut_pct") or 0),
           "standby_unavailable": [str(x)[:16] for x in (p.get("standby_unavailable") or [])][:30],
           "gate_closed": str(p.get("gate_closed") or "")[:8] or None, "mx_signed": bool(p.get("mx_signed"))}
    if not 0 <= out["extra_delay"] <= 240 or not 0 <= out["seat_cut_pct"] <= 100:
        raise ValueError("extra_delay must be 0-240 and seat_cut_pct 0-100")
    return out


def _pick(k):
    return {key: k.get(key) for key in KPI_KEYS}


def run(fid, params, actions=None, narrate=True):
    p = _clean_params(params)
    actions = S._clean_actions(actions) if actions else []
    with closing(S.connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            inb = S._inbound(con, fid)
            base = _pick(S._kpis(con, fid))
            applied = []
            if p["extra_delay"]:
                con.execute("UPDATE flights SET delay_min = delay_min + ? WHERE id = ?", (p["extra_delay"], fid))
                applied.append(f"{fid} arrives {p['extra_delay']} min later")
            if p["standby_unavailable"]:
                n = con.execute(f"UPDATE crew SET status = 'OFF' WHERE status = 'STANDBY' AND id IN ({','.join('?' * len(p['standby_unavailable']))})",
                                p["standby_unavailable"]).rowcount
                applied.append(f"{n} standby crew unavailable")
            if p["seat_cut_pct"]:
                free = S.rows(con, "SELECT rowid FROM seats WHERE pax_id IS NULL ORDER BY rowid")
                cut = free[: round(len(free) * p["seat_cut_pct"] / 100)]
                con.executemany("DELETE FROM seats WHERE rowid = ?", [(r["rowid"],) for r in cut])
                applied.append(f"{len(cut)} free onward seats sold elsewhere")
            if p["gate_closed"]:
                if con.execute("UPDATE gates SET occupied_by = 'CLOSED' WHERE id = ? AND occupied_by IS NULL", (p["gate_closed"],)).rowcount:
                    applied.append(f"gate {p['gate_closed']} closed")
            if p["mx_signed"]:
                con.execute("UPDATE maintenance SET status = 'SIGNED', signed_by = 'what-if' WHERE tail = ? AND severity = 'NO-GO'", (inb["tail"],))
                con.execute("UPDATE tasks SET status = 'DONE' WHERE flight_id = ? AND id = 'MX_CHECK'", (fid,))
                applied.append("NO-GO maintenance assumed signed off")
            inb = S._inbound(con, fid)
            out = S._get(con, "flights", inb["next_leg"], "flight")
            after = _pick(S._kpis(con, fid))
            crew = [{"id": c["id"], "role": c["role"], "margin": S.MAX - G.projected_fdp(c, out),
                     "legal_standby": [s["id"] for s in S._standby_options(con, c, out)]} for c in S._rotation(con, inb)]
            plan = {"actions": actions, "ok": None, "reason": "", "kpis": None}
            if actions:
                try:
                    for a in actions:
                        S._apply(con, fid, a)
                    plan.update(ok=True, kpis=_pick(S._kpis(con, fid)))
                except Exception as e:  # noqa: BLE001
                    plan.update(ok=False, reason=str(e))
        finally:
            con.execute("ROLLBACK")  # a what-if never persists
    findings = [f"No legal standby {c['role']} for {c['id']}: the outbound cannot depart legally until one is found"
                for c in crew if c["margin"] < 0 and not c["legal_standby"]]
    if plan["ok"] is False:
        findings.append(f"The tested recovery plan is refused by the guards: {plan['reason']}")
    if plan["ok"] and plan["kpis"]["vulnerable_at_risk"]:
        findings.append(f"{plan['kpis']['vulnerable_at_risk']} vulnerable passengers still have no onward seat after the plan (no capacity)")
    if plan["ok"] and plan["kpis"]["bags_rush"]:
        findings.append(f"{plan['kpis']['bags_rush']} bags rush-tagged: their passenger makes the flight but the bag may not")
    if after["bags_short_connection"] > base["bags_short_connection"]:
        findings.append(f"{after['bags_short_connection'] - base['bags_short_connection']} more bags fall below the bag minimum connection time")
    res = {"params": p, "applied": applied, "baseline": base, "after": after, "findings": findings,
           "delta": {k: (after[k] - base[k]) if isinstance(after[k], (int, float)) and isinstance(base[k], (int, float)) else None for k in KPI_KEYS},
           "crew": crew, "crew_without_legal_standby": [c["id"] for c in crew if c["margin"] < 0 and not c["legal_standby"]], "plan": plan}
    if narrate:
        try:
            res["narrative"] = agent.explain("what_if", {k: res[k] for k in ("findings", "applied", "baseline", "after", "delta", "plan")})
        except agent.LLMUnavailable as e:
            res["narrative"] = {"source": "unavailable", "summary": "", "risks": [], "error": str(e)}
    return res


COMPARE_KEYS = (("at_risk", "min"), ("vulnerable_at_risk", "min"), ("network_delay_added", "min"), ("total_cost_eur", "min"),
                ("crew_min_margin", "max"), ("turn_gap", "min"), ("bags_rush", "min"), ("protected", "max"))


def compare(fid, priorities, regenerate=False, narrate=True):
    lab = agent.scenario_lab(fid, priorities, regenerate)
    ok = [s for s in lab["scenarios"] if s["ok"]]
    best = {k: (min if d == "min" else max)(s["kpis"][k] for s in ok) for k, d in COMPARE_KEYS} if ok else {}
    rows = [{"title": s["title"], "why": s["why"], "actions": s["actions"], "ok": s["ok"], "reason": s.get("reason", ""), "score": s["score"],
             "revised_from": s.get("revised_from"),
             "kpis": s["kpis"],
             "best": [k for k, _ in COMPARE_KEYS if s["ok"] and s["kpis"][k] == best.get(k)]} for s in lab["scenarios"]]
    res = {"priorities": lab["priorities"], "scenario_source": lab["source"], "rows": rows, "recommended": rows[0]["title"] if rows and rows[0]["ok"] else None}
    if not narrate:
        return res
    try:
        res["narrative"] = agent.explain("scenario_comparison", {"priorities": lab["priorities"], "ranked_by_code": [
            {**{k: r[k] for k in ("title", "ok", "reason", "score")}, "kpis": {k: r["kpis"][k] for k, _ in COMPARE_KEYS} if r["ok"] else None}
            for r in rows]})
    except agent.LLMUnavailable as e:
        res["narrative"] = {"source": "unavailable", "summary": "", "risks": [], "error": str(e)}
    return res
