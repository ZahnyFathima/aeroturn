// Live flight map: a pixel world map (rasterised from the globe's own equirectangular texture) with every inbound
// flight drifting slowly along a curved route into FRA. Hover a plane for its flight number, click it for its live report.
// Usage: FlightMap.mount(el, { hub:[lat, lon], color:(f) => css colour, label:(f) => html }) then .update(flights)
window.FlightMap = (() => {
  const W = 1000, LAT_TOP = 80, LAT_SPAN = 140, H = +(W * LAT_SPAN / 360).toFixed(1); // crop the poles, keep 80N..60S
  const CELL = 4, COLS = W / CELL, ROWS = Math.floor(H / CELL);
  const px = (lon) => ((lon + 180) / 360) * W, py = (lat) => ((LAT_TOP - lat) / LAT_SPAN) * H;
  const PLANE = "M9,0 L5,-1.2 L1.5,-1.2 L-2.5,-8 L-5,-8 L-2.5,-1.2 L-6,-1.2 L-8,-3.6 L-9.5,-3.6 L-8.5,0 L-9.5,3.6 L-8,3.6 L-6,1.2 L-2.5,1.2 L-5,8 L-2.5,8 L1.5,1.2 L5,1.2 Z";
  const NS = "http://www.w3.org/2000/svg";
  const hash = (s) => [...String(s)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let mask = null, maskP = null, raf = 0, st = null;

  // land mask: draw the SVG texture into a COLS x ROWS canvas and keep the cells that are mostly land
  function loadMask() {
    if (maskP) return maskP;
    maskP = new Promise((res, rej) => {
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas"); c.width = COLS; c.height = ROWS;
        const g = c.getContext("2d", { willReadFrequently: true });
        const sy = ((90 - LAT_TOP) / 180) * img.naturalHeight, sh = (LAT_SPAN / 180) * img.naturalHeight;
        g.drawImage(img, 0, sy, img.naturalWidth, sh, 0, 0, COLS, ROWS);
        const d = g.getImageData(0, 0, COLS, ROWS).data;
        mask = new Uint8Array(COLS * ROWS);
        for (let i = 0; i < mask.length; i++) mask[i] = d[i * 4 + 3] > 95 ? 1 : 0;
        res(mask);
      };
      img.onerror = rej;
      img.src = "/globe/globe-map.svg";
    });
    return maskP;
  }

  // paint the mask as square pixels with a 1px gap; land in grey with a little dither, sea as faint dots
  function paint(canvas) {
    canvas.width = COLS * CELL; canvas.height = ROWS * CELL;
    const g = canvas.getContext("2d");
    for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
      const land = mask[r * COLS + c], n = hash(r * 977 + c) % 9;
      if (land) {
        g.fillStyle = n === 0 ? "#3d3d3d" : n < 3 ? "#2c2c2c" : "#333333";
        g.fillRect(c * CELL, r * CELL, CELL - 1, CELL - 1);
      } else if ((r + c) % 2 === 0) {
        g.fillStyle = "#141414"; g.fillRect(c * CELL + 1, r * CELL + 1, 1, 1);
      }
    }
  }

  // quadratic route from origin to hub, bowed toward the pole like the reference map
  function route(f, hub) {
    const x0 = px(f.lon), y0 = py(f.lat), x2 = px(hub[1]), y2 = py(hub[0]);
    const dx = x2 - x0, dy = y2 - y0, d = Math.hypot(dx, dy) || 1;
    let nx = -dy / d, ny = dx / d;
    if (ny > 0) { nx = -nx; ny = -ny; }
    const k = Math.min(0.32, 40 / d + 0.18) * d;
    return { x0, y0, x1: (x0 + x2) / 2 + nx * k, y1: (y0 + y2) / 2 + ny * k, x2, y2 };
  }
  const at = (r, t) => {
    const u = 1 - t;
    return { x: u * u * r.x0 + 2 * u * t * r.x1 + t * t * r.x2, y: u * u * r.y0 + 2 * u * t * r.y1 + t * t * r.y2,
      a: Math.atan2(2 * u * (r.y1 - r.y0) + 2 * t * (r.y2 - r.y1), 2 * u * (r.x1 - r.x0) + 2 * t * (r.x2 - r.x1)) * 180 / Math.PI };
  };
  const el = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };

  function mount(root, opts) {
    cancelAnimationFrame(raf);
    root.innerHTML = `<div class="fmap-stage"><canvas aria-hidden="true"></canvas>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="group" aria-label="Inbound flights to FRA"></svg><div class="fmap-tip" hidden></div></div>`;
    const stage = root.querySelector(".fmap-stage"), svg = stage.querySelector("svg"), tip = stage.querySelector(".fmap-tip");
    const hub = opts.hub;
    st = { svg, tip, stage, opts, planes: new Map(), hot: null, last: performance.now(), arcs: el("g"), dots: el("g"), fleet: el("g") };
    const hx = px(hub[1]), hy = py(hub[0]);
    const hubG = el("g", { class: "fm-hub" });
    hubG.append(el("rect", { x: hx - 4, y: hy - 4, width: 8, height: 8, fill: "#f4f4f2" }),
      el("rect", { x: hx - 8, y: hy - 8, width: 16, height: 16, fill: "none", stroke: "#f4f4f2", "stroke-width": 1 }));
    const lbl = el("text", { x: hx + 12, y: hy - 8, class: "fm-hub-label" }); lbl.textContent = "FRA";
    svg.append(st.arcs, st.dots, hubG, lbl, st.fleet);
    loadMask().then(() => paint(stage.querySelector("canvas"))).catch(() => {});

    const open = (id) => { location.hash = `#/flight/${encodeURIComponent(id)}`; };
    const hover = (p) => {
      if (st.hot === p) return;
      if (st.hot) { st.hot.g.classList.remove("hot"); st.hot.arc.classList.remove("hot"); }
      st.hot = p;
      if (!p) { tip.hidden = true; return; }
      p.g.classList.add("hot"); p.arc.classList.add("hot");
      tip.innerHTML = opts.label(p.f); tip.hidden = false; place();
    };
    svg.addEventListener("pointerover", (e) => { const g = e.target.closest(".fm-plane"); if (g) hover(st.planes.get(g.dataset.id)); });
    svg.addEventListener("pointerout", (e) => { const g = e.target.closest(".fm-plane"); if (g && !g.contains(e.relatedTarget)) hover(null); });
    svg.addEventListener("click", (e) => { const g = e.target.closest(".fm-plane"); if (g) open(g.dataset.id); });
    svg.addEventListener("focusin", (e) => { const g = e.target.closest(".fm-plane"); if (g) hover(st.planes.get(g.dataset.id)); });
    svg.addEventListener("focusout", () => hover(null));
    svg.addEventListener("keydown", (e) => { const g = e.target.closest(".fm-plane"); if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(g.dataset.id); } });
    raf = requestAnimationFrame(tick);
  }

  function place() {
    const p = st.hot; if (!p) return;
    const pos = at(p.r, p.t);
    st.tip.style.left = `${(pos.x / W) * 100}%`; st.tip.style.top = `${(pos.y / H) * 100}%`;
    // keep the tooltip inside the map near the edges
    st.tip.style.transform = `translate(${pos.x < 90 ? "-10%" : pos.x > W - 90 ? "-90%" : "-50%"}, ${pos.y < 60 ? "18px" : "calc(-100% - 14px)"})`;
  }

  function update(flights) {
    if (!st) return;
    const { opts, planes } = st, keep = new Map();
    st.arcs.replaceChildren(); st.dots.replaceChildren(); st.fleet.replaceChildren();
    if (st.hot) { st.tip.hidden = true; st.hot = null; }
    for (const f of flights) {
      if (f.lat == null || f.lon == null) continue;
      const r = route(f, opts.hub), col = opts.color(f), h0 = hash(f.id);
      const arc = el("path", { d: `M${r.x0},${r.y0} Q${r.x1},${r.y1} ${r.x2},${r.y2}`, class: "fm-arc", stroke: col });
      const dot = el("rect", { x: r.x0 - 2.5, y: r.y0 - 2.5, width: 5, height: 5, fill: col, class: "fm-org" });
      const g = el("g", { class: "fm-plane", "data-id": f.id, tabindex: 0, role: "link", "aria-label": `${f.id} from ${f.origin}, open live report` });
      const body = el("g", { class: "body" });
      body.append(el("path", { d: PLANE, fill: col, stroke: "#000", "stroke-width": 0.8 }));
      g.append(el("circle", { r: 14, class: "hit" }), body);
      st.arcs.append(arc); st.dots.append(dot); st.fleet.append(g);
      // very slow: one crossing takes 80-140 s; each flight starts at its own point along the route
      keep.set(f.id, { f, r, g, arc, t: planes.get(f.id)?.t ?? (h0 % 1000) / 1000, dur: 80 + (h0 % 60) });
    }
    st.planes = keep;
    draw();
  }

  function draw() {
    for (const p of st.planes.values()) {
      const pos = at(p.r, p.t);
      const fade = Math.min(1, p.t / 0.04, (1 - p.t) / 0.04);
      p.g.setAttribute("transform", `translate(${pos.x.toFixed(2)},${pos.y.toFixed(2)}) rotate(${pos.a.toFixed(1)})`);
      p.g.style.opacity = fade.toFixed(2);
    }
  }

  function tick(now) {
    if (!st || !st.svg.isConnected) { st = null; return; } // the page re-rendered: stop
    const dt = Math.min(0.1, (now - st.last) / 1000); st.last = now;
    if (!still) for (const p of st.planes.values()) if (p !== st.hot) p.t = (p.t + dt / p.dur) % 1;
    draw(); place();
    raf = requestAnimationFrame(tick);
  }

  return { mount, update };
})();
