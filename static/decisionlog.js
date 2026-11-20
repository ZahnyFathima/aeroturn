// Decision log: calls column + key details, transcript drawer, one tab per decision, per-decision report, controller approval queue.
// Loaded before app.js; uses app.js helpers (h, api, toast, actionsText, askAuth, resultToast, refreshBell, notifList, statusTag, S) at call time.
// Rules: every transcript/LLM string is escaped with h(); statuses, guard codes, fixes and approval flags come from the server's code;
// nothing on this page executes an action: approval happens only in the controller approval queue (PIN).
const DecisionLog = (() => {
  const q = (s, el = document) => el.querySelector(s);
  const X = { root: null, selCall: null, selDec: null, f: { status: "", call: "", type: "" }, calls: [], decs: [], extracting: new Set(),
    play: null, rec: null, drawer: null, newCall: null };
  let me = "";
  try { me = localStorage.getItem("aeroturn.me") || ""; } catch { /* private mode */ }
  const ICON = { BLOCKED: "⛔", REJECTED: "✕", ROLLED_BACK: "↺", AWAITING_APPROVAL: "⏳", READY: "➜", GUARD_CHECKED: "🛡", EXECUTED: "✔", RECORDED: "•", PROPOSED: "○" };
  const st = (code, label) => `<span class="dl-st ${h(code)}">${ICON[code] || ""} ${h(label || code)}</span>`;
  const ts = (call, line, at) => `<button class="dl-ts" data-tx="${call}" data-line="${line}" title="Open the transcript at this line">${h(String(at || "").slice(0, 5))}</button>`;
  const ai = (it) => (it.status === "UNCONFIRMED" ? '<span class="dl-ai" title="Extracted by AI, not yet confirmed by a human">AI</span>' : "");
  const initials = (n) => h(String(n).split(/\s+/).filter((w) => /[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0]).join("").toUpperCase());
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const SV = '<span class="dl-sv" title="Computed by deterministic code">SYSTEM-VERIFIED</span>';
  const AS = '<span class="dl-as" title="Written by the LLM">AI-SUGGESTED</span>';
  async function busy(btn, fn) {
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="x-spin"></span> ${old}`;
    try { return await fn(); } catch (e) { toast(e.message, "bad"); } finally { btn.disabled = false; btn.innerHTML = old; }
  }

  // ---------------- shell ----------------
  async function mount(el) {
    X.root = el;
    el.innerHTML = `<div id="dl-alert"></div>
      <div class="dl-top">
        <div><div class="dl-h"><h3>Calls</h3><span class="small muted" id="dl-ccount"></span></div>
          <div class="dl-ctrl"><button class="dl-btn solid" id="dl-live" hidden></button><button class="dl-btn" id="dl-new">+ New call</button>
            <label class="small muted">Your name <input id="dl-me" size="11" maxlength="40" value="${h(me)}" placeholder="to confirm"></label></div>
          <div id="dl-newbox" class="card" style="margin:0 0 12px;padding:14px" hidden></div>
          <div class="dl-calls" id="dl-calls" aria-live="polite"><div class="dl-skel"></div><div class="dl-skel"></div></div></div>
        <div class="card" style="margin:0" id="dl-kd"><div class="dl-skel"></div><div class="dl-skel"></div></div>
      </div>
      <div class="card"><div class="dl-h"><h3>Decision log</h3><span class="small muted">Proposal → guard check → human confirm → controller approval</span></div>
        <div class="dl-filters" id="dl-filters"></div><div class="dl-log" id="dl-log"><div class="dl-skel"></div></div></div>
      <div class="card" id="dl-queue"></div>
      <div class="card"><details><summary><b>System decision history & notifications</b> <span class="small muted">· every change, from any screen</span></summary><div id="dl-hist"></div></details></div>`;
    q("#dl-me").onchange = (e) => { me = e.target.value.trim(); try { localStorage.setItem("aeroturn.me", me); } catch { /* ignore */ } };
    q("#dl-live").onclick = playLive;
    q("#dl-new").onclick = toggleNew;
    el.onclick = onClick;
    el.onchange = onChange;
    el.onkeydown = onKey;
    await load();
  }

  async function load() {
    if (!X.root?.isConnected) return;
    let calls, decs, log, ledger;
    try {
      [calls, decs, log, ledger] = await Promise.all([api("/api/calls"), api("/api/ledger/decisions"), api("/api/decisions"), api(`/api/ledger/${S.flight}`)]);
    } catch (e) { q("#dl-calls").innerHTML = `<p class="bad">Could not load calls: ${h(e.message)}</p>`; return; }
    X.calls = calls.filter((c) => c.flight_id === S.flight);
    X.decs = decs.filter((d) => d.flight_id === S.flight);
    X.demo = ledger.demo_calls.filter((d) => !X.calls.some((c) => c.demo_id === d.id));
    if (!X.selCall || !X.calls.some((c) => c.id === X.selCall)) X.selCall = (X.calls.find((c) => c.status === "LIVE") || X.calls[0])?.id ?? null;
    renderCalls(); renderKeyDetails(); renderFilters(); renderLog(); renderQueue(log); renderAlert();
    autoExtract();
  }

  // ---------------- calls column ----------------
  function renderCalls() {
    const live = q("#dl-live");
    live.hidden = !X.demo.length || !!X.play;
    if (X.demo.length) live.textContent = `▶ Start live call: ${X.demo[0].title}`;
    q("#dl-ccount").textContent = `${X.calls.length} calls · ${X.decs.length} decisions`;
    q("#dl-calls").innerHTML = X.calls.map((c) => {
      const isLive = c.status === "LIVE", n = c.counts;
      return `<div class="dl-call ${isLive ? "live" : ""} ${c.id === X.selCall ? "sel" : ""}" data-call="${c.id}" tabindex="0" role="button" aria-pressed="${c.id === X.selCall}"
        aria-label="Call ${h(c.label)}: ${h(c.title)}">
        <div class="t"><span>CALL ${h(c.label)}</span>${isLive ? `<span class="dl-live">LIVE · ${c.duration_min} min</span>` : `<span>Ended · ${c.duration_min} min</span>`}</div>
        <h4>${h(c.title)}</h4>
        <div class="dl-chips">${c.participants.map((p) => `<span class="dl-chip" title="${h(p.name)}">${h(p.role || p.name)}</span>`).join("")}</div>
        <div class="dl-meta"><span>${h((c.started || "").slice(0, 5))} → ${isLive ? "now" : h((c.ended || "").slice(0, 5))}</span>
          ${X.extracting.has(c.id) ? '<span><span class="x-spin"></span> AI extracting…</span>' : !c.extracted ? '<span>not analysed yet</span>' :
            `<span>${n.issues} issue${n.issues === 1 ? "" : "s"}</span><span>${n.decisions} decisions</span><span>${n.actions} actions</span>${n.blocked ? `<span class="bad">${n.blocked} blocked</span>` : ""}`}</div>
        <button class="dl-btn" data-tx="${c.id}">${isLive ? "〜 View live transcript" : "View transcript"}</button></div>`;
    }).join("") || '<p class="dl-empty">No calls for this flight yet. Start the live call or add your own.</p>';
  }

  function renderKeyDetails() {
    const c = X.calls.find((x) => x.id === X.selCall), el = q("#dl-kd");
    if (!c) { el.innerHTML = '<p class="dl-empty">Select a call to see its key details.</p>'; return; }
    if (!c.extracted) {
      el.innerHTML = `<div class="dl-cap">${h(c.label)} · KEY DETAILS</div><h3 style="margin:6px 0 12px">${h(c.title)}</h3>
        ${X.extracting.has(c.id) ? '<div class="dl-skel"></div><div class="dl-skel"></div><p class="small muted"><span class="x-spin"></span> The AI is reading the transcript…</p>'
          : `<p class="dl-empty">${c.lines ? "Not analysed yet." : "No transcript yet."}</p>${c.lines ? `<button class="dl-btn" data-extract="${c.id}">Analyse call (AI)</button>` : ""}`}`;
      return;
    }
    const people = c.participants.map((p) => `<li><span class="dl-av" aria-hidden="true">${initials(p.name)}</span><span class="grow">${h(p.name)}<div class="small muted">${h(p.role || "")}${p.left ? ` · left ${h(p.left.slice(0, 5))}` : ""}</div></span>
      ${ts(c.id, p.joined_line, p.joined)}</li>`).join("");
    const issues = c.issues.map((i) => `<li>${ts(c.id, i.line_n, i.at)}<span class="grow">${h(i.summary)}</span></li>`).join("");
    const facts = c.facts.map((f) => `<li class="dl-fact"><span class="muted">${h(f.label)}</span><b>${h(f.value)}</b>${ts(c.id, f.line_n, f.at)}</li>`).join("");
    const acts = c.actions.map((a) => `<li><input type="checkbox" data-done="${a.id}" ${a.done ? "checked" : ""} aria-label="Mark done: ${h(a.summary)}">
      <span class="grow" ${a.done ? 'style="text-decoration:line-through;opacity:.7"' : ""}>${h(a.summary)}<div class="small muted">${h(a.owner || "owner not stated")}${a.due ? ` · due ${h(a.due)}` : ""}${a.done_by ? ` · done by ${h(a.done_by)}` : ""}</div></span>
      ${ts(c.id, a.line_n, a.at)}</li>`).join("");
    const decs = c.decisions.map((d) => `<button class="dl-decchip" data-dec="${d.id}"><b class="${d.status_code === "BLOCKED" ? "bad" : ""}">${h(d.label_id)}</b>${h(d.summary)} ${st(d.status_code, d.status_label)}</button>`).join("");
    el.innerHTML = `<div class="dl-h"><div><div class="dl-cap">${h(c.label)} · KEY DETAILS</div><h3 style="margin:6px 0 0">${h(c.title)}</h3></div>
        <span class="small muted" style="text-align:right"><span class="dl-ai">AI</span> = not yet confirmed</span></div>
      <div class="dl-kd-grid">
        <section class="dl-sec" aria-label="Issues raised"><div class="dl-cap">Issues raised</div><ul>${issues || '<li class="dl-empty">None extracted</li>'}</ul></section>
        <section class="dl-sec" aria-label="Who was on the call"><div class="dl-cap">Who was on the call</div><ul>${people}</ul></section>
        <section class="dl-sec" aria-label="Facts and constraints"><div class="dl-cap">Facts and constraints</div><ul>${facts || '<li class="dl-empty">None stated</li>'}</ul></section>
        <section class="dl-sec" aria-label="Action items"><div class="dl-cap">Action items</div><ul>${acts || '<li class="dl-empty">None</li>'}</ul></section>
        <section class="dl-sec full" aria-label="Decisions from this call"><div class="dl-cap">Decisions from this call → decision log</div>
          <div class="dl-decchips" style="margin-top:10px">${decs || '<span class="dl-empty">No decisions</span>'}</div></section>
      </div><div class="dl-actions"><button class="dl-btn" data-extract="${c.id}">↻ Re-run AI extraction</button></div>`;
  }

  async function autoExtract() { // pre-loaded calls are only transcripts: the LLM analyses them on first view
    for (const c of X.calls.filter((x) => x.status === "ENDED" && !x.extracted && x.lines && !X.extracting.has(x.id))) await extract(c.id, true);
  }
  async function extract(cid, quiet) {
    if (X.extracting.has(cid)) return;
    X.extracting.add(cid); renderCalls(); renderKeyDetails();
    try {
      const r = await api("/api/ledger/extract", { call_id: cid });
      if (!quiet || r.alerts.length) toast(r.alerts.length ? `⛔ ${r.alerts.length} illegal proposal(s) on this call blocked by the guards` : `${r.items} items extracted`, r.alerts.length ? "bad" : "ok");
    } catch (e) { toast(e.message, "bad"); } finally { X.extracting.delete(cid); await load(); }
  }

  function renderAlert() {
    const live = new Set(X.calls.filter((c) => c.status === "LIVE").map((c) => c.id));
    const b = X.decs.filter((d) => live.has(d.call_id) && d.status_code === "BLOCKED");
    q("#dl-alert").innerHTML = b.length ? `<div class="dl-alert" role="alert"><b class="bad">⛔ Live guard alert</b> ${b.map((d) =>
      `<div class="small"><b>${h(d.made_by)}</b> proposed “${h(d.summary)}” · <span class="dl-code">${h(d.guard?.code || "")}</span> ${h(d.guard?.detail || "")}
       <button class="dl-btn" style="padding:2px 10px" data-dec="${d.id}">Open ${h(d.label_id)}</button></div>`).join("")}</div>` : "";
  }

  // ---------------- decision log tabs ----------------
  const TYPES = ["crew", "maintenance", "passengers", "bags", "gate", "network", "other"];
  const STATUSES = [["BLOCKED", "Blocked"], ["AWAITING_APPROVAL", "Awaiting approval"], ["GUARD_CHECKED", "Guard-checked"], ["READY", "Ready to send"],
    ["EXECUTED", "Executed"], ["RECORDED", "Recorded"], ["REJECTED", "Rejected"], ["ROLLED_BACK", "Rolled back"]];
  function renderFilters() {
    const sel = (k, label, opts) => `<label class="small muted">${label} <select data-filter="${k}"><option value="">All</option>${opts.map(([v, l]) =>
      `<option value="${h(v)}" ${X.f[k] === String(v) ? "selected" : ""}>${h(l)}</option>`).join("")}</select></label>`;
    q("#dl-filters").innerHTML = sel("status", "Status", STATUSES) + sel("call", "Call", X.calls.map((c) => [c.id, `${c.label} ${c.title}`])) +
      sel("type", "Issue type", TYPES.map((t) => [t, t[0].toUpperCase() + t.slice(1)]));
  }
  const shown = () => X.decs.filter((d) => (!X.f.status || d.status_code === X.f.status) && (!X.f.call || String(d.call_id) === X.f.call) &&
    (!X.f.type || d.category === X.f.type));
  function renderLog() {
    const list = shown(), el = q("#dl-log");
    if (!X.decs.length) { el.innerHTML = '<p class="dl-empty">No decisions yet. They appear here as soon as the AI has analysed a call.</p>'; return; }
    if (!list.length) { el.innerHTML = '<p class="dl-empty">No decisions match these filters.</p>'; return; }
    if (!list.some((d) => d.id === X.selDec)) X.selDec = (list.find((d) => d.status_code === "BLOCKED") || list[0]).id;
    el.innerHTML = `<div class="dl-tabs" role="tablist" aria-orientation="vertical" aria-label="Decisions">${list.map((d) => `<button class="dl-tab" role="tab" id="dl-tab-${d.id}"
        aria-selected="${d.id === X.selDec}" aria-controls="dl-panel" tabindex="${d.id === X.selDec ? 0 : -1}" data-dec="${d.id}">
        <div class="r1"><span class="dl-code">${h(d.label_id)}</span>${st(d.status_code, d.status_label)}<span>${h((d.at || "").slice(0, 5))}</span></div>
        <div class="r2">${h(d.summary)}</div></button>`).join("")}</div>
      <div class="dl-panel" role="tabpanel" id="dl-panel" aria-labelledby="dl-tab-${X.selDec}" tabindex="0"><div class="dl-skel"></div><div class="dl-skel"></div></div>`;
    if (matchMedia("(max-width:900px)").matches) q(`#dl-tab-${X.selDec}`).after(q("#dl-panel")); // accordion on narrow screens
    renderDetail(X.selDec);
  }

  async function renderDetail(did) {
    let d;
    try { d = await api(`/api/ledger/decisions/${did}`); } catch (e) { if (q("#dl-panel")) q("#dl-panel").innerHTML = `<p class="bad">${h(e.message)}</p>`; return; }
    const el = q("#dl-panel");
    if (!el || X.selDec !== did) return;
    const pipe = d.pipeline.map((p) => `<li class="${p.state}">${{ done: "✔", current: "⏳", failed: "✕", todo: "○" }[p.state]} ${h(p.step)}</li>`).join("");
    const fix = d.fix ? `<div style="margin-top:10px"><b>Smallest legal fix</b> ${SV}${d.fix.steps.map((s, k) => `<div class="small" style="margin:6px 0">${k + 1}. <span class="dl-code">${h(s.for)}</span> → ${h(s.fix)}</div>`).join("")}
      <div class="small">Resulting actions: ${actionsText(d.fix.actions)}</div>
      <div class="small ${d.fix.ok ? "ok" : "bad"}">${d.fix.ok ? "✔ passes all guards in a rolled-back dry run" : `✕ no automatic legal fix: ${h(d.fix.remaining?.code || "")} ${h(d.fix.remaining?.detail || "")}`}</div></div>` : "";
    el.innerHTML = `<div class="dl-h"><span class="dl-code">${h(d.label_id)} · ${h(d.call_label)}</span>${st(d.status_code, d.status_label)}</div>
      <h4>${h(d.summary)}</h4>
      <ol class="dl-pipe" aria-label="Status pipeline">${pipe}</ol>
      ${d.status_code === "BLOCKED" ? `<div class="dl-block" role="status"><b class="bad">⛔ BLOCKED by code</b> <span class="dl-code">${h(d.guard?.code)}</span>: ${h(d.guard?.detail)}
        <div class="small muted" title="${h(d.guard?.message)}">${h(shortIds(d.guard?.message))}</div>${fix}</div>` : ""}
      <dl class="dl-fields">
        <dt>Decision</dt><dd>${h(d.summary)} ${ai(d)}</dd>
        <dt>Decided by</dt><dd><b>${h(d.made_by)}</b>${d.role ? `, ${h(d.role)}` : ""}<div class="dl-chips">${d.others_on_call.map((o) => `<span class="dl-chip">${h(o.name)} · ${h(o.role || "")}</span>`).join("") || '<span class="small muted">nobody else on the call</span>'}</div></dd>
        <dt>When · source</dt><dd>${ts(d.call_id, d.line_n, d.at)} <button class="dl-btn" style="padding:2px 10px" data-call="${d.call_id}">${h(d.call_label)} ${h(d.call_title)}</button></dd>
        <dt>Issue addressed</dt><dd>${d.issue_addressed.map((x) => `<div style="margin-bottom:4px">${ts(x.call_id, x.line_n, x.at)} ${h(x.summary)}</div>`).join("") || '<span class="muted">No linked issue</span>'}</dd>
        <dt>Evidence</dt><dd><div class="dl-quote">“${h(d.quote)}”</div><button class="dl-btn" style="margin-top:6px;padding:3px 10px" data-tx="${d.call_id}" data-line="${d.line_n}">View in transcript</button></dd>
        <dt>System actions</dt><dd>${d.actions.length ? actionsText(d.actions) : '<span class="muted">none: recorded only</span>'} ${d.requires_approval ? '<span class="dl-flag">Requires controller approval</span>' : ""}</dd>
        <dt>AI extraction</dt><dd><span class="dl-bar"><i style="width:${Math.round(d.confidence * 100)}%"></i></span>${Math.round(d.confidence * 100)}% confidence
          ${d.flags.length ? [...new Set(d.flags)].map((f) => `<div class="warn small">⚑ ${h(shortIds(f))}</div>`).join("") : ""}
          <div class="small">${d.confirmed_by ? `${d.status === "REJECTED" ? "Rejected" : "Confirmed"} by <b>${h(d.confirmed_by)}</b>` : "Not yet confirmed by a human"}</div></dd>
      </dl>
      <div class="dl-actions">
        <button class="dl-btn solid" data-report="${d.id}">📄 View report</button>
        ${d.can_confirm ? `<button class="dl-btn" data-conf="${d.id}">✔ Confirm extraction</button><button class="dl-btn" data-rej="${d.id}">✕ Reject extraction</button>` : ""}
        ${d.can_send ? `<button class="dl-btn" data-send="${d.id}">➜ Send to controller approval</button>` : ""}
        ${d.status_code === "AWAITING_APPROVAL" ? `<button class="dl-btn" data-goqueue="1">⏳ In the controller approval queue (#${d.change_id})</button>` : ""}
      </div>
      <p class="small muted" style="margin-top:12px">Confirming never executes anything.</p>${how("Confirming checks the AI captured the call correctly. It never executes anything: legal decisions go to the controller approval queue below.")}`;
  }

  function renderQueue(log) {
    const pend = log.decisions.filter((c) => c.status === "PENDING" && c.flight_id === S.flight);
    q("#dl-queue").innerHTML = `<div class="dl-h"><h3>Controller approval queue</h3><span class="small muted">Name + PIN · applied atomically</span></div>
      ${pend.length ? `<table><thead><tr><th>#</th><th>Change</th><th>Actions</th><th>Proposed by</th><th></th></tr></thead><tbody>${pend.map((c) => `<tr><td>${c.id}</td><td>${h(c.title)}</td>
        <td class="small">${actionsText(JSON.parse(c.actions))}</td><td class="small">${h(c.proposed_by)}</td>
        <td><button data-appr="${c.id}" data-ok="1">Approve</button> <button class="ghost" data-appr="${c.id}">Reject</button></td></tr>`).join("")}</tbody></table>`
        : '<p class="dl-empty">Nothing waiting for approval.</p>'}`;
    q("#dl-hist").innerHTML = `<table><thead><tr><th>#</th><th>Time</th><th>Flight</th><th>Decision</th><th>Actions</th><th>Status</th><th>Proposed</th><th>Decided</th><th>Reason</th></tr></thead><tbody>${log.decisions.map((c) =>
      `<tr><td>${c.id}</td><td class="small">${h(c.ts)}</td><td>${h(c.flight_id)}</td><td>${h(c.title)}</td><td class="small">${actionsText(JSON.parse(c.actions))}</td><td>${statusTag(c.status)}</td>
      <td class="small">${h(c.proposed_by)}</td><td class="small">${h(c.decided_by)}</td><td class="small">${h(c.reason)}</td></tr>`).join("") ||
      '<tr><td colspan="9" class="muted">No decisions yet</td></tr>'}</tbody></table><h3 style="margin-top:18px">Notifications sent</h3>${notifList(log.notifications)}`;
  }

  // ---------------- events ----------------
  async function onClick(e) {
    const t = e.target.closest("[data-tx],[data-dec],[data-call],[data-conf],[data-rej],[data-send],[data-report],[data-appr],[data-extract],[data-goqueue]");
    if (!t || t.matches("input")) return;
    const d = t.dataset;
    if (d.tx) { e.stopPropagation(); return openTranscript(+d.tx, d.line ? +d.line : null, t); }
    if (d.dec) { X.selDec = +d.dec; X.f = { status: "", call: "", type: "" }; renderFilters(); renderLog(); return q("#dl-log").scrollIntoView({ behavior: "smooth", block: "start" }); }
    if (d.call) { X.selCall = +d.call; renderCalls(); renderKeyDetails(); return; }
    if (d.extract) return extract(+d.extract);
    if (d.goqueue) return q("#dl-queue").scrollIntoView({ behavior: "smooth" });
    if (d.report) return openReport(+d.report, false, t);
    if (d.conf || d.rej) {
      if (!me) { toast("Enter your name (Calls panel) to confirm an extraction", "warn"); return q("#dl-me").focus(); }
      return busy(t, async () => {
        const r = await api("/api/ledger/confirm", { item_id: +(d.conf || d.rej), by: me, confirm: !!d.conf });
        toast(r.staged ? (r.staged.status === "PENDING" ? `Sent to the controller approval queue (#${r.staged.id})` : `Confirmed, but not sent: blocked by guards (${r.staged.reason})`)
          : `Extraction ${r.status.toLowerCase()}`, r.staged?.status === "BLOCKED" ? "warn" : "ok");
        await load();
      });
    }
    if (d.send) return busy(t, async () => { const r = await api("/api/ledger/send", { item_id: +d.send });
      toast(r.status === "PENDING" ? `Sent to the controller approval queue (#${r.id})` : `Blocked by guards: ${r.reason}`, r.status === "PENDING" ? "ok" : "bad"); await load(); });
    if (d.appr) return askAuth(`${d.ok ? "Approve" : "Reject"} change #${h(d.appr)}`, async (a) => {
      resultToast(await api("/api/approve", { id: +d.appr, approve: !!d.ok, ...a })); refreshBell(); await load(); });
  }
  async function onChange(e) {
    const t = e.target;
    if (t.dataset.filter !== undefined) { X.f[t.dataset.filter] = t.value; return renderLog(); }
    if (t.dataset.done) {
      if (!me) { t.checked = !t.checked; toast("Enter your name (Calls panel) first", "warn"); return q("#dl-me").focus(); }
      try { await api("/api/ledger/action_done", { item_id: +t.dataset.done, done: t.checked, by: me }); await load(); } catch (err) { toast(err.message, "bad"); }
    }
  }
  function onKey(e) {
    const tab = e.target.closest?.(".dl-tab");
    if (tab && ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      const tabs = [...document.querySelectorAll(".dl-tab")], i = tabs.indexOf(tab);
      const n = tabs[e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowDown" ? 1 : -1) + tabs.length) % tabs.length];
      X.selDec = +n.dataset.dec; renderLog(); q(`#dl-tab-${X.selDec}`)?.focus();
    }
    const card = e.target.closest?.(".dl-call");
    if (card && e.target === card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); X.selCall = +card.dataset.call; renderCalls(); renderKeyDetails(); q(`[data-call="${X.selCall}"]`)?.focus(); }
  }

  // ---------------- transcript drawer ----------------
  function trap(box, opener, onClose) {
    const prev = opener || document.activeElement;
    const keys = (e) => {
      if (e.key === "Escape") { e.preventDefault(); return close(); }
      if (e.key !== "Tab") return;
      const f = [...box.querySelectorAll("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.offsetParent);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    function close() { document.removeEventListener("keydown", keys, true); onClose(); prev?.focus?.(); }
    document.addEventListener("keydown", keys, true);
    return close;
  }
  async function openTranscript(cid, line, opener) {
    closeDrawer();
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="dl-scrim" data-close="1"></div><aside class="dl-drawer" role="dialog" aria-modal="true" aria-labelledby="dl-dt">
      <header><div class="dl-h"><div><div class="dl-cap" id="dl-dsub"></div><h3 id="dl-dt" style="margin:4px 0 0">Transcript</h3></div><button class="dl-btn" data-close="1" aria-label="Close transcript">✕ Close</button></div>
        <div class="bar"><input type="search" id="dl-search" placeholder="Search the transcript…" aria-label="Search the transcript"><button class="dl-btn" id="dl-latest">↓ Jump to latest</button></div></header>
      <div class="dl-lines" id="dl-lines" aria-live="polite"><div class="dl-skel"></div><div class="dl-skel"></div></div></aside>`;
    document.body.appendChild(wrap);
    X.drawer = { wrap, cid, line, follow: !line, timer: null, close: null };
    X.drawer.close = trap(wrap, opener, () => { clearInterval(X.drawer?.timer); wrap.remove(); X.drawer = null; });
    wrap.onclick = (e) => { if (e.target.closest("[data-close]")) X.drawer.close(); };
    q("#dl-search", wrap).oninput = () => drawLines();
    q("#dl-latest", wrap).onclick = () => { X.drawer.follow = true; const L = q("#dl-lines", wrap); L.scrollTop = L.scrollHeight; };
    q("#dl-lines", wrap).onscroll = (e) => { const L = e.target; X.drawer.follow = L.scrollHeight - L.scrollTop - L.clientHeight < 40; };
    await fetchLines(true);
    q("#dl-search", wrap).focus();
    X.drawer.timer = setInterval(() => X.drawer?.data?.call.status === "LIVE" && fetchLines(false), 2000); // live: keep updating
  }
  function closeDrawer() { X.drawer?.close?.(); }
  async function fetchLines(first) {
    const dr = X.drawer; if (!dr) return;
    try { dr.data = await api(`/api/calls/${dr.cid}/transcript`); } catch (e) { q("#dl-lines", dr.wrap).innerHTML = `<p class="bad">${h(e.message)}</p>`; return; }
    q("#dl-dsub", dr.wrap).innerHTML = `${h(dr.data.call.label)} · ${dr.data.call.status === "LIVE" ? '<span class="dl-live">LIVE</span>' : "ENDED"}`;
    q("#dl-dt", dr.wrap).textContent = dr.data.call.title;
    drawLines();
    const L = q("#dl-lines", dr.wrap);
    if (first && dr.line) q(`#dl-l-${dr.line}`, dr.wrap)?.scrollIntoView({ block: "center" });
    else if (dr.follow) L.scrollTop = L.scrollHeight;
  }
  function drawLines() {
    const dr = X.drawer; if (!dr?.data) return;
    const term = q("#dl-search", dr.wrap).value.trim().toLowerCase();
    const mark = (s) => { const e = h(s); if (!term) return e; const t = h(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); return e.replace(new RegExp(t, "gi"), (m) => `<mark>${m}</mark>`); };
    const lines = dr.data.lines.filter((l) => !term || `${l.speaker} ${l.role || ""} ${l.text}`.toLowerCase().includes(term));
    q("#dl-lines", dr.wrap).innerHTML = lines.map((l) => `<div class="dl-line ${l.tags.length ? "tagged" : ""} ${l.n === dr.line ? "hl" : ""}" id="dl-l-${l.n}">
      <span class="ts">[${h(l.at)}]</span><b>${mark(l.speaker)}</b>${l.role ? ` <span class="muted">(${mark(l.role)})</span>` : ""}: ${mark(l.text)}
      ${l.tags.map((t) => `<span class="tg ${h(t.kind)}">${h(t.kind)}${t.kind === "DECISION" ? ` D-${String(t.id).padStart(2, "0")}` : ""}</span>`).join("")}</div>`).join("")
      || `<p class="dl-empty">${term ? "No lines match." : "No transcript lines yet."}</p>`;
  }

  // ---------------- per-decision report ----------------
  async function openReport(did, regenerate, opener) {
    document.querySelector(".dl-modal")?.remove();
    const m = document.createElement("div");
    m.className = "dl-modal";
    m.innerHTML = `<div class="dl-report" role="dialog" aria-modal="true" aria-labelledby="dl-rt"><div class="tools">
      <button class="dl-btn" id="dl-rregen">↻ Regenerate</button><button class="dl-btn" id="dl-rprint">🖨 Print / Save as PDF</button>
      <button class="dl-btn" id="dl-rcopy">⧉ Copy as text</button><button class="dl-btn" id="dl-rclose" aria-label="Close report">✕ Close</button></div>
      <div id="dl-rbody"><h2 id="dl-rt">Report</h2><p class="small muted"><span class="x-spin"></span> ${regenerate ? "Regenerating" : "Generating"} the report: code runs the compliance checks and impact, the AI writes the next steps…</p>
        <div class="dl-skel"></div><div class="dl-skel"></div><div class="dl-skel"></div></div></div>`;
    document.body.appendChild(m);
    const close = trap(m, opener, () => m.remove());
    q("#dl-rclose", m).onclick = close;
    m.onclick = (e) => { if (e.target === m) close(); };
    q("#dl-rregen", m).onclick = () => { close(); openReport(did, true, opener); };
    q("#dl-rprint", m).onclick = () => window.print();
    q("#dl-rclose", m).focus();
    let r;
    try { r = await api(`/api/ledger/decisions/${did}/report`, { regenerate }); } catch (e) { q("#dl-rbody", m).innerHTML = `<h2 id="dl-rt">Report</h2><p class="bad">${h(e.message)}</p>`; return; }
    const imp = r.impact, w = r.who;
    const val = (v) => (v === null || v === undefined ? "—" : h(v));
    q("#dl-rbody", m).innerHTML = `<div class="dl-cap">${h(r.label)} · DECISION REPORT · generated ${h(r.generated)}${r.cached ? " (cached)" : ""}</div>
      <h2 id="dl-rt">${h(r.decision.statement)}</h2>${st(r.decision.status_code, r.decision.status)}
      <h3>Problem ${AS}</h3><p>${h(r.problem) || '<span class="muted">—</span>'}</p>
      <h3>Decision <span class="dl-as" title="Wording extracted by the LLM from the transcript">${w.confirmed_by ? "AI-EXTRACTED · HUMAN-CONFIRMED" : "AI-EXTRACTED · NOT YET CONFIRMED"}</span></h3>
      <p>${h(r.decision.statement)}</p><div class="small">System actions (schema-checked by code): ${r.decision.actions.length ? actionsText(r.decision.actions) : "none"}</div>
      <h3>Who decided and when ${SV}</h3><p><b>${h(w.made_by)}</b>${w.role ? `, ${h(w.role)}` : ""} at ${h(w.at)} on ${h(w.call_label)} “${h(w.call)}”.
        ${w.others.length ? `Also on the call: ${w.others.map((o) => `${h(o.name)} (${h(o.role || "")})`).join(", ")}.` : ""}
        ${w.confirmed_by ? `Extraction confirmed by ${h(w.confirmed_by)}.` : "Extraction not yet confirmed by a human."} ${w.approved_by ? `Approved by ${h(w.approved_by)}.` : ""}</p>
      <h3>Evidence ${SV}</h3>${r.evidence.map((e) => `<div style="margin:8px 0"><div class="dl-quote">“${h(e.quote)}”</div><div class="small muted">${h(e.speaker)} · ${h(e.at)}
        <button class="dl-btn" style="padding:1px 8px" data-rtx="${e.call_id}" data-line="${e.line_n}">View in transcript</button></div></div>`).join("")}
      <h3>Compliance check results ${SV}</h3>${r.guard ? `<p class="bad"><span class="dl-code">${h(r.guard.code)}</span>: ${h(r.guard.detail)}</p>` : ""}
      <table><thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead><tbody>${r.compliance.map((c) => `<tr><td>${h(c.check)}</td>
        <td><b class="${{ PASS: "ok", NO: "ok", FAIL: "bad", "AT RISK": "warn", OPEN: "warn", YES: "warn" }[c.result] || ""}">${{ PASS: "✔ ", FAIL: "✕ ", "AT RISK": "⚠ ", OPEN: "⚠ " }[c.result] || ""}${h(c.result)}</b></td>
        <td class="small">${h(c.detail)}</td></tr>`).join("")}</tbody></table>
      ${r.fix ? `<p class="small"><b>Smallest legal fix:</b> ${r.fix.steps.map((s) => h(s.fix)).join(" → ")} ${r.fix.ok ? '<span class="ok">✔ passes all guards</span>' : ""}</p>` : ""}
      <h3>Impact ${SV}</h3><p class="small muted">Basis: ${h(imp.basis)}${imp.applied_in_dry_run ? " (rolled-back dry run)" : ""}</p>
      <table><tbody><tr><td>Passengers affected</td><td>${val(imp.passengers_affected)}</td><td>Vulnerable passengers affected</td><td>${val(imp.vulnerable_affected)}</td></tr>
        <tr><td>At risk now</td><td>${val(imp.passengers_at_risk_now)} (${val(imp.vulnerable_at_risk_now)} vulnerable)</td><td>Still stranded after</td><td>${val(imp.stranded_after)}</td></tr>
        <tr><td>Crew</td><td colspan="3">${h(imp.crew)}</td></tr><tr><td>Gate</td><td colspan="3">${h(imp.gate)}</td></tr>
        <tr><td>Downstream flights</td><td colspan="3">${imp.downstream_flights.map(h).join(", ") || "none"} · network delay ${val(imp.network_delay_min)} min</td></tr>
        <tr><td>Bags moving</td><td>${val(imp.bags_moving)}</td><td>Bags at risk</td><td>${val(imp.bags_at_risk)}</td></tr></tbody></table>
      <h3>Suggested next steps ${AS}</h3><table><thead><tr><th>P</th><th>Step</th><th>Owner</th><th>Deadline</th><th>Checks ${SV}</th></tr></thead><tbody>${r.next_steps.map((s) => `<tr>
        <td>P${s.priority}</td><td>${h(s.step)}${s.action ? `<div class="small muted">${actionsText([s.action])}</div>` : ""}</td><td>${h(s.owner)}</td><td>${h(s.deadline || "—")}</td>
        <td>${s.requires_controller_approval ? '<span class="dl-flag">Requires controller approval</span> ' : ""}${s.guard ? (s.guard === "PASS" ? '<span class="ok small">✔ guards pass</span>' : `<span class="bad small">✕ ${h(s.guard)}</span>`) : '<span class="small muted">no system action</span>'}</td></tr>`).join("") ||
        '<tr><td colspan="5" class="muted">No steps suggested</td></tr>'}</tbody></table>
      <h3>Open risks</h3>${r.system_risks.length ? `<div>${SV}<ul>${r.system_risks.map((x) => `<li>${h(x)}</li>`).join("")}</ul></div>` : ""}
      ${r.open_risks.length ? `<div>${AS}<ul>${r.open_risks.map((x) => `<li>${h(x)}</li>`).join("")}</ul></div>` : ""}
      <p class="small muted">Source: ${h(r.source)} for AI-suggested sections; every other section is computed by AeroTurn's deterministic code.</p>`;
    q("#dl-rbody", m).onclick = (e) => { const b = e.target.closest("[data-rtx]"); if (b) { close(); openTranscript(+b.dataset.rtx, +b.dataset.line); } };
    q("#dl-rcopy", m).onclick = async () => {
      try { await navigator.clipboard.writeText(q("#dl-rbody", m).innerText); toast("Report copied as text"); } catch { toast("Clipboard not available in this browser", "warn"); }
    };
  }

  // ---------------- live call + your own call ----------------
  async function playLive() {
    const demo = X.demo[0];
    if (!demo || X.play) return;
    const r = await api("/api/ledger/call/start", { flight_id: S.flight, title: demo.title, source: "live", demo_id: demo.id });
    X.play = { stop: false, cid: r.call_id }; X.selCall = r.call_id;
    await load();
    try {
      for (let k = 0; k < demo.lines.length && !X.play.stop; k++) {
        await api("/api/ledger/call/line", { call_id: r.call_id, ...demo.lines[k] });
        if ((k + 1) % 3 === 0 && k + 1 < demo.lines.length) await extract(r.call_id, true); else await load();
        await sleep(2400);
      }
      await api("/api/ledger/call/end", { call_id: r.call_id });
      await extract(r.call_id);
    } catch (e) { toast(e.message, "bad"); } finally { X.play = null; load(); }
  }
  function stopPlayback() { if (X.play) X.play.stop = true; closeDrawer(); }
  function toggleNew() {
    const b = q("#dl-newbox");
    b.hidden = !b.hidden;
    if (b.hidden) return;
    b.innerHTML = `<div class="row"><input id="dl-ntitle" placeholder="Call title" value="Controller call" style="flex:1"><button class="dl-btn" id="dl-nstart">Start</button></div>
      <div id="dl-nbody" hidden><div class="row"><input id="dl-nspk" placeholder="Name" size="12" maxlength="40"><input id="dl-nrole" placeholder="Role" size="12" maxlength="40"></div>
        <div class="row"><input id="dl-ntxt" placeholder="What was said… (Enter)" style="flex:1" maxlength="1000"><button class="dl-btn" id="dl-nadd">Add</button></div>
        <div class="row"><button class="dl-btn" id="dl-nrec">🎙 Record (AI transcribes)</button><label class="small"><input type="file" id="dl-nupl" accept=".wav,.mp3,audio/wav,audio/mpeg" hidden><span class="tag">⤴ Upload wav/mp3</span></label>
          <button class="dl-btn solid" id="dl-nend">End & analyse</button></div></div>`;
    q("#dl-nstart").onclick = (e) => busy(e.target, async () => {
      const r = await api("/api/ledger/call/start", { flight_id: S.flight, title: q("#dl-ntitle").value, source: "typed" });
      X.newCall = r.call_id; X.selCall = r.call_id; q("#dl-nbody").hidden = false; await load();
    });
    const add = async () => { if (!q("#dl-ntxt").value.trim()) return;
      try { await api("/api/ledger/call/line", { call_id: X.newCall, speaker: q("#dl-nspk").value || "Speaker", role: q("#dl-nrole").value, text: q("#dl-ntxt").value }); q("#dl-ntxt").value = ""; await load(); }
      catch (e) { toast(e.message, "bad"); } };
    q("#dl-nadd").onclick = add;
    q("#dl-ntxt").onkeydown = (e) => { if (e.key === "Enter") add(); };
    q("#dl-nend").onclick = (e) => busy(e.target, async () => { await api("/api/ledger/call/end", { call_id: X.newCall }); q("#dl-newbox").hidden = true; await extract(X.newCall); });
    q("#dl-nrec").onclick = record;
    q("#dl-nupl").onchange = async (e) => { const f = e.target.files[0]; if (f) await transcribe(await f.arrayBuffer(), /mp3|mpeg/.test(f.type + f.name) ? "mp3" : "wav"); e.target.value = ""; };
  }
  async function record() {
    const b = q("#dl-nrec");
    if (!X.rec) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true }), mr = new MediaRecorder(stream), chunks = [];
        mr.ondataavailable = (e) => chunks.push(e.data);
        X.rec = { stream, mr, chunks, timer: setTimeout(() => X.rec && record(), 170000) }; mr.start(); b.textContent = "⏹ Stop & transcribe";
      } catch (e) { toast(`Microphone unavailable: ${e.message}`, "bad"); }
      return;
    }
    const { stream, mr, chunks, timer } = X.rec; X.rec = null; clearTimeout(timer); b.textContent = "🎙 Record (AI transcribes)";
    await new Promise((res) => { mr.onstop = res; mr.stop(); });
    stream.getTracks().forEach((t) => t.stop());
    try {
      const dec = await new AudioContext().decodeAudioData(await new Blob(chunks).arrayBuffer());
      const off = new OfflineAudioContext(1, Math.ceil(dec.duration * 16000), 16000), s = off.createBufferSource();
      s.buffer = dec; s.connect(off.destination); s.start();
      const pcm = (await off.startRendering()).getChannelData(0), buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
      const w = (o, str) => [...str].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
      w(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
      v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, pcm.length * 2, true);
      pcm.forEach((x, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, x)) * 0x7fff, true));
      await transcribe(buf, "wav");
    } catch (e) { toast(`Could not process audio: ${e.message}`, "bad"); }
  }
  async function transcribe(arrayBuf, fmt) {
    const bytes = new Uint8Array(arrayBuf);
    if (bytes.length > 6_000_000) return toast("Audio is too long (max ~3 minutes)", "bad");
    let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    toast("Transcribing with the LLM…", "warn");
    try { const r = await api("/api/ledger/transcribe", { call_id: X.newCall, audio: btoa(bin), format: fmt }); toast(`${r.lines_added} line(s) transcribed`); await load(); }
    catch (e) { toast(e.message, "bad"); }
  }

  return { mount, stopPlayback };
})();
