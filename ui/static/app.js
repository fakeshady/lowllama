"use strict";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  config: null,
  nodes: [],
  selected: [], // ordered node_ids; index = RPC index
  status: { status: "stopped" },
  reveal: false,
  history: [], // chat messages sent to /chat
  startingSince: null,
  busy: false,
};

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  init.headers["X-LowLlama-UI"] = "1"; // the local server refuses POSTs without it (blocks cross-site requests)
  const resp = await fetch(path, init);
  let data = null;
  try { data = await resp.json(); } catch (_) { data = null; } // non-JSON error page
  if (!resp.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${resp.status}`;
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function showBanner(text) {
  const el = $("banner");
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.textContent = text;
  el.hidden = false;
}

function redact(address) {
  if (!address) return "no address";
  if (state.reveal) return address;
  const m = /^(\d+)\.\d+\.\d+\.(\d+)$/.exec(address);
  return m ? `${m[1]}.*.*.${m[2]}` : "hidden";
}

function fmtAgo(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function fmtGb(v) { return v === null || v === undefined ? "?" : `${Number(v).toFixed(1)}`; }

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function startStarfield() {
  const canvas = $("starfield");
  const ctx = canvas.getContext("2d");
  let stars = [];
  let shooting = null;
  let w = 0, h = 0, dpr = 1;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth; h = window.innerHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.round((w * h) / 2600);
    stars = Array.from({ length: count }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      z: Math.random() * 0.8 + 0.2, // depth: bigger, brighter, faster when near
      phase: Math.random() * Math.PI * 2,
      hue: Math.random() < 0.15 ? (Math.random() < 0.5 ? 200 : 280) : 0,
    }));
  }

  function frame(t) {
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      if (!reduceMotion) {
        s.x -= s.z * 0.06;
        if (s.x < -2) { s.x = w + 2; s.y = Math.random() * h; }
      }
      const twinkle = reduceMotion ? 0.8 : 0.55 + 0.45 * Math.sin(t / 900 * s.z + s.phase);
      ctx.globalAlpha = twinkle * (0.35 + s.z * 0.65);
      ctx.fillStyle = s.hue ? `hsl(${s.hue}, 90%, 80%)` : "#ffffff";
      const r = s.z * 1.3;
      ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2); ctx.fill();
    }
    if (!reduceMotion) {
      if (!shooting && Math.random() < 0.003) {
        shooting = { x: Math.random() * w * 0.7 + w * 0.3, y: Math.random() * h * 0.4, life: 1 };
      }
      if (shooting) {
        const len = 120;
        const grad = ctx.createLinearGradient(shooting.x, shooting.y, shooting.x + len, shooting.y - len * 0.4);
        grad.addColorStop(0, "rgba(255,255,255,0.9)");
        grad.addColorStop(1, "rgba(110,231,255,0)");
        ctx.globalAlpha = shooting.life;
        ctx.strokeStyle = grad; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(shooting.x, shooting.y); ctx.lineTo(shooting.x + len, shooting.y - len * 0.4); ctx.stroke();
        shooting.x -= 9; shooting.y += 3.6; shooting.life -= 0.018;
        if (shooting.life <= 0) shooting = null;
      }
    }
    ctx.globalAlpha = 1;
    if (!reduceMotion) requestAnimationFrame(frame);
  }

  window.addEventListener("resize", () => { resize(); if (reduceMotion) frame(0); });
  resize();
  requestAnimationFrame(frame);
}

const CX = 400, CY = 220;
let orbitSeq = 0;

function orbitFor(i) {
  const ring = i % 4; // up to four rings; extra nodes share a ring, offset in angle
  const rx = 150 + ring * 68;
  return { rx, ry: rx * 0.42, speed: 0.00006 / (1 + ring * 0.45), offset: (i * 2.39996) % (Math.PI * 2) }; // golden angle spread
}

function createOrbitMap(root, opts = {}) {
  const id = `orbit${orbitSeq++}`; // gradient ids are per map so a hidden map never breaks a visible one
  const m = { root, layers: {}, planets: new Map(), key: "" };
  const defs = svg("defs", {}, root);
  const sun = svg("radialGradient", { id: `${id}-sun` }, defs);
  svg("stop", { offset: "0%", "stop-color": "#fde68a", "stop-opacity": "0.9" }, sun);
  svg("stop", { offset: "45%", "stop-color": "#f59e0b", "stop-opacity": "0.35" }, sun);
  svg("stop", { offset: "100%", "stop-color": "#f59e0b", "stop-opacity": "0" }, sun);
  const planet = svg("radialGradient", { id: `${id}-planet`, cx: "35%", cy: "35%" }, defs);
  svg("stop", { offset: "0%", "stop-color": "#e0fbff" }, planet);
  svg("stop", { offset: "55%", "stop-color": "#6ee7ff" }, planet);
  svg("stop", { offset: "100%", "stop-color": "#6d28d9" }, planet);
  root.style.setProperty("--sun-fill", `url(#${id}-sun)`);
  root.style.setProperty("--planet-fill", `url(#${id}-planet)`);
  root.classList.toggle("interactive", !!opts.interactive);
  m.layers.orbits = svg("g", {}, root);
  m.layers.beams = svg("g", {}, root);
  m.layers.back = svg("g", {}, root); // planets on the far side pass behind the master
  const master = svg("g", {}, root);
  svg("circle", { cx: CX, cy: CY, r: 62, class: "master-glow" }, master);
  svg("circle", { cx: CX, cy: CY, r: 24, class: "master-core" }, master);
  svg("text", { x: CX, y: CY + 48, class: "master-label" }, master).textContent = "Master";
  svg("text", { x: CX, y: CY + 63, class: "master-sub" }, master).textContent = opts.masterSub || "llama-server · CUDA0";
  m.layers.planets = svg("g", {}, root);
  m.empty = svg("text", { x: CX, y: 410, class: "empty-sky" }, root);

  function build(nodes) {
    const key = nodes.map((n) => n.node_id).join("|");
    if (key === m.key) return;
    m.key = key;
    ["orbits", "beams", "planets", "back"].forEach((k) => m.layers[k].replaceChildren());
    m.planets.clear();
    const rings = new Set();
    nodes.forEach((node, i) => {
      const o = orbitFor(i);
      if (!rings.has(o.rx)) {
        rings.add(o.rx);
        svg("ellipse", { cx: CX, cy: CY, rx: o.rx, ry: o.ry, class: "orbit", "data-rx": o.rx }, m.layers.orbits);
      }
      const beam = svg("line", { x1: CX, y1: CY, class: "beam" }, m.layers.beams);
      const g = svg("g", { class: "planet" }, m.layers.planets);
      const r = 9 + Math.min(10, (node.ram_gb || 4) * 0.6);
      svg("circle", { r: r + 9, class: "sel-ring" }, g);
      svg("circle", { r: r + 4, class: "rpc-ring" }, g);
      svg("circle", { r, class: "planet-body" }, g);
      const tag = svg("text", { y: -(r + 14), class: "rpc-tag" }, g);
      const label = svg("text", { y: r + 17, class: "planet-label" }, g);
      const sub = svg("text", { y: r + 31, class: "planet-sub" }, g);
      const title = svg("title", {}, g);
      if (opts.interactive) {
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "button");
        g.addEventListener("click", () => toggleSelect(node.node_id));
        g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSelect(node.node_id); } });
      }
      m.planets.set(node.node_id, { g, beam, tag, label, sub, title, orbit: o });
    });
  }

  m.update = (nodes, crew = [], runStatus = null) => {
    build(nodes);
    nodes.forEach((node) => {
      const p = m.planets.get(node.node_id);
      if (!p) return;
      const idx = crew.indexOf(node.node_id);
      p.g.setAttribute("class", ["planet", node.status === "online" ? "online" : "offline", node.rpc_ready ? "rpc" : "", idx >= 0 ? "selected" : ""].join(" ").trim());
      p.g.setAttribute("aria-label", `${node.node_name}, ${node.status}${idx >= 0 ? `, RPC${idx}` : ""}`);
      p.tag.textContent = idx >= 0 ? `RPC${idx}` : "";
      p.label.textContent = node.node_name;
      p.sub.textContent = opts.hideSub ? "" : `${fmtGb(node.free_ram_gb)}/${fmtGb(node.ram_gb)} GB`;
      p.title.textContent = opts.hideSub ? node.node_name : `${node.node_name} (${node.node_id})\n${node.status}, RPC ${node.rpc_ready ? "ready" : "not ready"}, seen ${fmtAgo(node.last_seen_s)}`;
      p.beam.setAttribute("class", idx >= 0 && runStatus ? `beam ${runStatus}` : "beam");
    });
    m.layers.orbits.querySelectorAll("ellipse").forEach((e) => e.classList.remove("selected"));
    crew.forEach((cid) => {
      const p = m.planets.get(cid);
      if (p) m.layers.orbits.querySelector(`ellipse[data-rx="${p.orbit.rx}"]`)?.classList.add("selected");
    });
    m.empty.textContent = nodes.length ? "" : (opts.empty || "");
  };

  function tick(t) {
    if (root.getClientRects().length) { // skip the work while this map's view is hidden
      const time = reduceMotion ? 0 : t;
      for (const p of m.planets.values()) {
        const a = p.orbit.offset + time * p.orbit.speed * 10;
        const x = CX + Math.cos(a) * p.orbit.rx;
        const y = CY + Math.sin(a) * p.orbit.ry;
        p.g.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
        p.beam.setAttribute("x2", x.toFixed(1));
        p.beam.setAttribute("y2", y.toFixed(1));
        const behind = Math.sin(a) < 0; // far side of the orbit: dimmer, and drawn behind the master
        const layer = behind ? m.layers.back : m.layers.planets;
        if (p.g.parentNode !== layer) layer.appendChild(p.g);
        p.g.style.opacity = behind ? "0.82" : "1";
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return m;
}

const maps = {};

function initMaps() {
  maps.dashboard = createOrbitMap($("orbit-map"), { interactive: true, empty: "No helpers in orbit yet. Generate an invite and join from a helper laptop." });
  maps.setup = createOrbitMap($("setup-orbit"), { empty: "Waiting for helpers to join..." });
  maps.hero = createOrbitMap($("hero-orbit"), { hideSub: true, masterSub: "holds the model" });
  maps.hero.update([1, 2, 3].map((i) => ({ node_id: `hero${i}`, node_name: `Helper ${i}`, status: "online", rpc_ready: true, ram_gb: 6 + i * 2 })),
    ["hero1", "hero2", "hero3"], "ready");
}

function updateMap() {
  const running = ["starting", "ready"].includes(state.status.status);
  const crew = running && state.status.worker_ids ? state.status.worker_ids : state.selected;
  maps.dashboard.update(state.nodes, crew, running ? state.status.status : null);
}

function toggleSelect(id) {
  const node = state.nodes.find((n) => n.node_id === id);
  const i = state.selected.indexOf(id);
  if (i >= 0) state.selected.splice(i, 1);
  else if (node && node.status === "online") state.selected.push(id);
  else showBanner(`${node ? node.node_name : id} is offline and cannot join the launch.`);
  renderAll();
}

const fleetRows = new Map(); // node_id -> row elements, reused so a click is never lost to a re-render

function fleetRow(node) {
  let row = fleetRows.get(node.node_id);
  if (row) return row;
  const li = el("li");
  const box = el("input");
  box.type = "checkbox";
  box.addEventListener("change", () => toggleSelect(node.node_id));
  const name = el("span", "name");
  const stateTag = el("span", "state");
  const bar = el("div", "ram-bar");
  const fill = el("i");
  bar.appendChild(fill);
  bar.title = "free RAM";
  const meta = el("span", "meta");
  li.append(box, name, stateTag, bar, meta);
  row = { li, box, name, stateTag, fill, meta };
  fleetRows.set(node.node_id, row);
  return row;
}

function renderFleet() {
  const list = $("fleet-list");
  $("fleet-count").textContent = String(state.nodes.length);
  if (!state.nodes.length) {
    fleetRows.clear();
    list.replaceChildren(el("li", "empty", "No helpers registered"));
    return;
  }
  const live = new Set(state.nodes.map((n) => n.node_id));
  for (const id of [...fleetRows.keys()]) if (!live.has(id)) fleetRows.delete(id);
  const rows = state.nodes.map((node) => {
    const row = fleetRow(node);
    const online = node.status === "online";
    const selected = state.selected.includes(node.node_id);
    row.li.className = `${online ? "online" : "offline"}${selected ? " selected" : ""}`;
    row.box.checked = selected;
    row.box.disabled = !online && !selected;
    row.box.setAttribute("aria-label", `Select ${node.node_name}`);
    row.name.textContent = node.node_name;
    row.stateTag.className = `state ${online ? "online" : "offline"}`;
    row.stateTag.textContent = online ? (node.rpc_ready ? "online · rpc" : "online") : "offline";
    const known = node.ram_gb && node.free_ram_gb !== null && node.free_ram_gb !== undefined;
    row.fill.style.width = `${known ? Math.max(0, Math.min(100, (node.free_ram_gb / node.ram_gb) * 100)) : 0}%`;
    row.meta.textContent = `${fmtGb(node.free_ram_gb)} of ${fmtGb(node.ram_gb)} GB free · ${redact(node.address)}:${node.rpc_port} · ${fmtAgo(node.last_seen_s)}`;
    return row.li;
  });
  const same = rows.length === list.children.length && rows.every((li, i) => list.children[i] === li);
  if (!same) list.replaceChildren(...rows);
}

function renderLaunch() {
  const s = state.status.status || "stopped";
  const chip = $("status-chip");
  chip.dataset.status = s;
  chip.textContent = s;
  const running = s === "starting" || s === "ready";
  const crew = running && state.status.worker_ids ? state.status.worker_ids : state.selected;
  const list = $("crew-list");
  list.replaceChildren();
  if (!crew.length) list.appendChild(el("li", "muted", "No helpers selected"));
  crew.forEach((id, i) => {
    const node = state.nodes.find((n) => n.node_id === id);
    const li = el("li");
    li.appendChild(el("span", "tag", `RPC${i}`));
    li.appendChild(el("span", "", node ? node.node_name : id));
    list.appendChild(li);
  });
  $("launch-btn").disabled = state.busy || running || !state.selected.length;
  $("stop-btn").disabled = state.busy || s === "stopped";
  $("model-select").disabled = running;
  const detail = $("status-detail");
  detail.classList.toggle("error", s === "error");
  if (s === "error") detail.textContent = state.status.error || "The launcher reported an error.";
  else if (s === "starting") detail.textContent = `Loading weights onto ${crew.length} helper${crew.length === 1 ? "" : "s"}. The first load can take a minute.`;
  else if (s === "ready") detail.textContent = `Online: ${state.status.model_id} across the master and ${crew.length} helper${crew.length === 1 ? "" : "s"}.`;
  else detail.textContent = state.selected.length ? "Ready for launch." : "Select at least one online helper.";
  if (s === "starting" && !state.startingSince) state.startingSince = Date.now();
  if (s !== "starting") state.startingSince = null;
  $("send-btn").disabled = s !== "ready" || state.busyChat;
}

function renderAll() {
  renderFleet();
  renderLaunch();
  updateMap();
}

async function launch() {
  state.busy = true; renderLaunch(); showBanner("");
  try {
    state.status = await api("/api/start", { method: "POST", body: { model_id: $("model-select").value, node_ids: state.selected } });
  } catch (e) {
    state.status = { status: "error", error: e.message };
  } finally {
    state.busy = false; renderAll();
  }
}

async function stop() {
  state.busy = true; renderLaunch();
  try {
    await api("/api/stop", { method: "POST" });
    state.status = { status: "stopped" };
  } catch (e) {
    showBanner(`Stop failed: ${e.message}`);
  } finally {
    state.busy = false; renderAll();
  }
}

const all = (sel) => [...document.querySelectorAll(sel)];

async function makeInvite() {
  all("[data-invite-btn]").forEach((b) => { b.disabled = true; });
  try {
    const inv = await api("/api/invite", { method: "POST" });
    state.invite = inv.invite_code;
    all("[data-invite-code]").forEach((c) => { c.textContent = inv.invite_code; });
    all("[data-copy-btn]").forEach((b) => { b.disabled = false; });
    const exp = new Date(inv.expires_at);
    const text = isNaN(exp) ? `Expires ${inv.expires_at}` : `Expires at ${exp.toLocaleTimeString()}`;
    all("[data-invite-expiry]").forEach((e) => { e.textContent = text; });
  } catch (e) {
    showBanner(`Invite failed: ${e.message}`);
  } finally {
    all("[data-invite-btn]").forEach((b) => { b.disabled = false; });
  }
}

async function copyText(text, btn, label, sourceEl) {
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copied";
  } catch (_) {
    const range = document.createRange(); // clipboard API blocked: select the text so Ctrl+C works
    range.selectNodeContents(sourceEl);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    btn.textContent = "Press Ctrl+C";
  }
  setTimeout(() => { btn.textContent = label; }, 1600);
}

function addMsg(cls, text, meta) {
  const log = $("chat-log");
  const m = el("div", `msg ${cls}`, text);
  if (meta) m.appendChild(el("span", "meta", meta));
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
  return m;
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || state.busyChat) return;
  if (state.status.status !== "ready") { addMsg("error", "The model is not ready. Launch it first."); return; }
  input.value = "";
  addMsg("user", text);
  state.history.push({ role: "user", content: text });
  state.busyChat = true; renderLaunch();
  const pending = addMsg("assistant pending", "");
  const t0 = performance.now();
  try {
    const data = await api("/api/chat", { method: "POST", body: { messages: state.history, max_tokens: 256 } });
    const reply = data && data.choices && data.choices[0] && data.choices[0].message ? data.choices[0].message.content : "";
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    const tokens = data && data.usage && data.usage.completion_tokens ? ` · ${data.usage.completion_tokens} tokens` : "";
    pending.remove();
    addMsg("assistant", reply || "(empty answer)", `${secs}s${tokens} · ${(state.status.worker_ids || []).length} helper(s)`);
    state.history.push({ role: "assistant", content: reply || "" });
  } catch (e) {
    pending.remove();
    state.history.pop(); // keep user/assistant turns paired for the next request
    addMsg("error", `Transmission failed: ${e.message}`);
  } finally {
    state.busyChat = false; renderLaunch();
    input.focus();
  }
}


const VIEWS = ["welcome", "master", "helper", "dashboard"];
const ROLE_KEY = "lowllama.role";
const GOOD_AGENT = { helper_registered: "registered with the master", helper_reconnected: "reconnected to the master",
  heartbeat_sent: "heartbeats flowing", heartbeat_restored: "heartbeats restored" };
const WAIT_AGENT = { helper_agent_started: "agent starting", stored_registration_loaded: "loading this laptop's saved registration",
  enrollment_connection_failed: "cannot reach the master yet, retrying", heartbeat_failed: "heartbeat failed, retrying",
  heartbeat_retry_failed: "heartbeat failed, retrying", reconnect_failed: "reconnect failed, retrying",
  reenrollment_scheduled: "re-joining shortly", registration_credentials_rejected: "the master forgot this laptop, re-joining with the invite" };
const BAD_AGENT = { enrollment_rejected: "invite rejected: ask the master for a new code", enrollment_conflict: "this laptop is already enrolled under another identity",
  enrollment_outcome_unknown: "join result unknown: check the master, then Leave and Join again",
  credential_persistence_failed: "could not save this laptop's credentials", helper_agent_startup_failed: "agent stopped: see the log below",
  shutdown_requested: "agent stopped" };

function savedRole() { try { return localStorage.getItem(ROLE_KEY); } catch (_) { return null; } }
function saveRole(role) { try { localStorage.setItem(ROLE_KEY, role); } catch (_) { } }

function route() {
  let view = location.hash.replace("#", "");
  const role = savedRole();
  if (!VIEWS.includes(view)) view = role === "master" || role === "helper" ? role : "welcome";
  if (view === "dashboard" && role === "helper") view = "helper"; // helpers have no admin access
  if (view === "master" || view === "helper") saveRole(view);
  if (view === "dashboard") saveRole("master");
  state.view = view;
  VIEWS.forEach((v) => { $(`view-${v}`).hidden = v !== view; });
  const r = savedRole();
  const pill = $("role-pill");
  pill.hidden = !r || view === "welcome";
  pill.textContent = r === "master" ? "Master" : "Helper";
  $("nav-setup").hidden = view === "welcome";
  $("nav-setup").href = r ? `#${r}` : "#welcome";
  $("nav-setup").classList.toggle("active", view === "master" || view === "helper");
  $("nav-dashboard").hidden = r !== "master" || view === "welcome";
  $("nav-dashboard").classList.toggle("active", view === "dashboard");
  showBanner("");
  if (view === "master" || view === "helper") pollSetup();
  if (view === "dashboard") { pollHealth(); pollNodes(); pollStatus(); }
  window.scrollTo(0, 0);
}

function setStep(id, stepState, detail) {
  const step = $(id);
  step.dataset.state = stepState;
  const d = step.querySelector("[data-detail]");
  if (d && detail !== undefined) d.textContent = detail;
}

function applyFlow(steps) {
  let activeGiven = false; // first unfinished step is "active", later ones "todo"
  for (const [id, s, detail] of steps) {
    let st = s;
    if (s === "pending") { st = activeGiven ? "todo" : "active"; activeGiven = true; }
    else if (s === "error" || s === "busy") { activeGiven = true; st = s === "busy" ? "active" : "error"; }
    setStep(id, st, detail);
  }
}

function showLog(root, name, proc) {
  const box = root.querySelector(`[data-log="${name}"]`);
  if (!box) return;
  const lines = proc && proc.tail ? proc.tail : [];
  box.hidden = !(proc && proc.started_at) || !lines.length;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  box.textContent = lines.join("\n") + (proc && proc.log ? `\n\n(full log: ${proc.log})` : "");
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function installStep(s) {
  const p = s.processes.install;
  if (s.runtime.ok) return ["done", s.runtime.detail];
  if (p.running) return ["busy", "downloading... this can take several minutes"];
  if (p.exit_code !== null && p.exit_code !== 0) return ["error", `download failed (exit ${p.exit_code}); see the log below, then try again`];
  return ["pending", s.runtime.detail];
}

function setInstallButton(root, role, s) {
  const btn = root.querySelector(`[data-install="${role}"]`);
  btn.disabled = state.busy || s.runtime.ok || s.processes.install.running;
  btn.textContent = s.runtime.ok ? "Downloaded" : s.processes.install.running ? "Downloading..." : "Download";
}

function renderMasterSetup(s) {
  const root = $("view-master");
  const ts = s.tailscale.ok ? ["done", `connected as ${s.tailscale.ip}`] : ["pending", s.tailscale.detail];
  const rt = installStep(s);
  const gpu = s.cuda.ok ? ["done", s.cuda.detail] : (s.runtime.ok ? ["error", s.cuda.detail] : ["pending", s.cuda.detail]);
  const c = s.coordinator;
  const cp = s.processes.coordinator;
  let coord;
  if (c.reachable && c.token_known) coord = ["done", `online at ${c.url}${c.started_here ? "" : " (started outside this page)"}`];
  else if (c.reachable) coord = ["error", "a coordinator is running, but this page has no admin key. Restart ui/server.py with LOWLLAMA_ADMIN_TOKEN set, or stop that coordinator and start it here."];
  else if (cp.running) coord = ["busy", "starting..."];
  else if (cp.exit_code !== null && cp.exit_code !== 0) coord = ["error", `coordinator exited (code ${cp.exit_code}); see the log below`];
  else coord = ["pending", "not running"];
  const online = state.nodes.filter((n) => n.status === "online");
  const inv = online.length ? ["done", `${online.length} helper${online.length === 1 ? "" : "s"} in orbit`] : ["pending", "waiting for helpers to join"];
  applyFlow([["m-step-ts", ...ts], ["m-step-runtime", ...rt], ["m-step-gpu", ...gpu], ["m-step-coord", ...coord], ["m-step-invite", ...inv]]);
  setInstallButton(root, "master", s);
  $("coord-start").disabled = state.busy || cp.running || c.reachable || !s.tailscale.ok;
  $("coord-stop").disabled = state.busy || !cp.running;
  root.querySelectorAll("[data-invite-btn]").forEach((b) => { b.disabled = !(c.reachable && c.token_known); });
  $("open-dashboard").disabled = !(c.reachable && c.token_known);
  showLog(root, "install", s.processes.install);
  showLog(root, "coordinator", cp);
  maps.setup.update(state.nodes);
  const list = $("joined-list");
  list.replaceChildren(...state.nodes.map((n) => {
    const li = el("li", n.status === "online" ? "" : "offline");
    li.append(el("i"), el("span", "", n.node_name), el("span", "muted", `${n.status}${n.rpc_ready ? " · RPC ready" : ""}`));
    return li;
  }));
}

function renderHelperSetup(s) {
  const root = $("view-helper");
  const ts = s.tailscale.ok ? ["done", `connected as ${s.tailscale.ip}`] : ["pending", s.tailscale.detail];
  const rt = installStep(s);
  const info = state.codeInfo;
  let code;
  if (state.codeError) code = ["error", state.codeError];
  else if (info && info.reachable) code = ["done", `master ${info.master_ip}:${info.port} is reachable`];
  else if (info) code = ["error", `master ${info.master_ip}:${info.port} is not reachable. Is its coordinator started, and are you both on the same Tailscale network?`];
  else code = ["pending", "paste the code; it is checked automatically"];
  const name = $("node-name").value.trim() ? ["done", ""] : ["pending", ""];
  const h = s.helper;
  const agent = s.processes.agent;
  const rpc = s.processes.rpc;
  const ev = h.agent_event;
  let agentCls = "";
  let agentText = "Registered with the master, sending heartbeats";
  if (agent.running && GOOD_AGENT[ev]) { agentCls = "on"; agentText = `Agent: ${GOOD_AGENT[ev]}`; }
  else if (agent.running) { agentCls = "wait"; agentText = `Agent: ${WAIT_AGENT[ev] || BAD_AGENT[ev] || "starting"}`; }
  else if (agent.started_at) { agentCls = "bad"; agentText = `Agent: ${BAD_AGENT[ev] || `stopped (exit ${agent.exit_code})`}`; }
  const rpcCls = h.rpc_listening ? "on" : rpc.running ? "wait" : rpc.started_at ? "bad" : "";
  $("chk-rpc").className = `check ${rpcCls}`;
  $("chk-rpc").querySelector("span").textContent = h.rpc_listening ? `RPC server listening on ${s.tailscale.ip}:50052`
    : rpc.started_at && !rpc.running ? `RPC server stopped (exit ${rpc.exit_code})` : "RPC server listening on port 50052";
  $("chk-agent").className = `check ${agentCls}`;
  $("chk-agent").querySelector("span").textContent = agentText;
  let joinState;
  if (agentCls === "on" && h.rpc_listening) joinState = ["done", "This laptop is in orbit. Keep it awake and plugged in, and keep this page's server running."];
  else if (agentCls === "bad" || rpcCls === "bad") joinState = ["error", "something stopped: see the checks and log below"];
  else if (agent.running || rpc.running) joinState = ["busy", "joining..."];
  else joinState = ["pending", ""];
  applyFlow([["h-step-ts", ...ts], ["h-step-runtime", ...rt], ["h-step-code", ...code], ["h-step-name", ...name], ["h-step-join", ...joinState]]);
  setInstallButton(root, "helper", s);
  $("join-btn").disabled = state.busy || agent.running || !(info && info.reachable) || !s.tailscale.ok || !s.runtime.ok || !$("node-name").value.trim();
  $("leave-btn").disabled = state.busy || !(agent.running || rpc.running);
  showLog(root, "install", s.processes.install);
  showLog(root, "agent", agent);
  const masterIp = info ? info.master_ip : "<MASTER_TAILSCALE_IP>";
  $("fw-cmd").textContent = `New-NetFirewallRule -DisplayName 'LowLlama RPC from master' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 50052 -LocalAddress ${s.tailscale.ip || "<THIS_LAPTOP_TAILSCALE_IP>"} -RemoteAddress ${masterIp} -Program '${h.rpc_exe}' -Profile Any`;
}

let setupTimer = null;
async function pollSetup() {
  clearTimeout(setupTimer);
  const view = state.view;
  if (view !== "master" && view !== "helper") return;
  try {
    const s = await api(`/api/setup/state?role=${view}`);
    state.setup = s;
    if (view === "master" && s.coordinator.reachable && s.coordinator.token_known) await refreshNodes(false);
    if (state.view === view) (view === "master" ? renderMasterSetup : renderHelperSetup)(s);
    if ($("banner").textContent.startsWith("Setup check failed")) showBanner("");
  } catch (e) {
    showBanner(`Setup check failed: ${e.message}`);
  }
  if (state.view === view) setupTimer = setTimeout(pollSetup, 2000);
}

async function refreshNodes(bannerOnError) {
  try {
    const nodes = await api("/api/nodes");
    state.nodes = Array.isArray(nodes) ? nodes : [];
    const online = new Set(state.nodes.filter((n) => n.status === "online").map((n) => n.node_id));
    state.selected = state.selected.filter((id) => online.has(id)); // a helper that went offline leaves the crew
    if ($("banner").textContent.startsWith("Fleet:")) showBanner("");
  } catch (e) {
    if (bannerOnError) showBanner(`Fleet: ${e.message}`);
  }
}

let nodesTimer = null;
async function pollNodes() {
  clearTimeout(nodesTimer);
  if (state.view !== "dashboard") return;
  await refreshNodes(true);
  renderAll();
  nodesTimer = setTimeout(pollNodes, 3000);
}

let statusTimer = null;
async function pollStatus() {
  clearTimeout(statusTimer);
  if (state.view !== "dashboard") return;
  try {
    if (!state.busy) state.status = await api("/api/status");
  } catch (e) {
    state.status = { status: "error", error: e.message };
  }
  renderLaunch(); updateMap();
  statusTimer = setTimeout(pollStatus, 2000);
}

let healthTimer = null;
async function pollHealth() {
  clearTimeout(healthTimer);
  if (state.view !== "dashboard") return;
  const pill = $("link-pill");
  try {
    const h = await api("/api/health");
    pill.dataset.state = h.coordinator;
    $("link-text").textContent = h.coordinator === "simulation" ? "Simulated coordinator" : `Coordinator ${h.coordinator}`;
  } catch (_) {
    pill.dataset.state = "unreachable";
    $("link-text").textContent = "Dashboard server down";
  }
  healthTimer = setTimeout(pollHealth, 5000);
}

function tickClock() {
  $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
  const timer = $("launch-timer");
  if (state.startingSince) {
    const s = Math.floor((Date.now() - state.startingSince) / 1000);
    timer.textContent = `T+ ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  } else {
    timer.textContent = "";
  }
}

async function runAction(fn, label) {
  state.busy = true;
  if (state.setup) (state.view === "master" ? renderMasterSetup : renderHelperSetup)(state.setup);
  try { await fn(); showBanner(""); }
  catch (e) { showBanner(`${label}: ${e.message}`); }
  finally { state.busy = false; pollSetup(); }
}

let codeTimer = null;
async function checkCode() {
  const code = $("invite-input").value.trim();
  state.codeInfo = null; state.codeError = null;
  $("code-result").className = "small";
  if (!code) { $("code-result").textContent = ""; pollSetup(); return; }
  $("code-result").textContent = "checking...";
  try {
    const info = await api("/api/setup/invite-info", { method: "POST", body: { code } });
    if ($("invite-input").value.trim() !== code) return; // the text changed while checking
    state.codeInfo = info;
    const mins = info.expires_in_s === null ? null : Math.round(info.expires_in_s / 60);
    const parts = [info.reachable ? "master reachable" : "master not reachable"];
    if (mins !== null) parts.push(mins > 0 ? `code expires in about ${mins} min` : "code looks expired: ask for a new one");
    if (!info.in_tailnet) parts.push("warning: the master address is not a Tailscale address");
    $("code-result").textContent = parts.join(" · ");
    $("code-result").className = `small ${info.reachable && (mins === null || mins > 0) ? "ok-text" : "bad-text"}`;
  } catch (e) {
    state.codeError = e.message;
    $("code-result").textContent = "";
  }
  pollSetup();
}

async function boot() {
  startStarfield();
  initMaps();
  setInterval(tickClock, 1000); tickClock();
  try {
    state.config = await api("/api/config");
  } catch (e) {
    showBanner(`Could not reach the local dashboard server: ${e.message}`);
    state.config = { mock: false, models: [], default_model: "", token_set: false, hostname: "" };
  }
  $("mode-badge").hidden = !state.config.mock;
  const select = $("model-select");
  for (const id of state.config.models) {
    const opt = el("option", "", id);
    opt.value = id;
    if (id === state.config.default_model) opt.selected = true;
    select.appendChild(opt);
  }
  $("node-name").value = state.config.hostname || "";
  all(".role-card").forEach((card) => card.addEventListener("click", () => {
    saveRole(card.dataset.role);
    location.hash = card.dataset.role;
  }));
  all("[data-install]").forEach((b) => b.addEventListener("click", () =>
    runAction(() => api("/api/setup/install", { method: "POST", body: { role: b.dataset.install } }), "Download")));
  $("coord-start").addEventListener("click", () => runAction(() => api("/api/setup/coordinator/start", { method: "POST" }), "Start coordinator"));
  $("coord-stop").addEventListener("click", () => runAction(() => api("/api/setup/coordinator/stop", { method: "POST" }), "Stop coordinator"));
  $("open-dashboard").addEventListener("click", () => { location.hash = "dashboard"; });
  $("check-code").addEventListener("click", checkCode);
  $("invite-input").addEventListener("input", () => {
    state.codeInfo = null; state.codeError = null; $("code-result").textContent = "";
    clearTimeout(codeTimer);
    codeTimer = setTimeout(checkCode, 500); // checks automatically shortly after a paste
  });
  $("node-name").addEventListener("input", () => { if (state.setup && state.view === "helper") renderHelperSetup(state.setup); });
  $("join-btn").addEventListener("click", () => runAction(() => api("/api/setup/helper/start", { method: "POST", body: {
    code: $("invite-input").value.trim(), node_name: $("node-name").value.trim(), threads: Number($("threads").value) } }), "Join"));
  $("leave-btn").addEventListener("click", () => runAction(() => api("/api/setup/helper/stop", { method: "POST" }), "Leave"));
  $("fw-copy").addEventListener("click", () => copyText($("fw-cmd").textContent, $("fw-copy"), "Copy command", $("fw-cmd")));
  $("launch-btn").addEventListener("click", launch);
  $("stop-btn").addEventListener("click", stop);
  all("[data-invite-btn]").forEach((b) => b.addEventListener("click", makeInvite));
  all("[data-copy-btn]").forEach((b) => b.addEventListener("click", () => {
    const codeEl = b.closest(".invite").querySelector("[data-invite-code]");
    copyText(state.invite || codeEl.textContent, b, "Copy", codeEl);
  }));
  $("chat-form").addEventListener("submit", sendChat);
  $("clear-btn").addEventListener("click", () => {
    state.history = [];
    $("chat-log").replaceChildren(el("div", "msg system", "Console cleared. The next message starts a new conversation."));
  });
  $("reveal-toggle").addEventListener("change", (e) => { state.reveal = e.target.checked; renderFleet(); });
  window.addEventListener("hashchange", route);
  route();
}

boot();
