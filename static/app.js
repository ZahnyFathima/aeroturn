// AeroTurn OCC front end: hash router, no build step. All server text is escaped with h().
const $ = (s) => document.querySelector(s);
const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const sign = (n) => (n >= 0 ? "+" : "") + n;
const HUB = [50.03, 8.57];
// reference palette: taupe for on time, then the site's red (#e23d3d) blended into its cream, deepening with delay
const BANDS = [["On time", 0, 0, "#8d8d8d"], ["1–15 min", 1, 15, "#f8c9c1"], ["16–30 min", 16, 30, "#f19b95"],
  ["31–60 min", 31, 60, "#e96c69"], ["60+ min", 61, 1e9, "#e23d3d"]];
const PRIO = [["connections", "Maximise connections"], ["on_time", "Minimise network delay"], ["cost", "Minimise EU261 cost"], ["crew_rest", "Crew rest margin"]];
const S = { tab: "map", filters: new Set(BANDS.map((b) => b[0])), flight: "EK812", auth: { controller: "", pin: "" },
  prio: { connections: true, on_time: false, cost: false, crew_rest: false }, chaos: false };
let globe, rec;

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const band = (d) => BANDS.find((b) => d >= b[1] && d <= b[2]);
const delayChip = (d) => `<span class="tag" style="background:${band(d)[3]};color:#1b1815">${d ? "+" + d + " min" : "on time"}</span>`;
const tag = (t, cls = "") => `<span class="tag ${cls}">${h(t)}</span>`;
const statusTag = (s) => tag(s, { APPROVED: "ok", PENDING: "warn", REJECTED: "warn" }[s] || "bad");
const tierTag = (t) => tag(t, t === "STANDARD" ? "" : "warn");
const ACT = { DELAY: (a) => `Delay ${a.flight_id} +${a.minutes} min`, GATE_CHANGE: (a) => `${a.flight_id} → gate ${a.gate}`,
  CREW_SWAP: (a) => `${a.crew_id} → ${a.standby_id}`, CREW_AUTOSWAP: (a) => `Swap every illegal crew member (${a.flight_id})`,
  REBOOK: (a) => `${a.pax_id} → ${a.to_flight}`, REBOOK_AT_RISK: (a) => `Rebook all at-risk pax of ${a.flight_id}, vulnerable first`,
  REBOOK_TIERS: (a) => `Rebook only ${(a.tiers || []).join(", ")} of ${a.flight_id}`,
  ASSIGN_ENGINEER: (a) => `${a.engineer_id} → ${a.item_id}`, DEPARTURE_RELEASE: (a) => `Release ${a.flight_id} for departure` };
const actionsText = (acts) => acts.length ? acts.map((a) => h((ACT[a.kind] || (() => a.kind))(a))).join("<br>") : '<span class="muted">No actions</span>';
const notifList = (ns) => `<ul class="feed small">${ns.map((n) => `<li><b>${h(n.recipient)}</b>: ${h(n.message)} <span class="muted">${h(n.ts)}</span></li>`).join("") || '<li class="muted">No notifications yet</li>'}</ul>`;

function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = shortIds(msg); t.className = kind; t.hidden = false;
  clearTimeout(t.timer); t.timer = setTimeout(() => (t.hidden = true), 8000);
}
function resultToast(r) {
  const i = r.integrity ? ` · integrity: ${r.integrity.double_bookings} double bookings, ${r.integrity.orphan_bags} orphaned bags` : "";
  if (r.status === "APPROVED") toast(`Decision #${r.id} applied · ${r.notified} personnel notified${i}`);
  else if (r.status === "ROLLED_BACK") toast(`Decision #${r.id} rolled back atomically: ${r.reason}${i}`, "bad");
  else if (r.status === "BLOCKED") toast(`Blocked by guards: ${r.reason}`, "bad");
  else toast(`Decision #${r.id}: ${r.status}`, "warn");
}

function modal(title, fields, onOk, onCancel) {
  const m = $("#modal");
  m.innerHTML = `<div class="box"><h3>${title}</h3>${fields.map(([id, label, type, val]) =>
    `<label>${h(label)}<input id="m_${id}" type="${type}" value="${h(val)}" maxlength="40" autocomplete="off"></label>`).join("")}
    <div class="row"><button id="mok">Confirm</button><button id="mno" class="ghost">Cancel</button></div></div>`;
  m.hidden = false;
  $("#mno").onclick = () => { m.hidden = true; onCancel?.(); };
  $("#mok").onclick = async () => {
    const v = Object.fromEntries(fields.map(([id]) => [id, $("#m_" + id).value]));
    $("#mok").disabled = true;
    try { await onOk(v); m.hidden = true; } catch (e) { toast(e.message, "bad"); $("#mok").disabled = false; }
  };
}
const askAuth = (title, onOk, onCancel) => modal(title, [["controller", "Operations Controller name", "text", S.auth.controller],
  ["pin", "Controller PIN", "password", S.auth.pin]], async (v) => { S.auth = v; await onOk(v); }, onCancel);

function decide(fid, title, actions, after, chaos = false) {
  askAuth(`Controller approval: ${h(title)}<div class="small muted" style="margin-top:6px">${actionsText(actions)}</div>` +
    (chaos ? '<p class="bad small">Chaos on: a competing booking grabs a seat mid-batch</p>' : ""),
  async (a) => { const r = await api("/api/decide", { flight_id: fid, title, actions, chaos, ...a }); resultToast(r); after(r); refreshBell(); },
  () => after(null));
}

// ---------- main page (sections are chosen from the sidebar menu) ----------
// Operations = the controller's daily workflow; Assurance = safety / compliance tools that prove the system can be trusted
const SECTIONS = [["map", "Live overview", "Operations"], ["crew", "Crew", "Operations"], ["pax", "Connecting passengers", "Operations"],
  ["mx", "Maintenance", "Operations"], ["log", "Decision log", "Operations"], ["attack", "Guard Attack Mode", "Assurance"], ["replay", "Audit replay", "Assurance"]];
async function mainView() {
  $("#view").innerHTML = `<section id="tab"></section>`;
  await { map: mapTab, crew: crewTab, pax: paxTab, mx: mxTab, log: logTab, attack: () => Extras.attackTab($("#tab")), replay: () => Extras.replayTab($("#tab")) }[S.tab]();
}

// live overview cards either side of the globe
const sideList = (fs, empty) => fs.map((f) => `<a class="live-row" href="#/flight/${h(f.id)}"><span><b>${h(f.id)}</b> ${h(f.origin)}→FRA</span>${delayChip(f.delay_min)}
  <span class="small muted">${[f.at_risk && `${f.at_risk} connections at risk`, f.crew_illegal && `${f.crew_illegal} crew over limit`, f.open_nogo && "NO-GO maintenance open",
    f.turn_gap > 0 && `turnaround +${f.turn_gap} min`].filter(Boolean).map(h).join(" · ") || "No issues"}</span></a>`).join("") || `<p class="small muted">${empty}</p>`;

async function mapTab() {
  const flights = await api("/api/flights");
  const late = flights.filter((f) => f.delay_min).sort((a, b) => b.delay_min - a.delay_min), onTime = flights.filter((f) => !f.delay_min);
  const action = late.filter((f) => f.at_risk || f.crew_illegal || f.open_nogo);
  $("#tab").innerHTML = `<div class="globe-zone"><div class="globe-stage">
      <div class="brand"><h2 class="brand-name" aria-label="AeroTurn">${[..."AeroTurn"].map((c, i) => `<span style="--i:${i}">${c}</span>`).join("")}</h2>
        <p class="brand-sub">Turnaround and disruption recovery · FRA hub</p></div>
      <aside class="live-side" style="grid-column:1"><div class="live-head"><span class="live-dot"></span>Delayed · take action</div>
        <div class="live-big">${late.length}<span> of ${flights.length} inbound delayed</span></div>
        <p class="small live-note">${action.length} need a decision now</p>${sideList(late, "No delays right now")}</aside>
      <div class="globe-disc"><div id="globe"></div></div>
      <aside class="live-side" style="grid-column:3"><div class="live-head"><span class="live-dot ok"></span>On schedule</div>
        <div class="live-big">${onTime.length}<span> on time</span></div>
        <p class="small live-note">${flights.reduce((n, f) => n + f.at_risk, 0)} connections at risk network-wide</p>${sideList(onTime, "No flights on time")}
        <p class="small live-note">Drag the globe to spin it. Click any flight for its live report.</p></aside>
    </div></div>
    <div class="card fmap"><div class="fmap-head"><h3>Inbound traffic · live</h3>
      <div class="fmap-legend"><span><i style="background:#f4f4f2"></i>On time</span><span><i style="background:${BANDS[2][3]}"></i>Delayed</span>
      <span><i style="background:${BANDS[4][3]}"></i>60+ min</span></div></div><div id="fmap"></div>
      <p class="fmap-note">Hover a plane for its flight number · click it for the live report</p></div>
    <div class="card filters"><h3>All inbound flights</h3><div class="row">${BANDS.map((b) =>
    `<label><input type="checkbox" value="${b[0]}" ${S.filters.has(b[0]) ? "checked" : ""}><i style="background:${b[3]}"></i>${b[0]}
     <span class="muted">(${flights.filter((f) => band(f.delay_min) === b).length})</span></label>`).join("")}</div><table id="ftable"></table></div>`;
  const el = $("#globe"), ROUTE = "#fff8ed"; // on-time routes in cream so they read clearly on the black globe
  globe?._destructor();
  // dotted globe: a near-black sphere, land drawn as rings of dots in dark greens (see landDots / globeGlow below)
  globe = new Globe(el).showAtmosphere(true).atmosphereColor("#2f7d52").atmosphereAltitude(0.12).backgroundColor("rgba(0,0,0,0)")
    .width(el.offsetWidth).height(el.offsetHeight)
    .arcColor("color").arcStroke(1.1).arcDashLength(0.6).arcDashGap(0.2).arcDashAnimateTime(8000).arcsTransitionDuration(0)
    .pointColor(() => "#d6efdd").pointAltitude(0.006).pointRadius(0.45).pointResolution(8).pointsTransitionDuration(0)
    .particleLat("lat").particleLng("lng").particleAltitude(0.004).particlesColor(() => "#ffffff")
    .particlesSize(2).particlesSizeAttenuation(false); // CSS px; three.js scales it by the pixel ratio below
  const mat = globe.globeMaterial();
  mat.color.set("#0b130e");  /* a hair lighter than the page so the sphere just separates from the black */ mat.shininess = 0; mat.specular.set(0); mat.needsUpdate = true; // matte near-black body
  // the globe is enlarged 1.58x by CSS (and more while zoomed), so render at a matching resolution to keep the dots crisp
  globe.renderer().setPixelRatio(Math.min(3, (devicePixelRatio || 1) * 1.6)); globe.width(el.offsetWidth).height(el.offsetHeight);
  landDots().then((dots) => { if (globe && $("#globe") === el) { globe.particlesData([dots]); globeGlow(globe, dots); } }).catch(() => {});
  globe.pointOfView({ lat: 35, lng: 10 }); // look from the hub's latitude so the routes into FRA face the viewer
  const ctl = globe.controls();
  ctl.enableZoom = false; ctl.enablePan = false;
  ctl.minPolarAngle = ctl.maxPolarAngle = ((90 - 35) * Math.PI) / 180; // lock that tilt: drag spins sideways only
  ctl.autoRotate = true; ctl.autoRotateSpeed = 2;
  const draw = () => {
    const shown = flights.filter((f) => S.filters.has(band(f.delay_min)[0]));
    globe.arcsData(shown.map((f) => ({ startLat: f.lat, startLng: f.lon, endLat: HUB[0], endLng: HUB[1], color: Array(2).fill(f.delay_min ? band(f.delay_min)[3] : ROUTE) })))
      .pointsData(shown.map((f) => ({ lat: f.lat, lng: f.lon })).concat([{ lat: HUB[0], lng: HUB[1] }]));
    $("#ftable").innerHTML = `<thead><tr><th>Flight</th><th>From</th><th>Status</th><th>On-block</th><th>Delay</th><th>Connections at risk</th>
      <th>Crew margin</th><th>NO-GO mx</th><th>Turn gap</th></tr></thead><tbody>${shown.map((f) => `<tr class="click" data-id="${h(f.id)}">
      <td><b>${h(f.id)}</b></td><td>${h(f.origin)}</td><td>${h(f.status)}</td><td>T${sign(f.eff)}</td><td>${delayChip(f.delay_min)}</td>
      <td>${f.at_risk}/${f.connecting} ${f.vulnerable_at_risk ? tag(f.vulnerable_at_risk + " vulnerable", "warn") : ""}</td>
      <td class="${f.crew_min_margin < 0 ? "bad" : f.crew_min_margin < 60 ? "warn" : ""}">${f.crew_min_margin} min</td>
      <td class="${f.open_nogo ? "bad" : ""}">${f.open_nogo}</td><td class="${f.turn_gap > 0 ? "warn" : ""}">${sign(f.turn_gap)} min</td></tr>`).join("")}</tbody>`;
    FlightMap.update(shown);
  };
  FlightMap.mount($("#fmap"), { hub: HUB, color: (f) => (f.delay_min ? band(f.delay_min)[3] : "#f4f4f2"),
    label: (f) => `<b>${h(f.id)}</b>${h(f.origin)} → FRA · ${f.delay_min ? "+" + f.delay_min + " min" : "on time"}<br>
      <span class="muted">${h(f.status)}${f.at_risk ? ` · ${f.at_risk} connections at risk` : ""}</span><br><span class="go">Click to open live report →</span>` });
  $(".filters .row").onchange = (e) => { e.target.checked ? S.filters.add(e.target.value) : S.filters.delete(e.target.value); draw(); };
  $("#ftable").onclick = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) location.hash = `#/flight/${tr.dataset.id}`; };
  draw();
  globeScroll();
}
// Land dots: rings of evenly spaced dots (like a dot-matrix globe), kept where the globe texture has land. Built once, cached.
let landDotsP;
function landDots(step = 1.25) {
  return (landDotsP ??= new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => {
      const W = 720, H = 360, c = document.createElement("canvas"); c.width = W; c.height = H;
      const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(img, 0, 0, W, H);
      const a = g.getImageData(0, 0, W, H).data, land = (lat, lng) => a[(Math.min(H - 1, Math.floor((90 - lat) / 180 * H)) * W + Math.min(W - 1, Math.floor((lng + 180) / 360 * W))) * 4 + 3] > 100;
      const dots = [];
      for (let lat = -58; lat <= 82; lat += step) {
        const n = Math.max(1, Math.round((360 * Math.cos((lat * Math.PI) / 180)) / step));
        for (let i = 0; i < n; i++) { const lng = -180 + (i + 0.5) * (360 / n); if (land(lat, lng)) dots.push({ lat, lng }); }
      }
      res(dots);
    };
    img.onerror = rej; img.src = "/globe/globe-map.svg";
  }));
}
// Subtle green light play: each dot's shade follows a soft light from the viewer's upper left, so as the globe turns, dots
// brighten as they pass under it and dim toward the rim; a slow per-dot shimmer adds a little life. Colours only, no motion.
function globeGlow(g, dots) {
  let pts, col, nrm, seed, t0 = performance.now();
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DARK = [0.008, 0.05, 0.024], MID = [0.026, 0.19, 0.085], HI = [0.1, 0.43, 0.22]; // darker, slightly more saturated greens
  const frame = (now) => {
    if (globe !== g) return; // the globe was destroyed (page change): stop
    if (!pts) {
      g.scene().traverse((o) => { if (o.isPoints && !pts && o.geometry.attributes.position.count === dots.length) pts = o; });
      if (pts) {
        const pos = pts.geometry.attributes.position, n = pos.count;
        nrm = new Float32Array(n * 3); seed = new Float32Array(n);
        for (let i = 0; i < n; i++) {
          const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i), l = Math.hypot(x, y, z) || 1;
          nrm[i * 3] = x / l; nrm[i * 3 + 1] = y / l; nrm[i * 3 + 2] = z / l;
          seed[i] = ((i * 2654435761) >>> 0) / 4294967296; // stable pseudo-random per dot
        }
        col = new pos.constructor(new Float32Array(n * 3), 3);
        pts.geometry.setAttribute("color", col);
        pts.material.vertexColors = true; pts.material.color.set("#ffffff"); pts.material.needsUpdate = true;
      }
    }
    if (pts) {
      const cam = g.camera(), m = cam.matrixWorld.elements; // columns: right, up, back (toward the viewer)
      const V = new cam.position.constructor(m[8], m[9], m[10]);
      const L = new cam.position.constructor(m[8] - 0.45 * m[0] + 0.55 * m[4], m[9] - 0.45 * m[1] + 0.55 * m[5], m[10] - 0.45 * m[2] + 0.55 * m[6]);
      const inv = pts.matrixWorld.clone().invert(); V.transformDirection(inv); L.transformDirection(inv);
      const t = still ? 0 : (now - t0) / 1000, arr = col.array;
      for (let i = 0, n = seed.length; i < n; i++) {
        const nx = nrm[i * 3], ny = nrm[i * 3 + 1], nz = nrm[i * 3 + 2];
        const face = Math.max(0, nx * V.x + ny * V.y + nz * V.z), lit = Math.max(0, nx * L.x + ny * L.y + nz * L.z) ** 3;
        const shimmer = 1 + 0.09 * Math.sin(t * 0.7 + seed[i] * 40), tone = (0.8 + 0.35 * seed[i]) * shimmer;
        for (let k = 0; k < 3; k++) arr[i * 3 + k] = (DARK[k] + (MID[k] - DARK[k]) * face + (HI[k] - MID[k]) * lit) * tone;
      }
      col.needsUpdate = true;
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
// Scroll-driven zoom, as on the reference: across the tall dark zone the globe goes from 2x and pushed 135% down to 1x, centred.
function globeScroll() {
  const clamp = (x) => Math.min(1, Math.max(0, x)), inApp = scrollY >= $("#app").offsetTop - 2;
  const z = $(".globe-zone"), disc = $(".globe-disc"), brand = $(".brand");
  if (!z || !disc) return document.body.classList.toggle("hdr", inApp); // other pages: header whenever you're past the intro
  const top = z.getBoundingClientRect().top, p = clamp(-top / (z.offsetHeight - innerHeight));
  const v = clamp((p - 0.6) / 0.3); // side panels arrive once the globe has settled
  document.body.classList.toggle("hdr", inApp && p > 0.8); // header only after the opening name + globe
  if (top < innerHeight * 0.75) brand.classList.add("in"); // the name blurs in as the dark arrives
  brand.style.opacity = 1 - clamp((p - 0.25) / 0.35); // and gives way as the globe shrinks
  brand.style.transform = `translateY(${-12 * clamp(p / 0.6)}vh)`;
  // opening: the top half of a big globe under the name; scrolling shrinks it into the centre
  disc.style.transform = `translateY(${83 * (1 - p)}%) scale(${1.8 - 0.8 * p})`;
  document.querySelectorAll(".live-side").forEach((s, i) => {
    s.style.opacity = v; s.style.transform = `translateX(${(i ? 1 : -1) * 40 * (1 - v)}px)`; s.style.pointerEvents = v > 0.5 ? "auto" : "none";
  });
}
// The intro (sky, jet window, cloud fade) plays once. As soon as you are fully inside the console it is removed for the rest of
// the visit, keeping your place, so scrolling up on any page stops at the top of that page instead of flying back into the sky.
function retireIntro() {
  if (document.body.classList.contains("intro-done")) return;
  const top = $("#app").offsetTop;
  if (scrollY < top - 1) return;
  const y = scrollY - top;
  document.body.classList.add("intro-done");
  window.scrollTo({ top: y, behavior: "instant" });
}
window.addEventListener("scroll", () => { retireIntro(); requestAnimationFrame(globeScroll); }, { passive: true });
// the globe's edge mask drifts with the cursor (eased by a CSS transition on --mask-x), and the globe follows resizes
window.addEventListener("mousemove", (e) => $("#globe")?.style.setProperty("--mask-x", `${50 + 75 * (0.5 - e.clientX / innerWidth)}%`));
window.addEventListener("resize", () => { const el = $("#globe"); if (globe && el) globe.width(el.offsetWidth).height(el.offsetHeight); });

async function pickerTab(render) {
  const [flights, d] = await Promise.all([api("/api/flights"), api(`/api/flight/${S.flight}`)]);
  $("#tab").innerHTML = `<div class="row" style="margin-top:12px"><label>Flight <select id="pick">${flights.map((f) =>
    `<option value="${h(f.id)}" ${f.id === S.flight ? "selected" : ""}>${h(f.id)} ${h(f.origin)}→FRA ${f.delay_min ? "+" + f.delay_min + " min" : ""}</option>`).join("")}
    </select></label><a href="#/flight/${h(S.flight)}">Open live report →</a></div><div class="card" id="sub"></div>`;
  $("#pick").onchange = (e) => { S.flight = e.target.value; mainView(); };
  render(d, $("#sub"));
}

const crewTab = () => pickerTab((d, el) => {
  el.innerHTML = `<h3>Crew for ${h(d.outbound.id)} → ${h(d.outbound.dest)} (${h(d.outbound.aircraft_type)}, dep T${sign(d.outbound.eff)})
    <span class="muted small">· most urgent first · legal ceiling ${d.limits.MAX_FDP_MIN} min · dropdowns list only guard-approved standby</span></h3>
    <table><thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Rating</th><th>Duty used</th><th>Projected FDP</th><th>Margin</th><th>Replacement / standby</th></tr></thead>
    <tbody>${d.crew.map((c) => `<tr><td>${h(c.id)}</td><td>${h(c.name)}</td><td>${h(c.role)}</td><td>${h(c.type_rating)}</td><td>${c.duty_used} min</td>
    <td>${c.projected_fdp} min</td><td class="${c.margin < 0 ? "bad" : c.margin < 60 ? "warn" : "ok"}">${c.margin} min ${c.margin < 0 ? "illegal" : ""}</td>
    <td><select data-crew="${h(c.id)}"><option value="">${c.options.length ? "— choose standby —" : "no legal standby"}</option>${c.options.map((o) =>
      `<option value="${h(o.id)}">${h(o.id)} · ${h(o.name)} · ${h(o.location)} (${o.walk_min} min)</option>`).join("")}</select></td></tr>`).join("")}</tbody></table>`;
  el.onchange = (e) => { const s = e.target;
    if (s.dataset.crew && s.value) decide(d.flight.id, `Crew swap ${s.dataset.crew} → ${s.value}`, [{ kind: "CREW_SWAP", crew_id: s.dataset.crew, standby_id: s.value }],
      (r) => (r?.status === "APPROVED" ? mainView() : (s.value = ""))); };
});

// Connecting Passengers: Passenger Impact Agent (analysis + recommendation hand-off only; it never books anything)
const paxTab = () => pickerTab((d, el) => { el.className = ""; return PaxImpact.mount(el).catch((e) => toast(e.message, "bad")); });

// Maintenance: fleet-wide queue of open items, most urgent departure first (see Extras.maintenanceTab)
const mxTab = () => Extras.maintenanceTab($("#tab"));

// Decision log = every executed/pending decision + the Decision Ledger (calls -> AI-extracted, human-confirmed, guard-checked items)
const logTab = () => pickerTab((d, el) => { el.className = ""; return DecisionLog.mount(el).catch((e) => toast(e.message, "bad")); });

// ---------- live report page ----------
async function reportView(id) {
  S.flight = id;
  const d = await api(`/api/flight/${id}`), k = d.kpis, f = d.flight, o = d.outbound;
  const stats = [["Delay", `+${f.delay_min} min`, `on-block T${sign(k.eta)}`, f.delay_min > 30],
    ["Connecting passengers", k.connecting, `${k.at_risk} at risk · ${k.vulnerable_at_risk} vulnerable`, k.at_risk > 0],
    ["Crew duty margin", `${k.crew_min_margin} min`, `${k.crew_illegal} over the legal limit for ${o.id}`, k.crew_illegal > 0],
    ["Maintenance", `${k.open_nogo} NO-GO open`, `${d.maintenance.length} item(s) on ${f.tail}`, k.open_nogo > 0],
    ["Turnaround", `T${sign(k.off_block)}`, `off-block vs scheduled T${sign(k.outbound_dep)} (${sign(k.turn_gap)} min)`, k.turn_gap > 0],
    ["EU261 exposure", `€${k.pax_cost_eur.toLocaleString()}`, `do-nothing €${k.baseline_eur.toLocaleString()} · ${k.protected} protected`, k.pax_cost_eur > 0]];
  $("#view").innerHTML = `<div class="row"><a href="#/">← Network</a></div>
    <h2>${h(f.id)} <span class="muted">${h(f.origin)} → FRA</span> ${delayChip(f.delay_min)} ${tag(f.status)}</h2>
    <div class="row muted">${h(f.aircraft_type)} ${h(f.tail)} · outbound ${h(o.id)} → ${h(o.dest)} dep T${sign(o.eff)} · gate ${h(o.gate)} · ${h(o.status)}
      <button id="release" class="ghost">Propose departure release</button></div>
    <div class="card"><div class="row between"><h3>Live situation report</h3>
      <span><span id="rsrc" class="muted small"></span> <button id="regen" class="ghost">↻ Regenerate</button></span></div><div id="report" class="report muted">Generating report…</div></div>
    <div class="stats">${stats.map(([t, v, s, bad]) => `<div class="stat ${bad ? "alert" : ""}"><div class="muted small">${h(t)}</div><div class="big">${h(v)}</div><div class="small">${h(s)}</div></div>`).join("")}</div>
    <div class="card"><div class="row between"><h3>Top 3 agent suggestions <span class="muted small">· click one to see its passengers & bags</span></h3><a href="#/lab/${h(id)}"><button>Take to Scenario Lab →</button></a></div><div id="sugg" class="grid3"></div></div>
    <div class="card" id="x-pax" hidden></div>
    <div class="grid2"><div class="card"><h3>Calls on the system</h3><div class="row"><input id="spk" placeholder="Caller, e.g. Ramp control" maxlength="40">
      <button id="mic" class="ghost">Record call</button></div><textarea id="txt" rows="3" maxlength="2000" placeholder="Record (live transcription) or type the call…"></textarea>
      <button id="save">Save transcript → update report</button><ul class="feed">${d.comms.map((c) => `<li><b>${h(c.at)} · ${h(c.speaker)}</b><br>${h(c.text)}</li>`).join("")}</ul></div>
    <div class="card"><h3>Turnaround critical path</h3><p class="small">${d.turnaround.path.map(h).join(" → ")}</p><table>${d.turnaround.tasks.map((t) =>
      `<tr class="${t.critical ? "crit" : ""}"><td>${h(t.name)}</td><td>${t.minutes} min</td><td>ends T${sign(t.finish)}</td><td>${t.status === "DONE" ? tag("DONE", "ok")
      : `<button class="ghost small" data-task="${h(t.id)}">Mark done</button>`}</td></tr>`).join("")}</table>
      <h3 style="margin-top:14px">Decisions & notifications</h3>${d.decisions.slice(0, 5).map((c) => `<div class="small">#${c.id} ${statusTag(c.status)} ${h(c.title)}</div>`).join("") ||
      '<p class="muted small">No decisions yet</p>'}${notifList(d.notifications.slice(0, 8))}</div></div>`;
  $("#regen").onclick = () => loadReport(id, true);
  $("#release").onclick = () => decide(id, `Release ${o.id} for departure`, [{ kind: "DEPARTURE_RELEASE", flight_id: o.id }], (r) => r && reportView(id));
  $("#save").onclick = async () => {
    try { await api("/api/comms", { flight_id: id, speaker: $("#spk").value, text: $("#txt").value }); toast("Transcript saved, report updating"); reportView(id); }
    catch (e) { toast(e.message, "bad"); } };
  $("#mic").onclick = () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return toast("Live transcription isn't supported in this browser (use Chrome/Edge) — type the call instead", "warn");
    if (rec) return rec.stop();
    rec = new SR(); rec.continuous = true; rec.lang = "en-GB";
    rec.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal && $("#txt")) $("#txt").value += e.results[i][0].transcript + " "; };
    rec.onend = () => { rec = null; if ($("#mic")) $("#mic").textContent = "Record call"; };
    rec.start(); $("#mic").textContent = "Stop recording";
  };
  $("#view").onclick = async (e) => { const t = e.target.dataset.task;
    if (t) try { await api("/api/task", { flight_id: id, task_id: t }); reportView(id); } catch (err) { toast(err.message, "bad"); } };
  loadReport(id);
}

async function loadReport(id, force) {
  $("#report").textContent = "Generating report…";
  try {
    const r = force ? await api("/api/report", { flight_id: id }) : await api(`/api/report/${id}`);
    if (!$("#report") || S.flight !== id) return;
    $("#report").innerHTML = bullets(r.report); $("#report").classList.remove("muted");
    $("#rsrc").textContent = `AI · ${String(r.generated || "").slice(11, 16)}`; $("#rsrc").title = `${r.source} · ${r.generated} · from call transcripts + live data`;
    $("#sugg").innerHTML = r.suggestions.map((s, i) => `<div class="sugg x-pick" data-opt="${i}"><div class="muted small">Option ${i + 1}</div><b>${h(s.title)}</b>
      <p class="small clamp2" title="${h(s.why)}">${h(s.why)}</p><div class="small">${actionsText(s.actions)}</div>${s.revised_from ? `<div class="small warn" title="${h(shortIds(s.revised_from))}">↻ Revised after a guard refusal</div>` : ""}</div>`).join("");
    $("#sugg").onclick = (e) => { const c = e.target.closest("[data-opt]"); if (!c || !r.suggestions[c.dataset.opt].actions.length) return;
      document.querySelectorAll("#sugg .x-sel").forEach((x) => x.classList.remove("x-sel")); c.classList.add("x-sel");
      Extras.paxPanel($("#x-pax"), id, r.suggestions[c.dataset.opt]); };
  } catch (e) { toast(e.message, "bad"); }
}

// ---------- scenario lab ----------
async function labView(id) {
  S.flight = id;
  $("#view").innerHTML = `<div class="row"><a href="#/flight/${h(id)}">← Live report ${h(id)}</a></div><h2>Scenario Lab · ${h(id)}</h2>
    <div class="card"><h3>What should the recovery prioritise?</h3><div class="toggles">
    <label class="tog locked"><input type="checkbox" checked disabled> Protect vulnerable passengers <span class="muted small">(immutable rule)</span></label>
    ${PRIO.map(([k, l]) => `<label class="tog"><input type="checkbox" data-p="${k}" ${S.prio[k] ? "checked" : ""}> ${l}</label>`).join("")}
    <label class="tog chaos"><input type="checkbox" id="chaos" ${S.chaos ? "checked" : ""}> Inject mid-way seat conflict</label></div>
    <button id="regen">Ask agent for new options with these priorities</button> <span id="lsrc" class="muted small"></span>
    ${how("Every scenario is dry-run against the real guards inside a transaction that is rolled back, then ranked by code using your toggles.")}</div>
    <p class="muted small">Click a scenario to see its passengers & bags.</p><div id="scen" class="grid3"></div>
    <div class="card" id="x-pax" hidden></div><div class="card" id="x-cmp"></div><div class="card" id="x-wi"></div>`;
  $(".toggles").onchange = (e) => { if (e.target.id === "chaos") S.chaos = e.target.checked; else { S.prio[e.target.dataset.p] = e.target.checked; loadLab(id); } };
  $("#regen").onclick = () => loadLab(id, true);
  loadLab(id);
}

async function loadLab(id, regenerate = false) {
  $("#scen").innerHTML = `<p class="muted">${regenerate ? "Asking the agent, then simulating…" : "Simulating scenarios (dry run, rolled back)…"}</p>`;
  try {
    const c = await api("/api/compare", { flight_id: id, priorities: S.prio, regenerate, narrate: false });
    const r = { source: c.scenario_source, scenarios: c.rows };
    $("#lsrc").textContent = `Source: ${r.source}`;
    $("#scen").innerHTML = r.scenarios.map((s, i) => `<div class="card scen x-pick ${i === 0 && s.ok ? "best" : ""}" data-opt="${i}"><div class="row between"><b>#${i + 1} ${h(s.title)}</b>
      ${tag(s.ok ? "score " + s.score : "blocked", s.ok ? "" : "bad")}</div><p class="small">${h(s.why)}</p>${s.revised_from ? `<div class="small warn" title="${h(s.revised_from)}">↻ AI revised this after the guards refused: ${h(s.revised_from)}</div>` : ""}<div class="small">${actionsText(s.actions)}</div>
      ${s.ok ? `<div class="kpis">${[["Still at risk", s.kpis.at_risk], ["Vulnerable at risk", s.kpis.vulnerable_at_risk], ["Protected", `${s.kpis.protected}/${s.kpis.baseline_at_risk}`],
        ["Network delay added", `${s.kpis.network_delay_added} min`], ["Total cost", `€${s.kpis.total_cost_eur.toLocaleString()}`], ["Crew margin", `${s.kpis.crew_min_margin} min`],
        ["NO-GO open", s.kpis.open_nogo], ["Turn gap", `${sign(s.kpis.turn_gap)} min`]].map(([a, b]) => `<div><span class="muted small">${a}</span><b>${h(b)}</b></div>`).join("")}</div>
        <p class="ok small">Passes all guards (dry run)</p>` : `<p class="bad small">${h(s.reason)}</p>`}
      <button data-i="${i}" ${s.ok ? "" : "disabled"}>Choose this scenario</button></div>`).join("");
    $("#scen").onclick = (e) => { const s = r.scenarios[e.target.dataset.i];
      if (s) return decide(id, s.title, s.actions, (res) => res?.status === "APPROVED" && (location.hash = `#/flight/${id}`), S.chaos);
      const card = e.target.closest("[data-opt]"); if (!card || !r.scenarios[card.dataset.opt].actions.length) return;
      document.querySelectorAll("#scen .x-sel").forEach((x) => x.classList.remove("x-sel")); card.classList.add("x-sel");
      Extras.paxPanel($("#x-pax"), id, r.scenarios[card.dataset.opt]); };
    Extras.comparison($("#x-cmp"), id, c);
    Extras.whatif($("#x-wi"), id, r.scenarios).catch((e) => toast(e.message, "bad"));
  } catch (e) { toast(e.message, "bad"); }
}

// ---------- shell ----------
async function refreshBell() {
  try { const { notifications } = await api("/api/decisions"); $("#bellN").textContent = notifications.length;
    $("#bell").innerHTML = `<h3>Notifications to personnel</h3>${notifList(notifications.slice(0, 30))}`; } catch { /* ignore */ }
}
$("#bellBtn").onclick = () => ($("#bell").hidden = !$("#bell").hidden);
// hamburger menu: sections open from a left sidebar
const menu = (open) => { document.body.classList.toggle("menu-open", open); $("#menuBtn").setAttribute("aria-expanded", open); };
$("#menuBtn").onclick = () => menu(!document.body.classList.contains("menu-open"));
$("#scrim").onclick = () => menu(false);
$("#sidebar").onclick = (e) => { const k = e.target.closest("[data-sec]")?.dataset.sec; if (k) { menu(false); location.hash = `#/tab/${k}`; } };
const drawMenu = () => ($("#sidebar nav").innerHTML = SECTIONS.map(([k, l, g], i) =>
  `${g !== SECTIONS[i - 1]?.[2] ? `<div class="nav-group">${g}</div>` : ""}<button data-sec="${k}" class="${S.tab === k && !location.hash.match(/flight|lab/) ? "on" : ""}">${l}</button>`).join(""));
document.addEventListener("keydown", (e) => e.key === "Escape" && menu(false));
$("#reset").onclick = async () => { if (confirm("Reset the whole demo dataset?")) { await api("/api/reset", {}); S.tab = "map"; location.hash = "#/"; route(); } };
async function route() {
  const [, view, id] = location.hash.split("/");
  $("#view").onclick = null;
  DecisionLog.stopPlayback(); // a live demo call in the Decision log stops (and its transcript drawer closes) when you leave it
  globe?._destructor(); globe = null; // stop the globe's render loop when leaving the map
  if (view === "tab" && SECTIONS.some(([k]) => k === id)) S.tab = id;
  try { view === "flight" ? await reportView(id) : view === "lab" ? await labView(id) : await mainView(); } catch (e) { toast(e.message, "bad"); }
  refreshBell();
  drawMenu();
  globeScroll();
}
window.onhashchange = () => { // after the intro, page changes land at the top of the console, not back in the sky
  const top = $("#app").offsetTop; // skip past the intro
  if (window.scrollY > top) window.scrollTo(0, top);
  route();
};
route();
if (window.createFlight) createFlight().openShade(); // intro: shade lifts, scroll flies through the window into the OCC
