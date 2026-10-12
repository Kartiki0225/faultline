// Drawing code for the dashboard: round gauges (SVG) and trend lines (canvas).
// Nothing in this file knows about CAN or J1939.

const SVG = "http://www.w3.org/2000/svg";
const START_ANGLE = 135; // degrees, measured clockwise from "3 o'clock"
const SWEEP = 270;

function polar(radius, degrees) {
  const radians = (degrees * Math.PI) / 180;
  return [100 + radius * Math.cos(radians), 100 + radius * Math.sin(radians)];
}

function arc(radius, from, to) {
  const [x1, y1] = polar(radius, from);
  const [x2, y2] = polar(radius, to);
  const large = to - from > 180 ? 1 : 0;
  return `M ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2}`;
}

function svg(tag, attributes, parent) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  parent.appendChild(node);
  return node;
}

/**
 * Build a round gauge inside `host`.
 * options: min, max, unit, label, decimals, zones [{from, to, level}], tickStep, labelStep, labelDivide, needle
 * Returns an object with set(value), where value is a number, "error" or null.
 */
function createGauge(host, options) {
  const { min, max } = options;
  const angleOf = (value) => START_ANGLE + (SWEEP * (Math.min(max, Math.max(min, value)) - min)) / (max - min);
  const root = svg("svg", { viewBox: "0 0 200 200" }, host);

  svg("path", { d: arc(82, START_ANGLE, START_ANGLE + SWEEP), class: "track" }, root);
  for (const zone of options.zones || []) {
    svg("path", { d: arc(92, angleOf(zone.from), angleOf(zone.to)), class: `zone ${zone.level}` }, root);
  }

  for (let value = min; value <= max + 1e-9; value += options.tickStep) {
    const major = Math.round((value - min) / options.tickStep) % Math.round(options.labelStep / options.tickStep) === 0;
    const [x1, y1] = polar(major ? 70 : 74, angleOf(value));
    const [x2, y2] = polar(78, angleOf(value));
    svg("line", { x1, y1, x2, y2, class: major ? "tick major" : "tick" }, root);
    if (major) {
      const [x, y] = polar(59, angleOf(value));
      svg("text", { x, y, class: "tick-label" }, root).textContent = value / (options.labelDivide || 1);
    }
  }

  const fullArc = arc(82, START_ANGLE, START_ANGLE + SWEEP);
  const valueArc = svg("path", { d: fullArc, class: "value" }, root);
  const arcLength = valueArc.getTotalLength();

  let needle = null;
  if (options.needle) {
    needle = svg("g", { class: "needle" }, root);
    svg("line", { x1: 100, y1: 100, x2: 172, y2: 100 }, needle);
    svg("circle", { cx: 100, cy: 100, r: 7 }, root).setAttribute("class", "hub");
  }

  // With a needle, the number sits in the gap at the bottom of the dial.
  const number = svg("text", { x: 100, y: options.needle ? 172 : 104, class: "number" }, root);
  const unit = svg("text", { x: 100, y: options.needle ? 188 : 124, class: "unit" }, root);
  unit.textContent = options.unit;
  if (options.label) svg("text", { x: 100, y: 190, class: "label" }, root).textContent = options.label;

  let shown = min;
  let target = min;
  let status = "ok";

  function levelOf(value) {
    const zone = (options.zones || []).find((z) => value >= z.from && value <= z.to);
    return zone ? zone.level : "normal";
  }

  function draw() {
    const fraction = (angleOf(shown) - START_ANGLE) / SWEEP;
    valueArc.style.strokeDasharray = `${arcLength * fraction} ${arcLength}`;
    if (needle) needle.setAttribute("transform", `rotate(${angleOf(shown)} 100 100)`);
    host.dataset.level = status === "ok" ? levelOf(shown) : "fault";
    number.textContent = status === "ok" ? shown.toFixed(options.decimals || 0) : status === "error" ? "ERR" : "---";
  }

  return {
    set(value) {
      status = typeof value === "number" ? "ok" : value === "error" ? "error" : "missing";
      if (status === "ok") target = value;
    },
    // Called on every animation frame: glide towards the newest value.
    animate() {
      shown += (target - shown) * 0.16;
      draw();
    },
  };
}

/**
 * Draw a scrolling trend line.
 * samples: array of numbers (or null for gaps), oldest first.
 * limits: [{value, level}] drawn as dashed lines.
 */
function drawTrend(canvas, samples, { min, max, limits = [], capacity }) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== width * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const style = getComputedStyle(document.documentElement);
  const color = (name) => style.getPropertyValue(name).trim();
  const yOf = (value) => height - 4 - ((Math.min(max, Math.max(min, value)) - min) / (max - min)) * (height - 8);
  const xOf = (index) => (index / (capacity - 1)) * width;

  ctx.lineWidth = 1;
  for (const limit of limits) {
    ctx.strokeStyle = color(limit.level === "red" ? "--red" : "--amber");
    ctx.globalAlpha = 0.55;
    ctx.setLineDash([4, 5]);
    ctx.beginPath();
    ctx.moveTo(0, yOf(limit.value));
    ctx.lineTo(width, yOf(limit.value));
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  // The newest sample sits at the right edge.
  const offset = capacity - samples.length;
  ctx.strokeStyle = color("--ink");
  ctx.lineWidth = 1.6;
  ctx.lineJoin = "round";
  ctx.beginPath();
  let drawing = false;
  samples.forEach((value, index) => {
    if (value === null) {
      drawing = false;
      return;
    }
    const x = xOf(index + offset);
    const y = yOf(value);
    if (drawing) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
    drawing = true;
  });
  ctx.stroke();

  const last = samples[samples.length - 1];
  if (typeof last === "number") {
    ctx.fillStyle = color("--ink");
    ctx.beginPath();
    ctx.arc(width - 3, yOf(last), 3, 0, Math.PI * 2);
    ctx.fill();
  }
}
