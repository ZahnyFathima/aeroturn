import sqlite3
from contextlib import closing

import pytest

import agent
import seed
import services as S
from guards import GuardViolation

PIN = "2468"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DB", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(S, "_pin_failures", 0)
    monkeypatch.setenv("OCC_CONTROLLER_PIN", PIN)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # tests never call the real LLM
    seed.build(S.DB)


def snapshot():
    with closing(S.connect()) as con:
        return S.rows(con, "SELECT * FROM seats ORDER BY flight_id, seat_no"), S.rows(con, "SELECT * FROM bags ORDER BY id")


def state():
    return S.get_turnaround_and_crew_state("EK812")


def decide(actions, chaos=False):
    return S.decide("EK812", "test", actions, "Ops Ctl", PIN, chaos)


def test_scenario_shape():
    assert len(S.list_flights()) == 11
    d = state()
    k = d["kpis"]
    assert (k["connecting"], k["at_risk"], k["vulnerable_at_risk"], k["crew_illegal"]) == (280, 45, 12, 6)
    assert d["limits"]["MAX_FDP_MIN"] - max(c["duty_used"] for c in d["crew"]) == 20
    assert d["crew"][0]["margin"] <= d["crew"][-1]["margin"]  # most urgent first


def test_midway_seat_conflict_rolls_back_everything():
    before = snapshot()
    r = S.execute_atomic_passenger_rebook("EK812", chaos=True)
    assert not r["ok"] and "Seat conflict" in r["error"]
    assert snapshot() == before and r["integrity"] == {"double_bookings": 0, "orphan_bags": 0}


def test_whole_decision_is_atomic():
    r = decide([{"kind": "DELAY", "flight_id": "LH400", "minutes": 15}, {"kind": "REBOOK_AT_RISK", "flight_id": "EK812"}], chaos=True)
    assert r["status"] == "ROLLED_BACK"
    assert next(f for f in S.list_flights() if f["id"] == "EK812")["at_risk"] == 45  # the hold was undone too
    with closing(S.connect()) as con:
        assert con.execute("SELECT delay_min FROM flights WHERE id = 'LH400'").fetchone()[0] == 0


def test_commit_protects_vulnerable_first():
    r = S.execute_atomic_passenger_rebook("EK812")
    assert r["ok"] and r["integrity"] == {"double_bookings": 0, "orphan_bags": 0}
    assert all(p["tier"] == "STANDARD" for p in state()["passengers"] if p["at_risk"])


def test_batch_that_skips_a_vulnerable_flyer_is_refused():
    d = state()
    std = [{"pax_id": p["id"], "to": p["options"][0]["id"]} for p in d["passengers"] if p["at_risk"] and p["tier"] == "STANDARD" and p["options"]]
    r = S.execute_atomic_passenger_rebook("EK812", std)
    assert not r["ok"] and "PRIORITY" in r["error"]


def test_crew_duty_guard_blocks_departure():
    r = S.stage_controller_schedule_change("EK812", "release", [{"kind": "DEPARTURE_RELEASE", "flight_id": "EK813"}])
    assert r["status"] == "BLOCKED" and "CREW DUTY" in r["reason"]


def test_maintenance_guard_then_full_legal_release():
    assert decide([{"kind": "CREW_AUTOSWAP", "flight_id": "EK812"}])["status"] == "APPROVED"
    assert state()["kpis"]["crew_illegal"] == 0
    r = S.stage_controller_schedule_change("EK812", "release", [{"kind": "DEPARTURE_RELEASE", "flight_id": "EK813"}])
    assert r["status"] == "BLOCKED" and "MAINTENANCE" in r["reason"]
    with pytest.raises(PermissionError):
        S.sign_off_maintenance("M-1", "B1-20417")  # not assigned yet
    assert decide([{"kind": "ASSIGN_ENGINEER", "item_id": "M-1", "engineer_id": "E1"}])["status"] == "APPROVED"
    with pytest.raises(PermissionError):
        S.sign_off_maintenance("M-1", "B1-31882")  # another engineer's licence
    S.sign_off_maintenance("M-1", "B1-20417")
    assert decide([{"kind": "DEPARTURE_RELEASE", "flight_id": "EK813"}])["status"] == "APPROVED"
    assert state()["outbound"]["status"] == "RELEASED"


def test_illegal_crew_swaps_blocked():
    swap = lambda m, s: S.stage_controller_schedule_change("EK812", "s", [{"kind": "CREW_SWAP", "crew_id": m, "standby_id": s}])["status"]  # noqa: E731
    assert [swap("EK812-1", s) for s in ("SB-02", "SB-03", "SB-11")] == ["BLOCKED"] * 3  # too far, wrong role, off duty
    assert swap("EK812-2", "SB-04") == "BLOCKED"  # duty limit
    assert swap("EK812-1", "SB-01") == "PENDING"
    options = {c["role"]: [o["id"] for o in c["options"]] for c in state()["crew"]}  # standby matcher, closest first
    assert options["CPT"] == ["SB-01"] and options["FO"] == ["SB-03"] and options["CC"] == ["SB-07", "SB-09", "SB-08"]


def test_gate_change_needs_controller_pin():
    with pytest.raises(PermissionError):
        S.decide("EK812", "gate", [{"kind": "GATE_CHANGE", "flight_id": "EK813", "gate": "A7"}], "Ops Ctl", "0000")
    r = S.stage_controller_schedule_change("EK812", "gate", [{"kind": "GATE_CHANGE", "flight_id": "EK813", "gate": "A7"}])
    assert r["status"] == "PENDING" and state()["outbound"]["gate"] == "R4"
    with pytest.raises(PermissionError):
        S.approve_change(r["id"], "Ops Ctl", "0000")
    assert state()["outbound"]["gate"] == "R4"
    assert S.approve_change(r["id"], "Ops Ctl", PIN)["status"] == "APPROVED"
    d = state()
    assert d["outbound"]["gate"] == "A7" and any("A7" in n["message"] for n in d["notifications"])


def test_pin_lockout():
    for _ in range(5):
        with pytest.raises(PermissionError):
            S.decide("EK812", "x", [{"kind": "DELAY", "flight_id": "EK813", "minutes": 5}], "x", "bad")
    with pytest.raises(PermissionError, match="locked"):
        decide([{"kind": "DELAY", "flight_id": "EK813", "minutes": 5}])


def test_audit_log_is_append_only():
    decide([{"kind": "DELAY", "flight_id": "EK813", "minutes": 85}])
    with closing(S.connect()) as con, pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM audit")


def test_llm_output_is_sanitised():
    plan = agent.validate({"report": "<script>x</script>![a](http://evil/x.png) ok", "suggestions": [{"title": "<b>t</b>", "actions": [
        {"kind": "DROP_TABLE"}, {"kind": "GATE_CHANGE", "flight_id": "ek813", "gate": "A7"},
        {"kind": "DELAY", "flight_id": "EK813; DROP TABLE seats", "minutes": 5}, "junk"]}]})
    assert [a["kind"] for a in plan["suggestions"][0]["actions"]] == ["GATE_CHANGE"] and plan["dropped_actions"] == 3
    assert "<" not in plan["report"] + plan["suggestions"][0]["title"] and "evil" not in plan["report"]


def test_report_and_lab_work_offline_and_pass_guards():
    rep = agent.get_report("EK812")
    assert "Rule-based" in rep["source"] and len(rep["suggestions"]) == 3 and "Ramp control" in rep["report"]
    lab = agent.scenario_lab("EK812", {"connections": True})
    assert all(s["ok"] and s["kpis"]["vulnerable_at_risk"] == 0 for s in lab["scenarios"])
    assert lab["scenarios"][0]["kpis"]["at_risk"] <= lab["scenarios"][-1]["kpis"]["at_risk"]
    S.log_call("EK812", "Ramp control", "Ambulift now on stand")
    assert S.cached_report("EK812") is None  # new call invalidates the live report


def test_stretch_tools():
    cp = state()["turnaround"]
    assert cp["path"] == ["DEBOARD", "FUEL", "BOARDING", "DOORS"] and cp["off_block"] == 100
    before = state()["kpis"]
    S.execute_atomic_passenger_rebook("EK812")
    after = state()["kpis"]
    assert after["pax_cost_eur"] < before["pax_cost_eur"] == before["baseline_eur"] and after["protected"] > 0
