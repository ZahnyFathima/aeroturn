"""Scripted OCC calls for the Decision Ledger demo. These are only the INPUTS (who said what): nothing here is an extracted
issue, fact, decision or action. The LLM extracts those; the Python guards judge every decision.
Two calls are pre-loaded by seed.py as already-ended calls; the maintenance call is played live during the demo.
"{+N}" in a line is replaced with the wall-clock time N minutes from when the line is used (so deadlines line up with timestamps).
Two calls contain illegal suggestions (standard-first rebooking, extending crew duty) that the guards must BLOCK."""
from datetime import datetime, timedelta
import re

CONTROLLER = ("Sara Becker", "Operations Controller")
DEMO_CALLS = [
    {"id": "crew", "title": "Crew duty limit and reserve crew", "preload": True, "starts_min_ago": 30, "lines": [
        (*CONTROLLER, "Crew control, where are we on the EK812 crew for the EK813 turn?"),
        ("Tom Larsen", "Crew Control", "They will be at twelve hours forty of duty on block. The legal limit is thirteen hours, so they cannot operate EK813."),
        ("Tom Larsen", "Crew Control", "Reserve captain SB-01 is in crew room T1, twelve minutes away, and we have a full reserve crew rated on the B777."),
        (*CONTROLLER, "Decision: call out the reserve crew for EK813 and release the inbound crew to rest."),
        ("Priya Haddad", "Customer Recovery", "Forty-five connections are at risk. To save seats, let's rebook the standard passengers first and put the minors and wheelchair passengers on the evening flights."),
        (*CONTROLLER, "No. Vulnerable passengers are protected. Decision: rebook every at-risk passenger, unaccompanied minors and PRMs first."),
        ("Priya Haddad", "Customer Recovery", "Understood. Rebooking and passenger notifications will be done by {+15}."),
        ("Tom Larsen", "Crew Control", "Call-out goes now. The reserve crew will report at the aircraft by {+45}."),
    ]},
    {"id": "captain", "title": "Captain's request to extend duty", "preload": True, "starts_min_ago": 16, "lines": [
        ("Capt. Yusuf Khan", "Captain EK812", "OCC, Captain EK812. We're on block. My crew is happy to take EK813 back to Dubai, we're only twenty minutes short of the limit."),
        ("Capt. Yusuf Khan", "Captain EK812", "Let's just extend our duty and release EK813 with the current crew, it saves the whole reserve call-out."),
        (*CONTROLLER, "Captain, negative on the extension. Duty limits are not negotiable, crew control is already calling out the reserves."),
        ("Capt. Yusuf Khan", "Captain EK812", "Copy. Also, hydraulic system B pressure fluctuated on descent again. It's written up in the tech log."),
        (*CONTROLLER, "Thanks, Captain. Maintenance will put a licensed engineer on it before any release."),
    ]},
    {"id": "mx", "title": "Hydraulic inspection sign-off", "preload": False, "lines": [
        (*CONTROLLER, "Maintenance, what's the status of the EK812 hydraulic item?"),
        ("Elena Novak", "Maintenance Control", "Item M-1, hydraulic system B pressure fluctuation, is still open. It's a NO-GO item and needs a licensed engineer sign-off."),
        ("Elena Novak", "Maintenance Control", "I'm assigning engineer E1, K. Weber, to M-1."),
        ("K. Weber", "Certifying Engineer", "Weber here, at line ops T1. I can be at the aircraft in ten minutes and the inspection should be finished by {+45}."),
        (*CONTROLLER, "Decision: we hold EK813 until the sign-off is recorded. Retime EK813 by sixty minutes."),
        ("Elena Novak", "Maintenance Control", "EK813 is on remote stand R4 and the ambulift is late for the PRMs. Gate B12 is a free contact gate."),
        (*CONTROLLER, "Then move EK813 to gate B12. Hub control, confirm engineer access to the aircraft by {+10}."),
    ]},
]


def resolve(text, now=None):
    """Fill "{+N}" placeholders with HH:MM, N minutes after `now`."""
    now = now or datetime.now()
    return re.sub(r"\{\+(\d+)\}", lambda m: (now + timedelta(minutes=int(m[1]))).strftime("%H:%M"), text)
