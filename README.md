# AeroTurn: Turnaround and Atomic Disruption Recovery OCC

AeroTurn is a control-room console for the Frankfurt (FRA) hub. It tracks 11 inbound wide-body and narrow-body flights from around the world. The headline crisis is EK812 (DXB→FRA):
- It arrives 75 minutes late.
- 45 of its 280 connecting passengers are at risk, and 12 of those are vulnerable.
- The crew is 20 minutes from the legal duty limit.
- A NO-GO hydraulic item is still open.

| Who | Does what | Where |
|---|---|---|
| 🤖 **LLM** (Gemini 2.5 Flash Lite via OpenRouter) | Writes the live situation report from call transcripts and live data. Proposes 3 alternative recovery scenarios. | `agent.py` |
| 🛡️ **Python guards** | Crew duty ceiling, role and type rating, standby proximity, NO-GO maintenance sign-off, vulnerable-passenger priority. These are immutable, and the LLM cannot reach them. | `guards.py` |
| 🛡️ **Python tools** | `get_turnaround_and_crew_state()`, `execute_atomic_passenger_rebook()`, `stage_controller_schedule_change()`, seat allocation, standby matcher, critical path, EU261 cost, dry-run simulator, scenario ranking. | `services.py` |
| 👤 **Operations Controller** | Every decision needs a name and PIN. It is applied atomically, written to the decision log, and sent as notifications to the affected staff. | `decide()` / `approve_change()` |

## Setup
```bash
python -m venv .venv && .venv\Scripts\activate      # recommended
pip install -r requirements.txt                      # only requests, python-dotenv, pytest
copy .env.example .env                               # set OPENROUTER_API_KEY and OCC_CONTROLLER_PIN
pytest                                               # 28 offline safety tests (LLM mocked)
python live_check.py                                 # 8 live checks against the real LLM (< $0.01)
python server.py                                     # open http://127.0.0.1:8000
```
- The server uses only the Python standard library and listens on localhost only.
- Without an API key, a rule-based fallback writes the report and the scenarios, so the demo also works offline.
- Live call transcription uses the browser's speech recognition (Chrome or Edge). In Chrome, the audio is sent to Google's speech service. You can also type the call instead.

## UI flow
1. **Network map**: inbound planes are grey when on time and get deeper red the longer they are delayed. The delay filter on the right controls both the map and the table below it. Click a plane or a row to open that flight.
2. **Tabs** above the map:
   - **Crew**: pick a flight to see each crew member, most urgent first. The *Replacement / standby* dropdown lists only standby crew the guards allow.
   - **Connecting passengers**: rebook one passenger, or all at-risk passengers in one atomic batch.
   - **Maintenance**: assign an engineer, then the engineer signs off with their licence number.
   - **Decision log**: every decision and every notification sent.
3. **Live report** (`#/flight/EK812`): the AI report built from the calls, then the stats, then the top 3 agent suggestions. Below those are the call recorder, the turnaround critical path, and the latest decisions.
4. **Scenario Lab** (`#/lab/EK812`): toggle what to prioritise. Vulnerable-passenger protection is locked on. Each scenario is dry-run against the real guards inside a transaction that is rolled back, then ranked by code. Choose one, enter the PIN, and it is applied atomically. The decision log and report then update, and the affected staff are notified.

## Demo script (about 4 minutes)
1. **Map**: EK812 is dark red. Click it.
2. **Report**: the report quotes the captain, crew control, transfer desk and ramp calls. Record or type a new call; the report regenerates.
3. **Guards block departure**: on the report page, click *🛫 Propose departure release*. EK813 is **BLOCKED (crew duty)**. After the crew swap it is **BLOCKED (maintenance)**, until engineer E1 is assigned and signs off M-1 with licence `B1-20417`.
4. **Atomic rollback**: in the Lab, turn on *💥 Inject mid-way seat conflict* and choose a scenario. It shows **ROLLED BACK**: the holds, crew swaps and gate change are all undone, with 0 double bookings and 0 orphaned bags. Turn chaos off and choose again to get **APPROVED**.
5. **Vulnerable priority**: on the Connecting passengers tab, UM, PRM and MEDICAL passengers are listed first and rebooked first. A batch that skips one is refused (`PRIORITY`).
6. **Controller approves a gate change**: the chosen scenario moves EK813 from remote stand R4 to contact gate A7, which needs the name and PIN. The bell shows ramp, gate and PRM staff notified.

## Safety guarantees
- **Atomic state**: a decision's actions (holds, crew swaps, gate change, rebooking) run in one `BEGIN IMMEDIATE … COMMIT` transaction. Any failure triggers a `ROLLBACK` of all of them. The schema itself makes double booking impossible: `seats.pax_id UNIQUE` and `PRIMARY KEY(flight_id, seat_no)`. An integrity check runs before every commit.
- **Immutable floors**: the limits live in a read-only `MappingProxyType`. Guards run inside `_apply()`, the single write path, so nothing can skip them. The dropdowns only show options that already pass the guards.
- **HITL**: a decision needs the controller PIN (compared in constant time, locked after 5 failures). A maintenance sign-off needs the assigned engineer's own licence.
- **LLM containment**: the LLM gets a summary with no passenger names. Its actions are filtered against a whitelist and dry-run before anyone can choose them. Its text is stripped of HTML and links. It has no database access and no tool calls.
- **Web**: the server binds to 127.0.0.1 only and accepts JSON POSTs from the same origin only, up to 64 KB. Responses carry CSP, nosniff and no-store headers. The frontend escapes all text. There is no path traversal (a fixed file map is used). The audit log is append-only (an SQLite trigger blocks changes). The API key is read only from the project `.env`.

## Extra features (inside the main console)
- **Menu › Decision log**:
  - **Calls column:** compact call cards (participants, time, Live/Ended, counts, no summaries) with **View (live) transcript**. The drawer is searchable and tagged, with jump to latest and Esc to close.
  - **Key details:** issues, who was on the call, facts and constraints, action items (owner, due, tick when done) and the call's decisions. Every timestamp chip opens the exact transcript line.
  - **Decision log:** one tab per decision, filterable by status, call and issue type. Each shows the status pipeline (Proposed → Guard-checked → Awaiting controller approval → Approved → Executed, or Blocked), the guard reason code and the smallest legal fix computed by code, plus evidence, AI confidence, human confirmation and **View report**.
  - **Per-decision report:** compliance and impact are system-verified; problem, next steps and risks are AI-suggested. It has Print/PDF and Copy as text.
  - **Approval flow:** confirming a decision only sends it to the **controller approval queue**; approval happens there (name + PIN).
  - **Demo calls:** two calls are pre-loaded and analysed by the AI on first view; the maintenance call is played live.
- **Menu › Connecting passengers** (Passenger Impact Agent, `pax_impact.py`, deterministic and explainable):
  - **Summary:** a compact disruption summary (route, scheduled vs estimated arrival, delay and its stated reason/source, affected passengers and connections, active/recovering/resolved status, data freshness).
  - **Table:** a risk table sorted Critical first; each label (Critical/High/Medium/Low) comes with its factors (connection time vs MCT, departure countdown, hold feasibility within the 30-min cap, alternative seats, assistance, bags).
  - **Interaction:** filters, sorting, group by destination or connecting flight, and expandable rows (itinerary + options).
  - **Hand-off only:** **Send to Rebooking Recommendations** stages a PENDING recommendation in the controller approval queue and nothing on this tab books anything. "Rebooked" appears only when the audit log records a human-approved booking.
  - **Privacy:** names and assistance details are masked; revealing them needs the controller PIN and is audited.
  - **Data labels:** data is labelled SIMULATED / PREDICTED; MCT is the FRA hub rule (no itinerary-specific MCT table is configured, and the tab says so).
- **Scenario Lab**: side-by-side **recovery comparison** and the **what-if simulator** under the scenario cards.
- **Click any AI option** (live report suggestions or Scenario Lab scenarios): its **Passengers & bags** panel opens. It shows who moves where and what happens to their bags, lets you apply the option (optionally injecting a seat or baggage failure), and drafts passenger notifications.
- **Menu › Guard Attack Mode** and **Menu › Audit replay**: their own tabs.

| Feature | 🤖 LLM does | 🛡️ Code does | Files |
|---|---|---|---|
| **Decision Ledger** | Transcribes recorded or uploaded call audio into speaker turns. Extracts issues, decisions and actions with a line number, confidence and priority. A second pass finds every proposal and maps it to actions. Writes the report. | Stores lines and timestamps. Fills every quote and time from the referenced line, and drops items without evidence. Checks who really spoke the line and lowers the confidence if it doesn't match. Dry-runs every proposal through the guards and raises a **live alert** if one is illegal. Requires a human confirmation, then the controller PIN, to execute. Report statuses and next steps are derived from item statuses; illegal steps are never recommended. | `ledger.py`, `demo_calls.py` |
| **Guard Attack Mode** | Plays a reckless planner and invents bypass attempts plus a prompt-injection call. | Runs each attempt through the schema whitelist, then the guards (rolled-back dry run), then the HITL gate. Probes that the guard constants can't be changed at runtime. Proves via state hash that nothing changed. | `redteam.py` |
| **What-if simulator** | Explains the result. | Applies extra delay, sick standby crew, sold seats, closed gate or assumed sign-off, plus an optional plan, inside a transaction that is always rolled back. Computes every KPI and the authoritative findings. | `whatif.py` |
| **Recovery comparison** | Proposes the scenarios and explains the trade-offs. | Dry-runs each scenario, measures and ranks them, and marks the best value per KPI. Applying one needs the PIN. | `whatif.py` |
| **Baggage-aware rebooking** | n/a | Bag MCT is 60 min (+15 on a remote stand). The planner prefers flights that both the passenger and the bags can make, otherwise it rush-tags the bag. A positive passenger–bag match guard runs before commit. A mid-batch baggage-system failure rolls back the whole batch. | `services.py`, `guards.py` |
| **Passenger notifications** | Writes tier-aware messages per group and **never sees a name**. | Groups passengers and fills in names locally. Sending needs the PIN. | `agent.py` |
| **Audit replay** | n/a | Rebuilds the seed and re-applies every committed audit event, checking the state hash at each step. Any out-of-band change makes it fail. | `replay.py` |

New guard floors in `guards.py`: an onward-hold cap of 30 min, bag MCT, and passenger–bag match. Vulnerable priority now refuses a batch only when someone of *lower* priority got a seat on that route, so a capacity shortage no longer blocks everyone. Features that need the LLM return HTTP 503 rather than faking output when it is unavailable.

## Deploying

No build step and no extra dependencies: the app is the Python standard-library server plus `requests` and `python-dotenv`.
Set these environment variables in the platform dashboard (never commit `.env`):

| Variable | Required | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | LLM features return a clear 503 without it; nothing is faked |
| `OCC_CONTROLLER_PIN` | yes | approvals are refused while it is empty |
| `OPENROUTER_MODEL` | no | defaults to `google/gemini-2.5-flash-lite` |
| `AEROTURN_DB` | no | SQLite path; see each platform below |

### Railway (recommended for demos)
1. Push this folder to GitHub, then **New Project → Deploy from GitHub repo**.
2. Add the variables above. `railway.json` sets the start command (`python server.py`) and a health check on `/api/flights`.
3. **Settings → Networking → Generate Domain.** Railway sets `PORT` and `RAILWAY_ENVIRONMENT`, so the server listens on `0.0.0.0` automatically.
4. The demo database is seeded on first start. The filesystem resets on each redeploy; to keep state, attach a Volume at `/data` and set `AEROTURN_DB=/data/aeroturn.sqlite`.

### Vercel
1. Push to GitHub, then **Add New → Project** and import the repo. Framework preset: **Other**; leave build settings empty.
2. Add the variables above (do not set `AEROTURN_DB`).
3. `vercel.json` routes every URL to the serverless function `api/index.py`, which runs the same request handler as `server.py`.

Limits on Vercel (serverless, not a long-running server):
- The SQLite database lives in `/tmp` and is re-seeded on each cold start. Approvals and calls can reset after idle time, and separate instances do not share state. Use Railway when the demo needs state to persist.
- Request bodies are capped at 4.5 MB, so audio uploads longer than about 3 minutes fail. Typed and recorded short calls work.
- `maxDuration` is 300 s, which covers the slowest LLM calls.
