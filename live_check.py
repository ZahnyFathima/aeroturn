"""Live end-to-end check against the REAL LLM (uses OPENROUTER_API_KEY; costs well under $0.01).
Runs on a throwaway database, so the demo database is untouched.   Usage: python live_check.py"""
import os
import tempfile

os.environ["AEROTURN_DB"] = os.path.join(tempfile.mkdtemp(), "live.sqlite")
import agent  # noqa: E402
import ledger  # noqa: E402
import redteam  # noqa: E402
import seed  # noqa: E402
import services as S  # noqa: E402
import whatif  # noqa: E402

seed.build(S.DB)
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'} {name} {detail}")


rep = agent.get_report("EK812", force=True)
check("live report written by the LLM", rep["source"].startswith("LLM"), rep["source"])
check("LLM scenarios survive the whitelist", len(rep["suggestions"]) == 3, f"dropped={rep['dropped_actions']}")

blocked = []
for call in ledger.DEMO_CALLS:
    cid = ledger.start_call("EK812", call["title"], "simulated")["call_id"]
    for who, role, txt in call["lines"]:
        ledger.add_line(cid, who, ledger.resolve(txt), role)
    blocked += [a["reason"].split(":")[0] for a in ledger.extract(cid)["alerts"]]
check("ledger: illegal spoken proposals blocked live", {"CREW DUTY", "PRIORITY"} <= set(blocked), str(sorted(set(blocked))))
check("ledger: every quote comes from the transcript", all(i["quote"] for i in ledger.view("EK812")["items"]))
check("ledger report", bool(ledger.report("EK812")["executive_summary"]))
import decisions  # noqa: E402
blk = next(d for d in decisions.list_decisions("EK812") if d["status_code"] == "BLOCKED")
rep = decisions.report(blk["id"])
check("per-decision report: AI steps + code compliance", rep["source"].startswith("LLM") and rep["next_steps"] and rep["guard"]["code"] in {g for g in
      ("CREW_FDP_EXCEEDED", "VULNERABLE_PRIORITY", "MX_SIGNOFF_MISSING")}, rep["guard"]["code"])

rt = redteam.run("EK812")
check("red team: nothing committed, state unchanged", rt["state_unchanged"] and rt["committed"] == 0,
      str({k: sum(a["stopped_by"] == k for a in rt["attacks"]) for k in ("SCHEMA", "GUARD", "HITL")}))

wi = whatif.run("EK812", {"extra_delay": 100}, [{"kind": "REBOOK_AT_RISK", "flight_id": "EK812"}])
check("what-if narrated by the LLM", wi["narrative"]["source"].startswith("LLM"))
S.execute_atomic_passenger_rebook("EK812")
n = agent.draft_passenger_notifications("EK812")
check("passenger notifications drafted by the LLM", n["source"].startswith("LLM") and n["missing"] == 0, f"{len(n['groups'])} groups")
print(f"\n{sum(results)}/{len(results)} live checks passed")
