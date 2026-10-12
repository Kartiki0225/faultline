// FaultLine dashboard: receives snapshots over a WebSocket and shows them.

const $ = (selector) => document.querySelector(selector);

// ------------------------------------------------------------ gauges

const gauges = {
  rpm: createGauge($("#gauge-rpm"), {
    min: 0, max: 2500, unit: "RPM", needle: true,
    tickStep: 100, labelStep: 500, labelDivide: 100,
    zones: [{ from: 2100, to: 2500, level: "red" }],
  }),
  coolant_temp: createGauge($("#gauge-coolant"), {
    min: 40, max: 120, unit: "°C", label: "COOLANT",
    tickStep: 10, labelStep: 20,
    zones: [{ from: 104, to: 110, level: "amber" }, { from: 110, to: 120, level: "red" }],
  }),
  oil_pressure: createGauge($("#gauge-oil"), {
    min: 0, max: 500, unit: "kPa", label: "OIL PRESSURE",
    tickStep: 50, labelStep: 100,
    zones: [{ from: 0, to: 73, level: "red" }, { from: 73, to: 113, level: "amber" }],
  }),
};

// Smaller values shown as a number with a bar.
// bad: the value is a problem when it is "above" or "below" the limit.
const READOUTS = [
  { key: "load", label: "Load", unit: "%", min: 0, max: 100 },
  { key: "boost", label: "Boost", unit: "kPa", min: 0, max: 250 },
  { key: "exhaust_temp", label: "Exhaust", unit: "°C", min: 0, max: 700 },
  { key: "fuel_rate", label: "Fuel rate", unit: "L/h", min: 0, max: 70, decimals: 1 },
  { key: "air_filter_dp", label: "Air filter", unit: "kPa", min: 0, max: 12, decimals: 1, limit: 5, bad: "above" },
  { key: "coolant_level", label: "Coolant level", unit: "%", min: 0, max: 100, limit: 75, bad: "below" },
  { key: "battery", label: "Battery", unit: "V", min: 20, max: 30, decimals: 1, limit: 24, bad: "below" },
];

const readouts = {};
for (const config of READOUTS) {
  const node = document.createElement("div");
  node.className = "readout";
  node.innerHTML = `
    <span class="label">${config.label}</span>
    <strong><b>--</b><small>${config.unit}</small></strong>
    <div class="meter"><i></i>${config.limit === undefined ? "" : `<u style="left:${percentOf(config, config.limit)}%"></u>`}</div>`;
  $("#readouts").appendChild(node);
  readouts[config.key] = { config, node, number: node.querySelector("b"), fill: node.querySelector("i") };
}

function percentOf(config, value) {
  return Math.min(100, Math.max(0, ((value - config.min) / (config.max - config.min)) * 100));
}

function showReadouts(signals) {
  for (const { config, node, number, fill } of Object.values(readouts)) {
    const value = signals[config.key];
    if (typeof value !== "number") {
      number.textContent = value === "error" ? "ERR" : "--";
      continue;
    }
    number.textContent = value.toFixed(config.decimals || 0);
    fill.style.width = percentOf(config, value) + "%";
    const bad = config.bad === "above" ? value > config.limit : config.bad === "below" ? value < config.limit : false;
    node.classList.toggle("bad", bad);
  }
}

// ------------------------------------------------------------ trends

const TREND_POINTS = 600; // 60 seconds at 10 snapshots per second
const trends = {
  rpm: { canvas: $("#trend-rpm"), samples: [], min: 0, max: 2500 },
  coolant_temp: {
    canvas: $("#trend-coolant"), samples: [], min: 60, max: 120,
    limits: [{ value: 104, level: "amber" }, { value: 110, level: "red" }],
  },
  oil_pressure: { canvas: $("#trend-oil"), samples: [], min: 0, max: 450 },
};

function recordTrends(signals) {
  for (const [key, trend] of Object.entries(trends)) {
    const value = signals[key];
    trend.samples.push(typeof value === "number" ? value : null);
    if (trend.samples.length > TREND_POINTS) trend.samples.shift();
  }
}

// ------------------------------------------------------------ faults

const escapeHtml = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString("en-GB");

let faultKey = null;
let shownWarnings = null;
let shownHistory = null;
function showFaults(snapshot) {
  const { faults, warnings, events, lamps } = snapshot;
  $("#lamp-red").classList.toggle("on", lamps.red_stop || bulbCheck);
  $("#lamp-amber").classList.toggle("on", lamps.amber_warning || bulbCheck);
  $("#fault-count").textContent = `${faults.length} active`;
  $("#fault-count").classList.toggle("some", faults.length > 0);

  // Rebuild a list only when its content changes, so it does not flicker.
  const warningKey = JSON.stringify(warnings);
  if (warningKey !== shownWarnings) {
    shownWarnings = warningKey;
    $("#warnings").innerHTML = warnings.map((warning) => `
    <article class="fault prediction">
      <header><span class="code">PREDICTION</span><span>before the ECU reports it</span></header>
      <h4>Coolant temperature is climbing</h4>
      <p>Rising ${warning.rate} °C per second. It will reach the ${warning.limit} °C limit in about <b>${warning.seconds_left} s</b>.</p>
    </article>`).join("");
  }

  const key = faults.map((f) => `${f.spn}/${f.fmi}/${f.count}`).join(",");
  if (key !== faultKey) {
    faultKey = key;
    $("#fault-list").innerHTML = faults.length === 0
      ? `<p class="all-clear">No active faults.<span>The ECU is reporting DM1 with all lamps off.</span></p>`
      : faults.map((fault) => `
        <article class="fault ${fault.severe ? "severe" : "warning"}">
          <header>
            <span class="code">SPN ${fault.spn} · FMI ${fault.fmi}</span>
            <span>seen ${fault.count}×</span>
          </header>
          <h4>${escapeHtml(fault.title)}</h4>
          <p class="mode">${escapeHtml(fault.fmi_text)}</p>
          <p>${escapeHtml(fault.meaning)}</p>
          <p class="action"><b>Action</b> ${escapeHtml(fault.action)}</p>
        </article>`).join("");
  }

  const historyKey = events.length + ":" + (events.length ? events[events.length - 1].time : 0);
  if (historyKey === shownHistory) return;
  shownHistory = historyKey;
  $("#history").innerHTML = events.length === 0
    ? `<li class="empty">Nothing yet. Cause a failure on the test bench.</li>`
    : events.slice().reverse().map((event) => `
      <li class="${event.kind}">
        <time>${clock(event.time)}</time>
        <span class="kind">${event.kind === "set" ? "SET" : "CLEARED"}</span>
        <span>${escapeHtml(event.title)}</span>
        <code>${event.spn}/${event.fmi}</code>
      </li>`).join("");
}

// ------------------------------------------------------------ CAN bus view

let busView = "ids";
let selectedId = null;
let latestData = {};   // can id -> data bytes as text
const traceLines = [];

function showBus(snapshot) {
  const body = $("#id-table tbody");
  for (const row of snapshot.ids) {
    let tr = body.querySelector(`[data-id="${row.id}"]`);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.id = row.id;
      tr.innerHTML = `<td class="mono">${row.id}</td><td class="mono">${row.pgn}</td><td>${row.name}</td>
        <td class="mono bytes"></td><td class="num mono period"></td><td class="num mono count"></td>`;
      tr.addEventListener("click", () => selectFrame(row.id));
      body.appendChild(tr);
    }
    // Highlight the bytes that changed since the last snapshot.
    const before = (latestData[row.id] || "").split(" ");
    tr.querySelector(".bytes").innerHTML = row.data.split(" ")
      .map((byte, i) => `<span class="${before[i] !== undefined && before[i] !== byte ? "changed" : ""}">${byte}</span>`).join("");
    tr.querySelector(".period").textContent = row.period_ms ? row.period_ms + " ms" : "";
    tr.querySelector(".count").textContent = row.count;
    tr.classList.toggle("selected", row.id === selectedId);
    latestData[row.id] = row.data;
  }
  if (!selectedId && latestData["0CF00400"]) selectFrame("0CF00400"); // start with engine speed open

  for (const [, time, id, data] of snapshot.frames) {
    traceLines.push(`${clock(time)}.${String(Math.round((time % 1) * 1000)).padStart(3, "0")}  ${id}  [8]  ${data}`);
  }
  traceLines.splice(0, Math.max(0, traceLines.length - 22));
  if (busView === "trace") $("#trace").textContent = traceLines.join("\n");
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    busView = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((other) => other.classList.toggle("active", other === tab));
    $("#id-table").hidden = busView !== "ids";
    $("#trace").hidden = busView !== "trace";
  });
}

function selectFrame(id) {
  selectedId = id;
  refreshInspector();
}

// Ask the server to explain the selected frame using its newest data.
async function refreshInspector() {
  if (!selectedId || !latestData[selectedId]) return;
  const data = latestData[selectedId].replaceAll(" ", "");
  const response = await fetch(`/api/explain?id=${selectedId}&data=${data}`);
  if (response.ok) showInspector(await response.json());
}
setInterval(refreshInspector, 400);

function showInspector(frame) {
  const bits = frame.bits;
  // The 29 bits of the identifier, split into the fields J1939 defines.
  const fields = [
    ["Priority", bits.slice(0, 3), frame.priority],
    ["R·DP", bits.slice(3, 5), ""],
    ["PDU format", bits.slice(5, 13), "0x" + parseInt(bits.slice(5, 13), 2).toString(16).toUpperCase().padStart(2, "0")],
    ["PDU specific", bits.slice(13, 21), "0x" + parseInt(bits.slice(13, 21), 2).toString(16).toUpperCase().padStart(2, "0")],
    ["Source", bits.slice(21), "0x" + frame.source.toString(16).toUpperCase().padStart(2, "0")],
  ];

  const signals = frame.signals.map((signal) => {
    const formula = typeof signal.value === "number"
      ? `${signal.raw} × ${signal.scale} ${signal.offset < 0 ? "−" : "+"} ${Math.abs(signal.offset)} = <b>${signal.value} ${signal.unit}</b> ${signal.meaning}`
      : `<b>${signal.value === "error" ? "sensor error" : "not available"}</b> (reserved value)`;
    return `
      <li>
        <div><span class="code">SPN ${signal.spn}</span> ${escapeHtml(signal.name)}</div>
        <div class="mono step">byte ${signal.bytes} → 0x${signal.raw_hex} → ${formula}</div>
      </li>`;
  }).join("");

  $("#inspector").innerHTML = `
    <div class="inspector-head">
      <span class="mono big">${frame.id}</span>
      <span><b>${frame.name}</b> ${escapeHtml(frame.title)}</span>
    </div>
    <div class="id-bits">
      ${fields.map(([name, value, meaning]) => `<div><code>${value}</code><span>${name}</span><b>${meaning}</b></div>`).join("")}
    </div>
    <p class="pgn">PGN <b>${frame.pgn}</b> (0x${frame.pgn_hex}), ${frame.addressing}</p>
    <div class="byte-strip">
      ${frame.data.split(" ").map((byte, i) => `<div><small>${i + 1}</small>${byte}</div>`).join("")}
    </div>
    <ul class="decode">${signals}${frame.notes.map((note) => `<li class="mono step">${escapeHtml(note)}</li>`).join("")}</ul>`;
}

// ------------------------------------------------------------ test bench controls

let socket = null;
const send = (command) => socket && socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(command));

$("#start-stop").addEventListener("click", (event) => send({ cmd: event.target.dataset.next || "start" }));
$("#throttle").addEventListener("input", (event) => {
  $("#throttle-value").textContent = event.target.value + "%";
  send({ cmd: "throttle", value: event.target.value / 100 });
});
$("#repair").addEventListener("click", () => send({ cmd: "repair" }));
for (const button of document.querySelectorAll(".failure")) {
  button.addEventListener("click", () => send({ cmd: "fault", name: button.dataset.fault, on: !button.classList.contains("on") }));
}

function showBench(simulator) {
  const label = simulator.protection_shutdown ? "PROTECTION SHUTDOWN"
    : simulator.state === "running" && simulator.derate ? "DERATED"
    : simulator.state.toUpperCase();
  $("#engine-state").textContent = label;
  $("#engine-state").dataset.state = simulator.protection_shutdown ? "shutdown" : simulator.derate ? "derate" : simulator.state;

  const running = simulator.state !== "off";
  $("#start-stop").textContent = running ? "Stop" : "Start";
  $("#start-stop").dataset.next = running ? "stop" : "start";
  for (const button of document.querySelectorAll(".failure")) {
    button.classList.toggle("on", simulator.faults.includes(button.dataset.fault));
  }
  // Follow the server's throttle unless the user is dragging the slider.
  if (document.activeElement !== $("#throttle")) {
    $("#throttle").value = Math.round(simulator.throttle * 100);
    $("#throttle-value").textContent = $("#throttle").value + "%";
  }
}

// ------------------------------------------------------------ live connection

let bulbCheck = true; // like a real dashboard, light every lamp for a moment at switch-on
setTimeout(() => (bulbCheck = false), 1600);

function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);

  socket.onopen = () => {
    $("#link-dot").classList.add("live");
    $("#link-text").textContent = "Live";
  };

  socket.onmessage = (message) => {
    const snapshot = JSON.parse(message.data);
    for (const [key, gauge] of Object.entries(gauges)) gauge.set(snapshot.signals[key]);
    showReadouts(snapshot.signals);
    recordTrends(snapshot.signals);
    showFaults(snapshot);
    showBus(snapshot);
    showBench(snapshot.simulator);
    $("#frame-rate").textContent = snapshot.frames_per_second;
  };

  socket.onclose = () => {
    $("#link-dot").classList.remove("live");
    $("#link-text").textContent = "Reconnecting";
    setTimeout(connect, 1000);
  };
}

function frame() {
  for (const gauge of Object.values(gauges)) gauge.animate();
  for (const trend of Object.values(trends)) drawTrend(trend.canvas, trend.samples, { ...trend, capacity: TREND_POINTS });
  requestAnimationFrame(frame);
}

connect();
requestAnimationFrame(frame);
