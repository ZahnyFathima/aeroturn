// Connecting Passengers tab, powered by the Passenger Impact Agent (pax_impact.py).
// Read-only analysis + a recommendation hand-off. Nothing on this tab books, buys, modifies or issues a ticket:
// "Send to Rebooking Recommendations" stages a PENDING recommendation that a controller approves or rejects (with PIN).
// Uses app.js helpers at call time (h, api, toast, askAuth, S). Every server string is escaped with h().
const PaxImpact = (() => {
  const q = (s, el = document) => el.querySelector(s);
  const P = { root: null, data: null, sel: new Set(), open: null, view: "table", showLow: false, sort: { key: "priority", dir: 1 },
    f: { priority: "", status: "", flight: "", dest: "", assist: "", dep: "" }, revealed: {}, override: {}, timer: null };
  let me = "";
  try { me = localStorage.getItem("aeroturn.me") || ""; } catch { /* private mode */ }
  const PICON = { Critical: "◆", High: "▲", Medium: "●", Low: "○", Unknown: "?" };
  const PRANK = { Critical: 0, High: 1, Medium: 2, Low: 3, Unknown: 4 };
  const prio = (p) => `<span class="px-p ${h(p)}">${PICON[p] || ""} ${h(p)}</span>`;
  const SCLS = { "Confirmed missed": "miss", "Likely missed": "miss", "At risk": "risk", "Safe connection": "ok", Rebooked: "ok" };
  const MCT = '<abbr title="Minimum connection time: the shortest time a passenger needs between arriving and the onward departure">MCT</abbr>';

  async function mount(el) {
    P.root = el;
    el.innerHTML = '<div class="dl-skel"></div><div class="dl-skel"></div><div class="dl-skel"></div>';
    el.onclick = onClick; el.onchange = onChange;
    clearInterval(P.timer);
    P.timer = setInterval(tick, 1000);
    await load();
  }
  async function load() {
    try { P.data = await api(`/api/pax/${S.flight}`); }
    catch (e) {
      P.root.innerHTML = `<div class="card"><p class="bad">The Passenger Impact Agent could not load passenger data for ${h(S.flight)}: ${h(e.message)}</p>
        <p class="small muted">No values are shown rather than guessing.</p><button class="ghost" data-retry="1">Retry</button></div>`;
      return;
    }
    const known = new Set(P.data.passengers.map((p) => p.ref));
    P.sel = new Set([...P.sel].filter((x) => known.has(x)));
    render();
  }

  // ---------------- render ----------------
  function render() {
    const d = P.data, s = d.summary, dq = d.data;
    const flights = [...new Set(d.passengers.map((p) => p.connecting_flight))].sort(), dests = [...new Set(d.passengers.map((p) => p.final_destination))].sort();
    const low = d.passengers.filter((p) => p.priority === "Low").length;
    const opt = (k, label, vals) => `<label class="small muted">${label} <select data-f="${k}"><option value="">All</option>${vals.map(([v, l]) =>
      `<option value="${h(v)}" ${P.f[k] === v ? "selected" : ""}>${h(l)}</option>`).join("")}</select></label>`;
    P.root.innerHTML = `
      <section class="px-sum" aria-label="Disruption summary">
        <div class="it"><span>Inbound</span><b>${h(s.flight)}</b><small class="muted">${h(s.route)}</small></div>
        <div class="it"><span>Arrival</span><b>${h(s.estimated_arrival)} <small class="muted">est.</small></b><small class="muted">scheduled ${h(s.scheduled_arrival)} <span class="px-lbl">PREDICTED</span></small></div>
        <div class="it"><span>Delay</span><b>${s.delay_min ? `+${s.delay_min} min` : "on time"}</b><small class="muted">${s.delay_reason ? `${h(s.delay_reason)} · source: ${h(s.reason_source)}` : "Reason not reported"}</small></div>
        <div class="it"><span>Affected passengers</span><b>${s.affected_passengers} <small class="muted">of ${s.connecting_passengers}</small></b><small class="muted">${s.unresolved} unresolved</small></div>
        <div class="it"><span>Affected connections</span><b>${s.affected_connections}</b><small class="muted">onward flights</small></div>
        <div class="it"><span>Disruption</span><b><span class="px-st ${h(s.status)}">${{ active: "● active", recovering: "◐ recovering", resolved: "✔ resolved" }[s.status]}</span></b></div>
        <div class="it" style="flex:1;min-width:240px" title="${h(dq.source)} · computed ${h(dq.computed_at)} · ${h(dq.clock)}"><span>Data</span><b><span class="px-lbl sim">SIMULATED</span></b>
          <small class="muted">as of ${h(String(dq.as_of || "").slice(11, 16) || dq.as_of || "unknown")}${dq.age_min != null ? ` · ${dq.age_min} min old` : ""}</small>
          ${dq.stale ? `<small class="px-warn">⚠ No data update for ${dq.age_min} min while the disruption is active: verify before acting</small>` : ""}
          ${dq.conflicts.length ? `<small class="px-warn">⚠ ${dq.conflicts.length} data conflict(s): ${dq.conflicts.slice(0, 2).map(h).join("; ")}${dq.conflicts.length > 2 ? "…" : ""}</small>` : ""}
</div>
      </section>
      <div class="card" style="margin-top:0">
        <div class="row between"><h3 style="margin:0">Passenger connection risk <span class="small muted">· recommends only</span></h3>
          <span class="px-view" role="group" aria-label="View">${[["table", "Table"], ["final_destination", "Group by final destination"], ["connecting_flight", "Group by connecting flight"]]
            .map(([k, l]) => `<button data-view="${k}" class="${P.view === k ? "on" : ""}" aria-pressed="${P.view === k}">${l}</button>`).join(" ")}</span></div>
        <div class="px-bar">
          ${opt("priority", "Priority", ["Critical", "High", "Medium", "Low", "Unknown"].map((x) => [x, x]))}
          ${opt("status", "Status", ["Confirmed missed", "Likely missed", "At risk", "Safe connection", "Rebooked", "Unknown"].map((x) => [x, x]))}
          ${opt("flight", "Connecting flight", flights.map((x) => [x, x]))}${opt("dest", "Final destination", dests.map((x) => [x, x]))}
          ${opt("assist", "Special assistance", [["yes", "Recorded"], ["no", "None recorded"]])}
          ${opt("dep", "Departs within", [["30", "30 min"], ["60", "60 min"], ["120", "2 h"], ["gone", "Already departed"]])}
          <label class="small"><input type="checkbox" data-showlow="1" ${P.showLow ? "checked" : ""}> Show low-priority (safe) passengers (${low})</label>
        </div>
        <div class="px-bar"><b>${P.sel.size} selected</b>
          <label class="small muted">Your name <input id="px-me" size="12" maxlength="40" value="${h(me)}" placeholder="required"></label>
          <button data-send="1" ${P.sel.size ? "" : "disabled"}>Send ${P.sel.size || ""} to Rebooking Recommendations</button>
          ${P.sel.size ? '<button class="ghost" data-clear="1">Clear selection</button>' : ""}
          <span class="small muted">Nothing is booked here: it goes to the <a href="#/tab/log">approval queue</a>.</span></div>
        <div id="px-handoff"></div>
        <div id="px-main">${P.view === "table" ? table() : groups(P.view)}</div>
        <details class="how" style="margin-top:14px"><summary>How the labels are set</summary><div>Deterministic, no statistical model.<br><b>Likely missed</b> = available connection &lt; ${MCT} (${d.rules.MCT_MIN} min);
          <b>At risk</b> = less than ${d.rules.VULNERABLE_SLACK_MIN} min spare beyond ${MCT}. <b>Critical</b> = likely missed and (protected-priority passenger, no alternative seats, or a hold
          that could still save it must be decided within ${d.rules.DECISION_WINDOW_MIN} min). <b>High</b> = likely missed with alternatives, or at risk with a protected-priority passenger.
          <b>Medium</b> = at risk. <b>Low</b> = feasible or rebooked. ${MCT} source: ${h(d.mct_source)} A passenger shows <b>Rebooked</b> only when the audit log holds a human-approved booking.</div></details>
      </div>`;
    tick();
  }

  const depBucket = (p) => P.f.dep === "gone" ? p.departs_in_min !== null && p.departs_in_min <= 0 : p.departs_in_min !== null && p.departs_in_min > 0 && p.departs_in_min <= +P.f.dep;
  function visible() {
    const f = P.f;
    const rows = P.data.passengers.filter((p) => (P.showLow || p.priority !== "Low" || f.priority === "Low" || f.status === "Safe connection" || f.status === "Rebooked")
      && (!f.priority || p.priority === f.priority) && (!f.status || p.status === f.status) && (!f.flight || p.connecting_flight === f.flight)
      && (!f.dest || p.final_destination === f.dest) && (!f.assist || p.assistance === (f.assist === "yes")) && (!f.dep || depBucket(p)));
    const k = P.sort.key, dir = P.sort.dir;
    const val = (p) => k === "priority" ? PRANK[p.priority] * 10000 + (p.departs_in_min ?? 9999) : p[k] ?? "";
    return rows.sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * dir);
  }
  function table() {
    const rows = visible();
    if (!rows.length) return `<p class="muted">No passengers match these filters.${!P.showLow ? " Low-priority (safe) passengers are hidden." : ""}</p>`;
    const th = (k, l, title) => `<th data-sort="${k}" title="${title}" aria-sort="${P.sort.key === k ? (P.sort.dir > 0 ? "ascending" : "descending") : "none"}">${l}${P.sort.key === k ? (P.sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`;
    const all = rows.every((r) => P.sel.has(r.ref));
    return `<div style="overflow-x:auto"><table class="px-tbl"><thead><tr><th><input type="checkbox" data-all="1" ${all ? "checked" : ""} aria-label="Select all shown"></th>
      ${th("priority", "Priority", "Intervention priority")}${th("ref", "Pax ref", "Passenger reference")}${th("final_destination", "Final dest.", "Final destination")}
      ${th("connecting_flight", "Conn. flight", "Connecting flight")}${th("departs_in_min", "Departs", "Connecting flight departure (estimated)")}<th class="px-hide">${MCT}</th>
      ${th("available_min", "Available", "Available connection time (predicted)")}<th title="Connection risk">Risk</th><th class="px-hide" title="Passenger priority / service category">Category</th>
      <th title="Special assistance">Assistance</th>${th("status", "Status", "Current status")}<th></th></tr></thead><tbody>${rows.map(row).join("")}</tbody></table></div>`;
  }
  function row(p) {
    const open = P.open === p.ref, rv = P.revealed[p.ref];
    const short = p.available_min !== null && p.available_min < p.mct_min;
    return `<tr class="px-row ${open ? "open" : ""}" data-row="${h(p.ref)}" aria-expanded="${open}">
      <td><input type="checkbox" data-sel="${h(p.ref)}" ${P.sel.has(p.ref) ? "checked" : ""} aria-label="Select ${h(p.ref)}"></td>
      <td>${prio(p.priority)}</td>
      <td><span class="px-mono">${h(p.ref)}</span><div class="small muted">${rv ? h(rv.name) : h(p.name_masked)}</div></td>
      <td>${h(p.final_destination)}</td><td class="px-mono">${h(p.connecting_flight)}</td>
      <td>${p.departure_est ? `<span class="px-mono">${h(p.departure_est)}</span>${p.departure_sched !== p.departure_est ? ` <small class="muted">(sched ${h(p.departure_sched)})</small>` : ""}
        <div><span class="px-cd" data-iso="${h(p.departure_iso)}"></span></div>` : '<span class="muted">unknown</span>'}</td>
      <td class="px-hide">${p.mct_min} min</td>
      <td class="${short ? "bad" : ""}">${p.available_min === null ? '<span class="muted">unknown</span>' : `${p.available_min} min`} <span class="px-lbl">PRED.</span></td>
      <td>${p.risk ? `<span class="px-s ${SCLS[p.status] || ""}">${h(p.risk.category)}</span>` : '<span class="muted small">insufficient data</span>'}</td>
      <td class="px-hide small">${h(p.priority_category)}</td>
      <td class="small">${p.assistance ? (rv ? h(rv.assistance) : "Yes · <span class=\"muted\">restricted</span>") : '<span class="muted">None recorded</span>'}</td>
      <td class="wrap"><span class="px-s ${SCLS[p.status] || ""}">${h(p.status)}</span>${p.pending ? `<div class="small" style="color:var(--alert)">⏳ recommendation #${p.pending.change_id} awaiting approval</div>` : ""}</td>
      <td aria-hidden="true">${open ? "▾" : "▸"}</td></tr>${open ? detail(p) : ""}`;
  }
  function detail(p) {
    const rv = P.revealed[p.ref];
    const alts = p.alternatives || [];
    return `<tr class="px-detail"><td colspan="13"><div class="px-dgrid px-sticky">
      <div><h4>Why ${h(p.priority)}: ${h(p.priority_reason || "")}</h4><ul>${p.factors.map((x) => `<li>${h(x)}</li>`).join("")}</ul>
        ${p.missing.length ? `<h4 style="margin-top:12px">Missing or conflicting data</h4><ul>${p.missing.map((x) => `<li class="px-warn">${h(x)}</li>`).join("")}</ul>` : ""}
        <p class="small muted">Risk basis: ${h(p.risk?.basis || "insufficient data")}</p></div>
      <div><h4>Itinerary</h4><table><tbody>${(p.itinerary || []).map((s) => `<tr><td class="px-mono">${h(s.flight)}</td><td>${h(s.route)}</td>
          <td class="small">sched ${h(s.sched)} · est ${h(s.est)}</td><td class="small muted">${h(s.kind)}</td></tr>`).join("")}</tbody></table>
        <p class="small">Bags: ${p.bags ? `${p.bags} checked, currently on ${h(p.bag_location || "unknown")}` : "none checked"}</p>
        <h4 style="margin-top:12px">Available options</h4>
        ${["Safe connection", "Rebooked"].includes(p.status) ? '<p class="small muted">No intervention needed.</p>' : `
        ${alts.length ? alts.map((a, i) => `<label class="px-opt"><input type="radio" name="px-alt-${h(p.ref)}" data-alt="${h(p.ref)}" value="${h(a.flight)}"
            ${(P.override[p.ref] || alts.find((x) => x.bags_make_it || !p.bags)?.flight || alts[0].flight) === a.flight ? "checked" : ""}>
            <span class="px-mono">${h(a.flight)}</span> departs ${h(a.departs)} · ${a.connection_min} min connection · ${a.free_seats} free seats (shared) ·
            ${a.bags_make_it ? "bags make it" : '<span class="px-warn">bags may not make it</span>'}</label>`).join("")
          : '<p class="px-warn small">No alternative seats in the schedule: needs manual handling (hotel / other carrier).</p>'}
        ${p.hold_option ? `<p class="small">Network option: hold ${h(p.hold_option.flight)} by ${p.hold_option.minutes} min. That is a network schedule change for the controller (Scenario Lab / approval queue), not this tab.</p>` : ""}
        ${p.pending ? `<p class="small" style="color:var(--alert)">⏳ Already in the approval queue (#${p.pending.change_id}).</p>` : alts.length ? `<button class="ghost" data-sendone="${h(p.ref)}">Send ${h(p.ref)} with the selected option</button>` : ""}`}
        <h4 style="margin-top:12px">Personal data</h4>${rv ? `<p class="small">${h(rv.name)} · ${h(rv.assistance)} <button class="ghost small" data-hide="${h(p.ref)}">Mask again</button></p>`
          : `<p class="small muted">Masked by default. <button class="ghost small" data-reveal="${h(p.ref)}">Reveal (controller PIN, audited)</button></p>`}
      </div></div></td></tr>`;
  }
  function groups(key) {
    const gs = P.data.groups[key];
    if (!gs.length) return '<p class="muted">No affected passengers to group.</p>';
    return `<p class="small muted">Select a group to send it as one batch.</p><div class="px-groups">${gs.map((g) => {
      const refs = P.data.passengers.filter((p) => p[key] === g.key && p.status !== "Safe connection").map((p) => p.ref);
      return `<div class="px-group"><h4><span class="px-mono">${h(g.key)}</span><small class="muted">${g.passengers} pax</small></h4>
        <div class="row">${Object.entries(g.by_priority).sort((a, b) => PRANK[a[0]] - PRANK[b[0]]).map(([k, n]) => `${prio(k)} <small>${n}</small>`).join(" ")}</div>
        <div class="small">${g.assistance} with special assistance · alternatives: ${g.alternatives.map(h).join(", ") || '<span class="px-warn">none</span>'}</div>
        <div class="row"><button class="ghost small" data-gsel="${h(refs.join(","))}">Select group</button>
          <button class="ghost small" data-gopen="${h(key)}|${h(g.key)}">Show in table</button></div></div>`;
    }).join("")}</div>`;
  }

  function tick() {
    if (!P.root?.isConnected) { clearInterval(P.timer); return; }
    document.querySelectorAll(".px-cd[data-iso]").forEach((el) => {
      const s = Math.round((new Date(el.dataset.iso) - Date.now()) / 1000);
      el.classList.toggle("soon", s > 0 && s <= 1800);
      el.textContent = s <= 0 ? "departed" : s <= 5400 ? `in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `in ${Math.round(s / 60)} min`;
    });
  }

  // ---------------- events ----------------
  async function send(refs) {
    if (!me) { toast("Enter your name before sending a recommendation", "warn"); return q("#px-me")?.focus(); }
    const r = await api("/api/pax/recommend", { flight_id: S.flight, pax_ids: refs, by: me, overrides: P.override });
    const box = r.staged.map((s) => s.status === "PENDING"
      ? `<div>⏳ Recommendation <b>#${s.id}</b> for ${s.passengers.length} passenger(s) is <b>awaiting controller approval</b> in the <a href="#/tab/log">approval queue</a>.</div>`
      : `<div class="bad">⛔ Recommendation #${s.id} for ${s.passengers.length} passenger(s) refused by the guards: ${h(s.reason)}</div>`).join("") +
      (r.skipped.length ? `<div class="small muted">Not sent: ${r.skipped.map((x) => `${h(x.ref)} (${h(x.reason)})`).join("; ")}</div>` : "");
    P.sel.clear();
    await load();
    q("#px-handoff").innerHTML = `<div class="px-handoff" role="status">${box || "Nothing to send."}<div class="small muted">Nothing was booked. A controller must approve or reject each recommendation (PIN).</div></div>`;
  }
  async function onClick(e) {
    const t = e.target;
    const d = t.closest("[data-retry],[data-view],[data-sort],[data-send],[data-clear],[data-sendone],[data-reveal],[data-hide],[data-gsel],[data-gopen]")?.dataset;
    if (d) {
      if (d.retry) return load();
      if (d.view) { P.view = d.view; return render(); }
      if (d.sort) { P.sort = { key: d.sort, dir: P.sort.key === d.sort ? -P.sort.dir : 1 }; return render(); }
      if (d.clear) { P.sel.clear(); return render(); }
      if (d.send) return send([...P.sel]).catch((err) => toast(err.message, "bad"));
      if (d.sendone) return send([d.sendone]).catch((err) => toast(err.message, "bad"));
      if (d.hide) { delete P.revealed[d.hide]; return render(); }
      if (d.reveal) return askAuth(`Reveal personal data for ${h(d.reveal)}<div class="small muted" style="margin-top:6px">Restricted to Operations Controllers. The access is written to the audit log.</div>`,
        async (a) => { P.revealed[d.reveal] = await api("/api/pax/reveal", { pax_id: d.reveal, ...a }); render(); });
      if (d.gsel) { d.gsel.split(",").filter(Boolean).forEach((x) => P.sel.add(x)); return render(); }
      if (d.gopen) { const [k, v] = d.gopen.split("|"); P.view = "table"; P.f = { ...P.f, [k === "final_destination" ? "dest" : "flight"]: v }; return render(); }
    }
    if (t.matches("input, button, a, label, select") || t.closest("label, a")) return;
    const r = t.closest("tr[data-row]");
    if (r) { P.open = P.open === r.dataset.row ? null : r.dataset.row; render(); }
  }
  function onChange(e) {
    const t = e.target;
    if (t.id === "px-me") { me = t.value.trim(); try { localStorage.setItem("aeroturn.me", me); } catch { /* ignore */ } return; }
    if (t.dataset.f !== undefined) { P.f[t.dataset.f] = t.value; return render(); }
    if (t.dataset.showlow) { P.showLow = t.checked; return render(); }
    if (t.dataset.sel) { t.checked ? P.sel.add(t.dataset.sel) : P.sel.delete(t.dataset.sel); return render(); }
    if (t.dataset.all) { visible().forEach((r) => (t.checked ? P.sel.add(r.ref) : P.sel.delete(r.ref))); return render(); }
    if (t.dataset.alt) { P.override[t.dataset.alt] = t.value; }
  }
  return { mount };
})();
