"""Connecting Passengers tab (Passenger Impact Agent): explainable labels, privacy, hand-off only, audit-backed 'Rebooked'."""
import json
from contextlib import closing

import pytest

import pax_impact as P
import seed
import services as S

PIN = "2468"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DB", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(S, "_pin_failures", 0)
    monkeypatch.setenv("OCC_CONTROLLER_PIN", PIN)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    seed.build(S.DB)


def hash_now():
    with closing(S.connect()) as con:
        return S.state_hash(con)


def test_summary_labels_and_explanations():
    r = P.analyse("EK812")
    s = r["summary"]
    assert (s["affected_passengers"], s["affected_connections"], s["status"], s["delay_min"]) == (45, 3, "active", 75)
    assert s["delay_reason"] and s["reason_source"] == "Captain EK812 call"
    assert r["passengers"][0]["priority"] == "Critical"  # sorted by priority by default
    for p in r["passengers"]:
        if p["status"] == "Likely missed":
            assert p["priority"] in ("Critical", "High") and any(f"vs {p['mct_min']} min MCT" in f for f in p["factors"])
    vul = [p for p in r["passengers"] if p["assistance"] and p["status"] == "Likely missed"]
    assert vul and all(p["priority"] == "Critical" and "protected-priority" in p["priority_reason"] for p in vul)
    lhr_std = [p for p in r["passengers"] if p["connecting_flight"] == "LH900" and not p["assistance"]]
    assert all(p["priority"] == "High" and any("cannot be saved by a hold" in f for f in p["factors"]) for p in lhr_std)
    assert r["data"]["source"].startswith("SIMULATED") and "predicted" in r["data"]


def test_unknown_reason_is_not_guessed():
    assert P.analyse("UA960")["summary"]["delay_reason"] is None  # on time: nothing reported, nothing invented


def test_personal_data_is_masked_and_reveal_is_audited():
    with closing(S.connect()) as con:
        names = {r["name"] for r in S.rows(con, "SELECT name FROM passengers")}
    blob = json.dumps(P.analyse("EK812"))
    assert not any(n in blob for n in names) and "PRM" not in blob and "MEDICAL" not in blob
    with pytest.raises(PermissionError):
        P.reveal("P0248", "Ops", "0000")
    r = P.reveal("P0248", "Ops Ctl", PIN)
    assert r["name"] in names and r["assistance"] != "None recorded"
    with closing(S.connect()) as con:
        assert con.execute("SELECT COUNT(*) FROM audit WHERE action = 'PII_VIEWED'").fetchone()[0] == 1


def test_handoff_never_books_and_rebooked_needs_human_approval():
    before = hash_now()
    crit = [p["ref"] for p in P.analyse("EK812")["passengers"] if p["connecting_flight"] == "LH900"]
    with pytest.raises(PermissionError):
        P.recommend("EK812", crit, "")
    r = P.recommend("EK812", crit, "Desk agent")
    assert [s["status"] for s in r["staged"]] == ["PENDING"] and hash_now() == before  # nothing booked
    rows = {p["ref"]: p for p in P.analyse("EK812")["passengers"]}
    assert all(rows[x]["pending"] and rows[x]["status"] == "Likely missed" for x in crit)
    assert P.recommend("EK812", crit[:1], "Desk agent")["skipped"][0]["reason"].startswith("already in the approval queue")
    S.approve_change(r["staged"][0]["id"], "Ops Ctl", PIN)
    rows = {p["ref"]: p for p in P.analyse("EK812")["passengers"]}
    assert all(rows[x]["status"] == "Rebooked" and rows[x]["priority"] == "Low" for x in crit)
    assert "approved by Ops Ctl" in rows[crit[0]]["factors"][0]
    assert P.analyse("EK812")["summary"]["status"] == "recovering"


def test_standard_only_batch_is_refused_while_vulnerable_wait():
    std = [p["ref"] for p in P.analyse("EK812")["passengers"] if p["connecting_flight"] == "LH900" and not p["assistance"]]
    r = P.recommend("EK812", std, "Desk agent")
    assert r["staged"][0]["status"] == "BLOCKED" and "PRIORITY" in r["staged"][0]["reason"]


def test_move_without_approval_is_flagged_not_shown_as_rebooked():
    S.execute_atomic_passenger_rebook("EK812")  # system path with no named controller approval
    r = P.analyse("EK812")
    moved = [p for p in r["passengers"] if p["missing"]]
    assert moved and all(p["status"] != "Rebooked" for p in moved) and r["data"]["conflicts"]
