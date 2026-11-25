"use strict";

/* Shared front-end for the Weasley Clock UI.
   Plain JavaScript, vendored, no build step and no CDN, because the clock sits
   on a Pi behind a Tailscale address and the UI has to work when the network is
   the thing being debugged. */

/* ---- the mapping, mirroring controller/clockd/geo.py ------------------- */

const EARTH_RADIUS_KM = 6371.0;
const MAX_DISTANCE_KM = Math.PI * EARTH_RADIUS_KM;
const LOG_SPAN = Math.log10(MAX_DISTANCE_KM);

function distanceToAngle(km) {
  const clamped = Math.min(Math.max(km, 1.0), MAX_DISTANCE_KM);
  return Math.log10(clamped) / LOG_SPAN * 360.0;
}
function angleToDistance(deg) {
  return Math.pow(10, deg / 360.0 * LOG_SPAN);
}
function wrap360(deg) {
  const w = ((deg % 360) + 360) % 360;
  return w >= 360 ? 0 : w;
}
function wrap180(delta) {
  const d = ((delta + 180) % 360 + 360) % 360 - 180;
  return d === -180 ? 180 : d;
}

/* ---- formatting -------------------------------------------------------- */

function formatKm(km) {
  if (km === null || km === undefined) return "—";
  if (km < 1) return km.toFixed(2) + " km";
  if (km < 10) return km.toFixed(1) + " km";
  return Math.round(km).toLocaleString("en-US") + " km";
}

function formatDeg(deg) {
  if (deg === null || deg === undefined) return "—";
  return wrap360(deg).toFixed(1) + "°";
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return Math.round(seconds) + "s ago";
  if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + "h ago";
  return (seconds / 86400).toFixed(1) + "d ago";
}

function formatUptime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 3600) return Math.round(seconds / 60) + " min";
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + " h";
  return (seconds / 86400).toFixed(1) + " days";
}

function formatTime(unix) {
  if (!unix) return "—";
  const d = new Date(unix * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

/* ---- toasts ------------------------------------------------------------ */

let toastTimer = null;

function toast(message, bad) {
  let node = document.getElementById("toast");
  if (!node) {
    node = document.createElement("div");
    node.id = "toast";
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.className = bad ? "bad" : "";
  node.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, bad ? 6000 : 3000);
}

/* ---- API --------------------------------------------------------------- */

async function api(path, body, method) {
  const options = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body !== undefined && body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    toast("Cannot reach the controller. Is it still running?", true);
    throw error;
  }
  if (response.status === 401) {
    window.location = "/login?next=" + encodeURIComponent(window.location.pathname);
    throw new Error("signed out");
  }
  let data = null;
  const text = await response.text();
  if (text) {
    try { data = JSON.parse(text); } catch (error) { data = { raw: text }; }
  }
  if (!response.ok) {
    const message = (data && (data.error || data.detail)) || ("HTTP " + response.status);
    toast(message, true);
    const error = new Error(message);
    error.data = data;
    throw error;
  }
  if (data && data.ok === false) {
    toast(data.error || "That did not work.", true);
  }
  return data;
}

/* ---- live state -------------------------------------------------------- */

const listeners = [];
let latest = null;
let socket = null;
let pollTimer = null;

function onState(callback) {
  listeners.push(callback);
  if (latest) callback(latest);
}

function pushState(state) {
  latest = state;
  for (const callback of listeners) {
    try { callback(state); } catch (error) { console.error(error); }
  }
}

function connectLive() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  try {
    socket = new WebSocket(scheme + "//" + window.location.host + "/ws");
  } catch (error) {
    startPolling();
    return;
  }
  socket.onmessage = (event) => {
    try { pushState(JSON.parse(event.data)); } catch (error) { /* ignore a bad frame */ }
  };
  socket.onclose = () => {
    socket = null;
    /* Fall back to polling, then try the socket again. A UI that goes blank
       because a WebSocket dropped is useless for debugging a flaky link. */
    startPolling();
    setTimeout(() => { if (!socket) { stopPolling(); connectLive(); } }, 8000);
  };
  socket.onerror = () => { if (socket) socket.close(); };
}

function startPolling() {
  if (pollTimer) return;
  const tick = async () => {
    try { pushState(await api("/api/state")); } catch (error) { /* already toasted */ }
  };
  tick();
  pollTimer = setInterval(tick, 2000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

/* ---- the system banner, on every page ---------------------------------- */

function renderBanner(state) {
  const banner = document.getElementById("banner");
  if (!banner) return;
  const pico = state.pico || {};
  const mqtt = state.mqtt || {};
  const unverified = (state.people || []).filter((p) => !p.verified && p.enabled);

  banner.className = "banner " + (pico.connected ? (unverified.length ? "warn" : "ok") : "bad");
  const items = [];
  items.push(pico.connected
    ? `<span class="item"><b>Pico</b> connected · fw ${pico.firmware || "?"}</span>`
    : `<span class="item"><b>Pico</b> NOT CONNECTED — nothing will move</span>`);
  items.push(`<span class="item"><b>Drivers</b> ${pico.enabled ? "enabled" : "released"}</span>`);
  items.push(`<span class="item"><b>MQTT</b> ${mqtt.connected ? "connected" : "down"} (${mqtt.host || "?"})</span>`);
  if (unverified.length) {
    items.push(`<span class="item"><b>${unverified.length} hand${unverified.length > 1 ? "s" : ""} unverified</b> — <a href="/assist">zero them</a></span>`);
  }
  items.push(`<span class="item">up ${formatUptime(state.uptime_s)}</span>`);
  if (pico.last_fault) {
    items.push(`<span class="item">fault: ${pico.last_fault.code}</span>`);
  }
  banner.innerHTML = items.join("");

  const dot = document.querySelector(".brand .dot");
  if (dot) dot.className = "dot " + (pico.connected ? "up" : "down");
}

/* ---- the dial ---------------------------------------------------------- */

const SVG_NS = "http://www.w3.org/2000/svg";
const CX = 230, CY = 230;
const RING_OUTER = 180, RING_INNER = 152;
const HOUR_LABEL_R = 166, DECADE_LABEL_R = 201, HUB_R = 13;
const TIP_R = [100, 109, 118, 127, 136, 145];

function polar(radius, deg) {
  const rad = deg * Math.PI / 180;
  return [CX + radius * Math.sin(rad), CY - radius * Math.cos(rad)];
}

function svgEl(name, attrs, text) {
  const node = document.createElementNS(SVG_NS, name);
  for (const key in attrs) node.setAttribute(key, String(attrs[key]));
  if (text !== undefined) node.textContent = text;
  return node;
}

function handPath(radius, deg, width) {
  const [tx, ty] = polar(radius, deg);
  const [lx, ly] = polar(HUB_R, deg - width);
  const [rx, ry] = polar(HUB_R, deg + width);
  return `M ${lx.toFixed(2)} ${ly.toFixed(2)} L ${tx.toFixed(2)} ${ty.toFixed(2)} L ${rx.toFixed(2)} ${ry.toFixed(2)} Z`;
}

function buildDial(container) {
  container.replaceChildren();
  const svg = svgEl("svg", {
    class: "dial",
    viewBox: "0 0 460 460",
    role: "img",
    "aria-label": "Clock dial showing each person's distance from home",
  });
  const statics = svgEl("g", { id: "dial-static" });
  const ghosts = svgEl("g", { id: "dial-ghosts" });
  const hands = svgEl("g", { id: "dial-hands" });

  statics.appendChild(svgEl("circle", {
    cx: CX, cy: CY, r: RING_OUTER, fill: "var(--dial)", stroke: "var(--brass)", "stroke-width": 2,
  }));
  statics.appendChild(svgEl("circle", {
    cx: CX, cy: CY, r: RING_INNER, fill: "none", stroke: "var(--brass)", "stroke-width": 1.2, opacity: .8,
  }));
  for (let deg = 0; deg < 360; deg += 6) {
    const major = deg % 30 === 0;
    const [x1, y1] = polar(RING_INNER, deg);
    const [x2, y2] = polar(major ? RING_OUTER - 3 : RING_OUTER - 12, deg);
    statics.appendChild(svgEl("line", {
      x1, y1, x2, y2,
      stroke: major ? "var(--ink)" : "var(--edge)",
      "stroke-width": major ? 2 : 1,
      opacity: major ? .85 : .6,
      "stroke-linecap": "round",
    }));
  }
  ["12", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"].forEach((label, index) => {
    const [x, y] = polar(HOUR_LABEL_R, index * 30);
    statics.appendChild(svgEl("text", {
      x, y, "text-anchor": "middle", "dominant-baseline": "central",
      "font-size": label === "12" ? 17 : 14,
      "font-weight": label === "12" ? 700 : 500,
      fill: "var(--ink)", opacity: index <= 6 ? 1 : .35,
    }, label));
  });
  /* The 1 km tick sits exactly on the 12, whose numeral already says "home",
     so it gets a tick but no label -- two numbers stacked there read as one. */
  [[1, ""], [10, "10"], [100, "100"], [1000, "1k"], [10000, "10k"]].forEach(([km, label]) => {
    const deg = distanceToAngle(km);
    const [x1, y1] = polar(RING_OUTER + 2, deg);
    const [x2, y2] = polar(RING_OUTER + 11, deg);
    statics.appendChild(svgEl("line", {
      x1, y1, x2, y2, stroke: "var(--accent)", "stroke-width": 2, "stroke-linecap": "round",
    }));
    if (!label) return;
    const [tx, ty] = polar(DECADE_LABEL_R, deg);
    statics.appendChild(svgEl("text", {
      x: tx, y: ty, "text-anchor": "middle", "dominant-baseline": "central",
      "font-size": 11, fill: "var(--accent-ink)",
    }, label));
  });
  statics.appendChild(svgEl("circle", {
    cx: CX, cy: CY, r: HUB_R + 5, fill: "var(--dial)", stroke: "var(--brass)", "stroke-width": 1.5,
  }));

  svg.append(statics, ghosts, hands);
  container.appendChild(svg);
  return svg;
}

/* Draw the hands from a state snapshot. Solid = where the firmware says the
   hand is, faint = where it is heading. A hand with no target yet is not drawn
   at all, which is honest: we do not know where it points. */
function drawHands(people, options) {
  const ghosts = document.getElementById("dial-ghosts");
  const hands = document.getElementById("dial-hands");
  if (!ghosts || !hands) return;
  ghosts.replaceChildren();
  hands.replaceChildren();
  const only = (options && options.only) || null;

  for (const person of people) {
    if (only && person.slug !== only) continue;
    const radius = TIP_R[Math.min(Math.max(person.level - 1, 0), 5)];
    const actual = person.actual_deg;
    const target = person.target_deg;
    const known = actual !== null && actual !== undefined;

    if (target !== null && target !== undefined &&
        (!known || Math.abs(wrap180(target - actual)) > 0.3)) {
      ghosts.appendChild(svgEl("path", {
        d: handPath(radius, target, 26), fill: person.color, opacity: .22,
      }));
      const [gx, gy] = polar(radius + 9, target);
      ghosts.appendChild(svgEl("circle", { cx: gx, cy: gy, r: 2.5, fill: person.color, opacity: .5 }));
    }
    if (!known) continue;

    const dim = !person.enabled || !person.verified;
    hands.appendChild(svgEl("path", {
      d: handPath(radius, actual, 20),
      fill: person.color,
      opacity: dim ? .45 : 1,
      stroke: "var(--dial)",
      "stroke-width": .8,
      "stroke-dasharray": person.verified ? "" : "4 3",
    }));
    const [tx, ty] = polar(radius, actual);
    hands.appendChild(svgEl("circle", {
      cx: tx, cy: ty, r: 3.4, fill: person.color,
      opacity: dim ? .45 : 1, stroke: "var(--dial)", "stroke-width": 1,
    }));
  }
  hands.appendChild(svgEl("circle", {
    cx: CX, cy: CY, r: HUB_R, fill: "var(--brass)", stroke: "var(--ink)", "stroke-width": 1,
  }));
}

function renderLegend(container, people) {
  if (!container) return;
  container.replaceChildren();
  for (const person of people) {
    const item = document.createElement("span");
    const sw = document.createElement("span");
    sw.className = "sw";
    sw.style.background = person.color;
    item.append(sw, document.createTextNode(person.name));
    if (!person.verified) item.append(document.createTextNode(" (unverified)"));
    container.appendChild(item);
  }
}

function statusBadge(person) {
  const badge = document.createElement("span");
  badge.className = "badge " + person.status;
  badge.textContent = person.status;
  return badge;
}

/* ---- confirmation, since the sandboxed confirm() is not reliable -------- */

function confirmAction(message, onYes) {
  const existing = document.getElementById("confirm-overlay");
  if (existing) existing.remove();
  const overlay = document.createElement("div");
  overlay.id = "confirm-overlay";
  overlay.style.cssText =
    "position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.45);display:flex;" +
    "align-items:center;justify-content:center;padding:20px";
  const box = document.createElement("div");
  box.className = "card";
  box.style.cssText = "max-width:420px;width:100%";
  const text = document.createElement("p");
  text.textContent = message;
  text.style.margin = "0 0 14px";
  const row = document.createElement("div");
  row.className = "row";
  const yes = document.createElement("button");
  yes.className = "primary";
  yes.textContent = "Yes, do it";
  const no = document.createElement("button");
  no.textContent = "Cancel";
  row.append(yes, no);
  box.append(text, row);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  yes.focus();
  yes.onclick = () => { overlay.remove(); onYes(); };
  no.onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
}

/* ---- boot -------------------------------------------------------------- */

document.addEventListener("DOMContentLoaded", () => {
  onState(renderBanner);
  connectLive();
  startPolling();
  setTimeout(() => { if (socket && socket.readyState === 1) stopPolling(); }, 3000);
});

window.clockui = {
  api, toast, onState, pushState, confirmAction,
  buildDial, drawHands, renderLegend, statusBadge,
  formatKm, formatDeg, formatAge, formatUptime, formatTime,
  distanceToAngle, angleToDistance, wrap360, wrap180,
  get latest() { return latest; },
};
