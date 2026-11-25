/* Agent Gateway admin console. Plain JS, no build step, no external requests.
   Security: every value from the API is inserted as text (never innerHTML); the admin token lives
   in sessionStorage for this tab only.
   The console only displays what the API returns: totals, percentages, remaining amounts, attention items and
   groupings all come from the gateway. Every action that signs out, deletes, discards, or changes what the
   gateway enforces asks for confirmation first. */
"use strict";

// ------------------------------------------------------------------ DOM helpers
const SVGNS = "http://www.w3.org/2000/svg";
let uid = 0;

function h(tag, attrs, ...kids) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");
  if (attrs == null || typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs)) {
    kids.unshift(attrs);
    attrs = {};
  }
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false || k === "value" || k === "checked") continue;
    if (k === "class") el.className = [el.className, v].filter(Boolean).join(" ");
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the console's CSP (inline style attributes are not)
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  if ("value" in attrs && attrs.value != null) el.value = attrs.value;
  if ("checked" in attrs) el.checked = !!attrs.checked;
  return el;
}

function append(el, ...kids) {
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null) el.append(c instanceof Node ? c : String(c));
  return el;
}

const ICONS = {
  overview: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  agents: "M12 3v3M7 7h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-7a3 3 0 0 1 3-3zM9.5 13.5h.01M14.5 13.5h.01",
  mandates: "M4 8h16v11H4zM9 8V5h6v3M4 13h16",
  providers: "M14.5 9.5a3.5 3.5 0 1 1-1 2.5L4 21.5V18h3v-3h3l1-1M17 7h.01",
  tools: "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z",
  usage: "M4 19h16M7 16V10M12 16V5M17 16v-4",
  policy: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z",
  activity: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  settings: "M4 7h10M18 7h2M4 17h4M12 17h8M14 5v4M8 15v4",
  plus: "M12 5v14M5 12h14", up: "M12 19V5M6 11l6-6 6 6", down: "M12 5v14M6 13l6 6 6-6", x: "M6 6l12 12M18 6L6 18",
  copy: "M9 9h10v10H9zM5 15V5h10", check: "M5 12l5 5 9-10",
  key: "M14.5 9.5a3.5 3.5 0 1 1-1 2.5L4 21.5V18h3v-3h3l1-1M17 7h.01",
  info: "M12 8h.01M11 12h1v5h1M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  alert: "M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  arrow: "M5 12h14M13 6l6 6-6 6", chev: "M9 6l6 6-6 6",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10", gate: "M4 21V9l8-5 8 5v12M9 21v-6h6v6",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z", grip: "M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01",
  sun: "M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
  collapse: "M4 4h16v16H4zM9 4v16M15 10l-2 2 2 2", expand: "M4 4h16v16H4zM9 4v16M13 10l2 2-2 2",
};

function icon(name) {
  return svg("svg", { class: "i", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": name === "grip" ? "2.6" : "1.7",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, svg("path", { d: ICONS[name] }));
}

// ------------------------------------------------------------------ formatting (display only)
const CURRENCY = { USD: { sym: "$", locale: "en-US" }, INR: { sym: "₹", locale: "en-IN" } };

function money(value, currency = "USD") {
  const n = Number(value || 0);
  const abs = Math.abs(n);
  const digits = abs === 0 || abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6;
  const c = CURRENCY[currency] || CURRENCY.USD;
  return c.sym + n.toLocaleString(c.locale, { minimumFractionDigits: 2, maximumFractionDigits: digits });
}
const int = (n) => Number(n || 0).toLocaleString("en-US");
const dateTime = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const timeOnly = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dayLong = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" });
const when = (iso) => (iso ? dateTime.format(new Date(iso)) : "—");
function ago(iso) {
  if (!iso) return "Never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function monthName(ym) {
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleString(undefined, { month: "long", year: "numeric" });
}
const dayMonth = (isoDate) => new Date(`${isoDate}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "long" });
const slugify = (s) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
const plural = (n, word) => `${int(n)} ${word}${Number(n) === 1 ? "" : "s"}`;
const titleCase = (s) => s.charAt(0).toUpperCase() + s.slice(1);

const REASONS = {
  AGENT_REVOKED: "Agent revoked", AGENT_INACTIVE: "Agent suspended", MANDATE_NOT_ASSIGNED: "Not assigned to mandate",
  MANDATE_INACTIVE: "Mandate suspended", MODEL_NOT_ALLOWED: "Model not allowed", AGENT_BUDGET_EXCEEDED: "Agent budget used up",
  MANDATE_BUDGET_EXCEEDED: "Mandate budget used up", AGENT_RATE_LIMITED: "Rate limit reached",
  UPSTREAM_RATE_LIMITED: "Provider busy", UPSTREAM_UNAVAILABLE: "Model service unavailable", UPSTREAM_TIMEOUT: "Model timed out",
  UPSTREAM_ERROR: "Provider error", FIELD_NOT_ALLOWED: "Field not allowed", INVALID_REQUEST: "Invalid request",
  INVALID_JSON: "Invalid JSON", CONTENT_NOT_ALLOWED: "Content not allowed", TOOL_NOT_ALLOWED: "Tool not allowed",
  STREAMING_NOT_SUPPORTED: "Streaming not supported", REQUEST_TOO_LARGE: "Request too large", ABANDONED: "Abandoned, charged",
  TOOL_SERVER_NOT_ALLOWED: "Tool server not allowed", MCP_TOOL_NOT_ALLOWED: "Tool not allowed", TOOL_DENIED_BY_SERVER: "Refused by the tool server",
  TOOL_SERVER_UNAVAILABLE: "Tool server unavailable", TOOL_SERVER_TIMEOUT: "Tool server timed out", TOOL_SERVER_ERROR: "Tool server error",
  TOOL_SERVER_AUTH_FAILED: "Tool server refused the gateway's pass", STREAM_CLOSED: "Stopped by an admin change", POLICY_ERROR: "Policy error",
};
const reasonText = (code) => REASONS[code] || code;

// ------------------------------------------------------------------ per-viewer preferences (theme, sidebar)
const pref = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked: the choice lasts this page only */ } },
};
if (pref.get("gw_theme")) document.documentElement.dataset.theme = pref.get("gw_theme");
const themeNow = () => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
const stillMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// ------------------------------------------------------------------ API
const TOKEN_KEY = "gw_admin_token";
const store = { token: readToken() };

function readToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
function saveToken(t) {
  store.token = t;
  try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(method, path, body) {
  const headers = { Authorization: `Bearer ${store.token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  } catch {
    throw new ApiError(0, "NETWORK", "Can't reach the gateway. Is it running?");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && path.startsWith("/admin/")) {
    signOut("Your admin token was not accepted. Sign in again.");
    throw new ApiError(401, "ADMIN_UNAUTHORIZED", "Signed out");
  }
  if (!res.ok) throw new ApiError(res.status, data?.error?.code, data?.error?.message || `Request failed (${res.status})`);
  return data;
}

// ------------------------------------------------------------------ liquid-metal ring on primary buttons
// fx.js is the vendored metal-fx effect (scripts/vendor-ui.sh). It loads once, on first use; if it can't load or the
// browser has no WebGL2, the plain button stays.
let fxLoad = null;
function gloss(scope) {
  if (!scope) return;
  for (const b of scope.querySelectorAll(".btn.primary")) {
    if (b.parentElement?.closest(".fx")) continue;
    const host = h("span", { class: `fx${b.classList.contains("block") ? " block" : ""}` });
    b.replaceWith(host);
    host.append(b);
    (fxLoad ||= import("/admin/ui/fx.js").catch(() => null)).then((fx) => {
      if (fx) requestAnimationFrame(() => { try { fx.metal(host, b, themeNow(), stillMotion()); } catch { /* plain button stays */ } });
    });
  }
}

// ------------------------------------------------------------------ feedback: toasts, modals, confirm
function toast(message, kind = "") {
  let host = document.querySelector(".toasts");
  if (!host) document.body.append(host = h("div.toasts", { role: "status", "aria-live": "polite" }));
  const t = h("div", { class: `toast ${kind}` }, icon(kind === "error" ? "alert" : "check"), h("span", message));
  host.append(t);
  setTimeout(() => {
    t.classList.add("out");
    t.addEventListener("animationend", () => t.remove());
    setTimeout(() => t.remove(), 400); // reduced motion: no animationend
  }, kind === "error" ? 6000 : 3200);
}

function modal({ title, sub, wide }) {
  const previous = document.activeElement;
  const body = h("div.modal-body");
  const foot = h("div.modal-foot");
  const box = h("div", { class: `modal${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title, tabindex: "-1" },
    h("div.modal-head", h("h2", title), sub && h("p", sub)), body, foot);
  const overlay = h("div.overlay", box);
  const onKey = (e) => { if (e.key === "Escape") close(); };
  function close() {
    document.removeEventListener("keydown", onKey);
    overlay.classList.add("out");
    const gone = () => overlay.remove();
    overlay.addEventListener("animationend", gone);
    setTimeout(gone, 300);
    previous?.focus?.();
  }
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
  document.body.append(overlay);
  // focus the first field, or Cancel (the safe choice) when there is none
  requestAnimationFrame(() => {
    gloss(foot);
    (box.querySelector("input:not([type=checkbox]), select, textarea") || foot.querySelector(".btn") || box).focus();
  });
  return { body, foot, close };
}

function confirmDialog({ title, message, confirm = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const m = modal({ title });
    let answered = false;
    const done = (v) => { if (!answered) { answered = true; m.close(); resolve(v); } };
    append(m.body, h("p", message));
    append(m.foot,
      h("button.btn", { type: "button", onclick: () => done(false) }, "Cancel"),
      h("button", { type: "button", class: `btn ${danger ? "danger solid" : "primary"}`, onclick: () => done(true) }, confirm));
    const obs = new MutationObserver(() => { if (!document.body.contains(m.body)) { obs.disconnect(); done(false); } });
    obs.observe(document.body, { childList: true });
  });
}
/* Every action that signs out, deletes, discards, or changes what the gateway enforces asks first. */
const ask = (title, message, confirm, danger = false) => confirmDialog({ title, message, confirm, danger });

function formError() {
  const el = h("div.err", { role: "alert", hidden: true });
  el.show = (msg) => { el.textContent = msg; el.hidden = false; el.style.animation = "none"; void el.offsetWidth; el.style.animation = ""; };
  el.clear = () => { el.hidden = true; };
  return el;
}

async function busy(button, fn) {
  button.disabled = true;
  try { return await fn(); } finally { button.disabled = false; }
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    if (button) { button.classList.add("copied"); setTimeout(() => button.classList.remove("copied"), 1600); } else toast("Copied to clipboard");
  } catch {
    toast("Couldn't copy automatically. Select the text and copy it.", "error");
  }
}
function copyButton(text, label = "Copy") {
  const b = h("button.btn.sm", { type: "button", "aria-label": `${label}: ${text}` },
    h("span.default", icon("copy"), label), h("span.done", icon("check"), "Copied"));
  b.addEventListener("click", () => copyText(text, b));
  return b;
}
const copyRow = (text) => h("div.copy", h("span.code", { title: text }, text), copyButton(text));

// ------------------------------------------------------------------ form controls
function field(label, control, help) {
  if (!control.id) control.id = `f${++uid}`;
  return h("div.field", h("label", { for: control.id }, label), control, help && h("div.help", help));
}
const input = (attrs = {}) => h("input.input", { type: "text", autocomplete: "off", spellcheck: "false", ...attrs });
function select(options, value, attrs = {}) {
  return h("select.select", { ...attrs, value }, options.map(([v, label]) => h("option", { value: v }, label)));
}
function toggle(label, checked, onchange) {
  const box = h("input", { type: "checkbox", role: "switch", checked, onchange: onchange && ((e) => onchange(e.target.checked, e)) });
  const el = h("label.switch", box, h("span", label));
  el.input = box;
  return el;
}

/** Segmented control with a sliding thumb. options: [[value, label, tone?]]. onchange may return false to refuse. */
function segmented(options, value, onchange, label) {
  const el = h("div.seg", { role: "group", "aria-label": label || "" });
  const thumb = h("div.seg-thumb");
  el.value = value;
  const sync = () => {
    buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(options[i][0] === el.value)));
    const b = buttons[options.findIndex((o) => o[0] === el.value)];
    if (b) { thumb.style.width = `${b.offsetWidth}px`; thumb.style.transform = `translateX(${b.offsetLeft}px)`; }
  };
  const buttons = options.map(([v, text, tone]) => h("button", {
    type: "button", class: tone || "", "aria-pressed": String(v === value),
    onclick: async () => {
      if (el.value === v) return;
      if ((await onchange?.(v)) === false) return;
      el.value = v;
      sync();
    },
  }, tone && h("span.d"), text));
  append(el, thumb, buttons);
  requestAnimationFrame(() => { thumb.style.transition = "none"; sync(); requestAnimationFrame(() => { thumb.style.transition = ""; }); });
  return el;
}

/** Tabs with a sliding underline. items: [[value, label]] */
function tabs(items, value, onchange) {
  const bar = h("div.bar");
  const el = h("div.tabs", { role: "tablist" });
  const buttons = items.map(([v, label]) => h("button", { type: "button", role: "tab", "aria-selected": String(v === value), onclick: () => onchange(v) }, label));
  append(el, buttons, bar);
  requestAnimationFrame(() => {
    const b = buttons[items.findIndex(([v]) => v === value)];
    if (b) { bar.style.width = `${b.offsetWidth}px`; bar.style.transform = `translateX(${b.offsetLeft}px)`; }
  });
  return el;
}

/** Amount + currency picker. Returns { el, get(), amount } */
function budgetInput(amount, currency) {
  const addon = h("span", CURRENCY[currency].sym);
  const amountInput = input({ type: "number", min: "0", step: "0.01", inputmode: "decimal", value: amount, "aria-label": "Monthly budget amount" });
  const seg = segmented([["USD", "USD"], ["INR", "INR"]], currency, (c) => { addon.textContent = CURRENCY[c].sym; }, "Budget currency");
  const el = h("div.group", h("div.affix", addon, amountInput), seg);
  return { el, amount: amountInput, get: () => ({ budget: amountInput.value.trim() || "0", budget_currency: seg.value }) };
}

// ------------------------------------------------------------------ display components
const STATUS_TONE = { active: "ok", suspended: "warn", revoked: "danger" };
const statusPill = (status) => h("span", { class: `pill ${STATUS_TONE[status] || ""}` }, titleCase(status));
const OUTCOME = { ok: ["Answered", "ok"], denied: ["Blocked", "danger"], error: ["Failed", "warn"], timeout: ["Timed out", "warn"] };
// Tier tag: a pill plus where the verdict came from, so nobody mistakes a guess for a fact.
const TIER = { free: ["Free tier", "info"], paid: ["Paid", "ok"], unknown: ["Not detected yet", ""] };
const tierPill = (t) => h("span", { class: `pill plain ${TIER[t]?.[1] || ""}` }, TIER[t]?.[0] || t);
const outcomePill = (o) => h("span", { class: `pill ${OUTCOME[o]?.[1] || ""}` }, OUTCOME[o]?.[0] || o);

/** Budget meter from the API's percentages: solid = charged, hatched = held for calls in flight. */
function meter(v, large = false) {
  const level = v.used_pct >= 100 ? "danger" : v.used_pct >= 80 ? "warn" : "";
  const fill = h("div.fill");
  fill.style.width = `${Math.min(100, v.spent_pct)}%`;
  const hold = h("div.held", { title: Number(v.reserved) > 0 ? `${money(v.reserved, v.budget_currency)} held for calls in progress` : null });
  hold.style.width = `${Math.max(0, Math.min(100 - Math.min(100, v.spent_pct), v.held_pct))}%`;
  return h("div", { class: `meter ${level}${large ? " lg" : ""}` },
    h("div.track", { role: "img", "aria-label": `${Math.round(v.used_pct)}% of budget used` }, fill, hold),
    h("div.lab",
      h("span", h("b", money(v.spent, v.budget_currency)), ` of ${money(v.budget, v.budget_currency)}`),
      h("span", Number(v.budget) > 0 ? `${Math.round(v.used_pct)}%` : "No budget")));
}

function chips(items, { lead = false, arrows = false, text = null, href = null } = {}) {
  if (!items.length) return h("span.faint", "None");
  const out = [];
  items.forEach((t, i) => {
    if (i && arrows) out.push(h("span.sep", { "aria-hidden": "true" }, "→"));
    out.push(h(href ? "a" : "span", { class: `chip${lead && i === 0 ? " lead" : ""}${text ? " text" : ""}`, href: href?.(t) }, text ? text(t) : t));
  });
  return h("div.chips", out);
}

function card({ title, hint, actions, body, flush = false, foot, cls }) {
  return h("section", { class: `card${cls ? ` ${cls}` : ""}` },
    (title || actions) && h("div.card-head", h("div", title && h("h2", title), hint && h("div.hint", hint)), actions && h("div.actions", actions)),
    body !== undefined && h("div", { class: `card-body${flush ? " flush" : ""}` }, body),
    foot && h("div.card-foot", foot));
}

function emptyState(title, text, action) {
  return h("div.empty", h("strong", title), text && h("div", text), action);
}

function pageHead({ back, title, badge, sub, actions }) {
  return h("header.head",
    h("div", back && h("a.back", { href: `#/${back}` }, "← ", titleCase(back)), h("h1", title, badge), sub && h("div.sub", sub)),
    actions && h("div.actions", actions));
}

/** cols: [{ label, render(row), align: "r", title }] */
function table(cols, rows, { onRow, empty, foot } = {}) {
  if (!rows.length) return empty || emptyState("Nothing here yet");
  return h("div.tw", h("table",
    h("thead", h("tr", cols.map((c) => h("th", { class: c.align || null, scope: "col", title: c.title }, c.label)))),
    h("tbody", rows.map((r) => h("tr", {
      class: onRow ? "click" : null,
      onclick: onRow ? (e) => { if (!e.target.closest("a, button, input, select")) onRow(r); } : null,
    }, cols.map((c) => h("td", { class: c.align || null }, c.render(r)))))),
    foot && h("tfoot", h("tr", foot.map((f, i) => h("td", { class: cols[i]?.align || null }, f))))));
}
const goCell = { label: "", align: "r", render: () => h("span.go", icon("chev")) };

function callout(kind, ...content) {
  return h("div", { class: `callout ${kind || ""}` }, icon(kind === "warn" || kind === "danger" ? "alert" : "info"), h("div", content));
}

function errorCard(e, retry) {
  return card({ body: [callout("danger", h("b", "Couldn't load this page. "), e.message),
    retry && h("div", h("button.btn", { type: "button", onclick: retry }, "Try again"))] });
}

/** Animate a number from 0 to its API value. Whole numbers count in whole steps. */
function countUp(value, format, whole = false) {
  const el = h("span", format(value));
  const to = Number(value || 0);
  if (stillMotion() || !to) return el;
  const t0 = performance.now(), duration = 900;
  const step = (t) => {
    const p = Math.min(1, (t - t0) / duration), v = to * (1 - Math.pow(1 - p, 4));
    el.textContent = p < 1 ? format(whole ? Math.round(v) : v) : format(value);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return el;
}

function niceMax(v) {
  if (v <= 0) return 0;
  const e = 10 ** Math.floor(Math.log10(v));
  const f = v / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
}

/** Sparkline of daily spend (the API's daily values, scaled to the box). */
function spark(values) {
  const W = 200, H = 30, max = Math.max(...values) || 1, step = W / Math.max(1, values.length - 1);
  const pts = values.map((v, i) => [i * step, H - (v / max) * (H - 3) - 1.5]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    svg("path", { d: `${d}L${W},${H}L0,${H}Z`, fill: "var(--accent-soft)" }),
    svg("path", { d, fill: "none", stroke: "var(--accent)", "stroke-width": "1.5", "vector-effect": "non-scaling-stroke" }),
    svg("circle", { cx: last[0], cy: last[1], r: 2.5, fill: "var(--accent)" }));
}

/** Bar chart of the API's daily rows, with a tooltip per day. */
function barChart(days, label) {
  const W = 720, H = 200, L = 48, R = 4, T = 10, B = 26;
  const values = days.map((d) => Number(d.cost_usd));
  const top = niceMax(Math.max(0, ...values));
  const plotH = H - T - B, step = (W - L - R) / Math.max(1, days.length);
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": label });
  const tip = h("div.tip");
  for (const f of top ? [0, 0.5, 1] : [0]) {
    const y = T + plotH - plotH * f;
    el.append(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }),
      svg("text", { x: L - 8, y: y + 4, "text-anchor": "end", class: "axis" }, money(top * f).replace(/\.00$/, "")));
  }
  days.forEach((d, i) => {
    const hgt = top ? Math.max((values[i] / top) * plotH, 2) : 2, x = L + i * step + step * 0.18, w = step * 0.64, y = T + plotH - hgt;
    const r = Math.min(3, w / 2, hgt);
    const bar = svg("path", { class: values[i] > 0 ? "bar" : "bar zero",
      d: `M${x},${T + plotH}V${y + r}q0,-${r} ${r},-${r}h${w - 2 * r}q${r},0 ${r},${r}V${T + plotH}Z` });
    bar.style.animationDelay = `${i * 18}ms`;
    const hit = svg("rect", { class: "hit", x: L + i * step, y: T, width: step, height: plotH });
    hit.addEventListener("mouseenter", () => {
      el.classList.add("dim");
      el.querySelectorAll(".bar.on").forEach((b) => b.classList.remove("on"));
      bar.classList.add("on");
      const scale = el.getBoundingClientRect().width / W;
      tip.replaceChildren(h("b", money(d.cost_usd)),
        h("span", `${new Date(`${d.day}T00:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} · ${int(d.requests)} calls · ${int(d.denied)} blocked`));
      tip.style.left = `${(x + w / 2) * scale}px`;
      tip.style.top = `${y * scale}px`;
      tip.classList.add("on");
    });
    el.append(bar, hit);
    if (i % 7 === 2 || i === days.length - 1) {
      el.append(svg("text", { x: L + i * step + step / 2, y: H - 6, "text-anchor": "middle", class: "axis" },
        i === days.length - 1 ? "Today" : new Date(`${d.day}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" })));
    }
  });
  el.addEventListener("mouseleave", () => { el.classList.remove("dim"); tip.classList.remove("on"); });
  return h("div.chart-wrap", el, tip);
}

function dailyChartCard(daily, title = "Daily spend") {
  const t = daily.totals;
  return card({ title, hint: `Last ${daily.days.length} days, India time`, body: [
    barChart(daily.days, `${title}: ${money(t.cost_usd)} over ${daily.days.length} days`),
    h("div.legend", h("span", `${money(t.cost_usd)} spent · ${int(t.requests)} sent to a model · ${int(t.denied)} blocked`), h("span", "Hover a day for detail")),
  ] });
}

// ------------------------------------------------------------------ shell, routing, sign-in
const NAV = [
  ["overview", "Overview"], ["agents", "Agents"], ["mandates", "Mandates"], ["providers", "Providers"], ["tools", "Tools"], ["usage", "Usage"],
  ["policy", "Policy"], ["activity", "Activity"], ["settings", "Settings"],
];
const root = () => document.getElementById("app");
let renderSeq = 0;

function themeButton() {
  const b = h("button.btn.ghost.sm.icon", { type: "button" });
  const paint = () => {
    const dark = themeNow() === "dark";
    b.replaceChildren(icon(dark ? "sun" : "moon"));
    b.setAttribute("aria-label", dark ? "Switch to light" : "Switch to dark");
    b.title = b.getAttribute("aria-label");
  };
  b.addEventListener("click", () => {
    const next = themeNow() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    pref.set("gw_theme", next);
    paint();
    refresh(); // the metal ring takes the theme when it mounts
  });
  paint();
  return b;
}

/** Sidebar status: /health/ready, every 15 seconds while the console is open. */
function healthLink() {
  const dot = h("span.dot"), text = h("span.txt", "Checking…");
  const el = h("a.health", { href: "#/settings" }, dot, text);
  const check = async () => {
    if (!el.isConnected && el.dataset.started) return;
    el.dataset.started = "1";
    const r = await fetch("/health/ready", { cache: "no-store" }).then((x) => x.json()).catch(() => ({}));
    const down = [["db", "Database"], ["litellm", "LiteLLM"], ["prices", "Prices"]].filter(([k]) => !r[k]).map(([, label]) => label);
    dot.className = `dot ${down.length ? "danger" : "live"}`;
    text.textContent = down.length ? `${down.join(", ")} not ready` : "All systems normal";
    el.title = down.length ? `${down.join(", ")} not ready` : "Database, LiteLLM and prices are ready";
    setTimeout(check, 15000);
  };
  check();
  return el;
}

function moveIndicator() {
  const a = document.querySelector('.nav a[aria-current="page"]'), ind = document.querySelector(".nav-ind");
  if (!ind) return;
  ind.style.opacity = a ? "1" : "0";
  if (a) ind.style.transform = `translateY(${a.offsetTop}px)`;
}

function shell() {
  const link = ([id, label]) => h("a", { href: `#/${id}`, "data-nav": id, title: label }, icon(id), h("span.lbl", label));
  let mini = pref.get("gw_side_mini") === "1";
  const collapse = h("button.btn.ghost.sm.icon", { type: "button" });
  const paint = () => {
    el.classList.toggle("mini", mini);
    const label = mini ? "Expand sidebar" : "Collapse sidebar";
    collapse.setAttribute("aria-label", label);
    collapse.title = label;
    collapse.replaceChildren(icon(mini ? "expand" : "collapse"));
  };
  collapse.addEventListener("click", () => { mini = !mini; pref.set("gw_side_mini", mini ? "1" : "0"); paint(); setTimeout(moveIndicator, 320); });
  const el = h("div.shell",
    h("aside.side",
      h("div.side-top", collapse),
      h("nav.nav", { "aria-label": "Main" }, h("div.nav-ind"),
        h("div.nav-label", "Manage"), NAV.slice(0, 4).map(link),
        h("div.nav-label", "Monitor"), NAV.slice(4, 6).map(link),
        h("div.nav-label", "System"), NAV.slice(6).map(link)),
      h("div.side-foot",
        healthLink(),
        h("div.who", h("div.mark", icon("gate")), h("b", "Agent Gateway"),
          h("div.side-actions", themeButton(),
            h("button.btn.ghost.sm.icon", { type: "button", onclick: confirmSignOut, "aria-label": "Sign out", title: "Sign out" }, icon("logout")))))),
    h("main.main", { id: "main" }, h("div.sheet", h("div.page", { id: "page" }))));
  paint();
  return el;
}

function signOut(message) {
  saveToken("");
  location.hash = "";
  renderLogin(message);
}
async function confirmSignOut() {
  if (await ask("Sign out?", "You'll need the admin token to sign in again.", "Sign out")) signOut();
}

function renderLogin(message) {
  const token = input({ type: "password", autocomplete: "current-password", placeholder: "adm_…", class: "mono" });
  const err = formError();
  if (message) err.show(message);
  const submit = h("button.btn.primary.block", { type: "submit" }, "Sign in");
  const form = h("form.form", { onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    const value = token.value.trim();
    if (!value) return err.show("Enter the admin token.");
    await busy(submit, async () => {
      const res = await fetch("/admin/settings", { headers: { Authorization: `Bearer ${value}` } }).catch(() => null);
      if (!res) return err.show("Can't reach the gateway. Is it running?");
      if (!res.ok) return err.show(res.status === 401 ? "That token isn't right." : `The gateway answered ${res.status}.`);
      saveToken(value);
      if (!location.hash) location.hash = "#/overview";
      render();
    });
  } }, err, field("Admin token", token, "It's ADMIN_TOKEN in the gateway's .env file. Kept for this tab only."), submit);
  root().className = "";
  root().replaceChildren(h("div.login", h("div.login-card.enter",
    h("div.brand", h("div.mark", icon("gate")), h("b", "Agent Gateway")),
    h("h1", "Sign in"),
    h("section.card", form))));
  gloss(root());
  token.focus();
}

const ROUTES = [
  [/^\/overview$/, () => pageOverview()],
  [/^\/agents$/, () => pageAgents()],
  [/^\/agents\/([a-z0-9-]+)$/, (m) => pageAgent(m[1])],
  [/^\/mandates$/, () => pageMandates()],
  [/^\/mandates\/([a-z0-9-]+)$/, (m) => pageMandate(m[1])],
  [/^\/providers$/, () => pageProviders()],
  [/^\/tools$/, () => pageTools()],
  [/^\/usage$/, (m, q) => pageUsage(q)],
  [/^\/policy$/, () => pagePolicy()],
  [/^\/activity$/, () => pageActivity()],
  [/^\/settings$/, () => pageSettings()],
];

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/overview";
  const [path, qs] = raw.split("?");
  return { path, query: new URLSearchParams(qs || "") };
}

/** Render the current route. quiet = keep the current page on screen while loading (after a change). */
async function render(quiet = false) {
  if (!store.token) return renderLogin();
  if (!document.getElementById("page")) {
    root().className = "";
    root().replaceChildren(shell());
  }
  const { path, query } = parseHash();
  const section = path.split("/")[1];
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.dataset.nav === section ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  moveIndicator();
  const page = document.getElementById("page");
  const route = ROUTES.find(([re]) => re.test(path));
  const seq = ++renderSeq;
  if (!quiet) { page.className = "page"; page.replaceChildren(h("div.skeleton"), h("div.skeleton")); }
  try {
    if (!route) throw new ApiError(404, "NOT_FOUND", "This page doesn't exist.");
    const view = await route[1](path.match(route[0]), query);
    if (seq !== renderSeq) return;
    page.className = quiet ? "page" : "page enter";
    page.replaceChildren(...[view].flat().filter(Boolean));
    gloss(page.querySelector(".head"));
    document.title = `${(NAV.find(([id]) => id === section) || [, "Admin"])[1]} · Agent Gateway`;
    if (!quiet) window.scrollTo(0, 0);
  } catch (e) {
    if (seq !== renderSeq || e.status === 401) return;
    page.replaceChildren(errorCard(e, () => render()));
  }
}
const refresh = () => render(true);
const go = (hash) => { if (location.hash === hash) render(); else location.hash = hash; };

// ------------------------------------------------------------------ overview
function kpi(label, value, foot, extra) {
  return h("div.kpi", h("div.k", label), h("div.v", value), foot && h("div.f", foot), extra);
}

/** The gate: blocked calls per step today (API), then answered. Dots are ambient: they run the lane and drop
    out only at steps that blocked something today; their rate is not data. */
function gateCard(today) {
  const steps = today.blocked_by_step;
  const canvas = h("canvas", { "aria-hidden": "true" });
  const grid = h("div.stages",
    steps.map((st, i) => h("a.stage", { href: st.step === "policy" ? "#/policy" : "#/usage?outcome=denied" },
      h("span.n", String(i + 1).padStart(2, "0")), h("span.t", st.label),
      h("span", { class: `out${st.count ? " block" : ""}` }, st.count ? `${int(st.count)} blocked` : "none blocked"))),
    h("a.stage.end", { href: "#/usage?outcome=ok" }, h("span.n", "✓"), h("span.t", "Answered"), h("span.out.pass", plural(today.ok, "call")),
      today.failed ? h("span.out", `${int(today.failed)} failed at provider`) : null));
  requestAnimationFrame(() => {
    if (stillMotion()) return;
    const ctx = canvas.getContext("2d"), dots = [];
    let last = 0;
    const loop = (t) => {
      if (!canvas.isConnected) return;
      const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
      if (canvas.width !== Math.round(r.width * dpr)) { canvas.width = r.width * dpr; canvas.height = r.height * dpr; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, r.width, r.height);
      const cards = [...grid.children].map((c) => c.getBoundingClientRect());
      if (cards[0].top !== cards[cards.length - 1].top) { requestAnimationFrame(loop); return; } // wrapped layout: no lane
      const css = getComputedStyle(document.documentElement);
      const accent = css.getPropertyValue("--accent").trim(), danger = css.getPropertyValue("--danger").trim();
      const y = cards[0].top - r.top + 22.5;
      if (t - last > 240) { last = t; dots.push({ x: 18, y, vy: 0, alpha: 1, drop: steps.findIndex((st) => st.count && Math.random() < 0.08) }); }
      ctx.strokeStyle = accent; ctx.globalAlpha = 0.18;
      ctx.beginPath(); ctx.moveTo(18, y); ctx.lineTo(r.width - 18, y); ctx.stroke();
      for (let i = dots.length - 1; i >= 0; i--) {
        const d = dots[i], stopX = d.drop >= 0 ? cards[d.drop].right - r.left - 14 : Infinity;
        if (d.x < stopX) d.x += 1.6; else { d.vy += 0.12; d.y += d.vy; d.alpha -= 0.03; }
        if (d.x > r.width - 18 || d.alpha <= 0) { dots.splice(i, 1); continue; }
        ctx.globalAlpha = d.alpha; ctx.fillStyle = d.x >= stopX ? danger : accent;
        ctx.beginPath(); ctx.arc(d.x, d.y, 2.2, 0, 7); ctx.fill();
      }
      ctx.globalAlpha = 1;
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  });
  return card({ title: "The gate today", cls: "beam", flush: true, body: h("div.flow", canvas, grid),
    actions: h("a.btn.sm.ghost", { href: "#/usage?outcome=denied" }, "Blocked calls", icon("arrow")) });
}

async function pageOverview() {
  const [o, daily] = await Promise.all([api("GET", "/admin/overview"), api("GET", "/admin/usage/daily?days=30")]);
  const c = o.counts;
  return [
    pageHead({ title: "Overview" }),
    h("section.card.kpis",
      kpi("Spent this month", countUp(o.month_cost_usd, (x) => money(x)), `≈ ${money(o.month_cost_inr, "INR")} at ₹${o.usd_inr_rate} per $1`,
        spark(daily.days.map((d) => Number(d.cost_usd)))),
      kpi("Charged calls this month", countUp(o.month_requests, int, true), [h("span.ok", int(o.today.ok)), " answered today"]),
      kpi("Blocked today", countUp(o.today.denied, int, true), "Stopped before reaching a model"),
      kpi("Active agents", [countUp(c.agents_active, int, true), h("small", ` / ${int(c.agents_total)}`)], `${c.mandates_active} of ${c.mandates_total} mandates active`)),
    gateCard(o.today),
    h("div.grid-2",
      dailyChartCard(daily),
      card({ title: "Needs attention", hint: o.attention.length ? `${o.attention.length} item${o.attention.length > 1 ? "s" : ""}` : null, flush: true,
        body: o.attention.length
          ? h("div.list", o.attention.map((it) => h("a.li", { href: it.link }, h("span", { class: `sev ${it.severity}` }),
            h("div", h("div.t", it.title), h("div.s", it.detail)), h("span.go", icon("chev")))))
          : emptyState("All clear", "No agent or mandate needs attention.") })),
    card({ title: "Agents by spend this month", actions: h("a.btn.sm.ghost", { href: "#/agents" }, "All agents", icon("arrow")), flush: true,
      body: agentTable(o.top_agents, {}, true) }),
  ];
}

// ------------------------------------------------------------------ agents
function agentTable(agents, names, short = false) {
  return table([
    { label: "Agent", render: (a) => h("div", h("a.ct", { href: `#/agents/${a.id}` }, a.name), h("div.cs", a.id)) },
    { label: "Status", render: (a) => statusPill(a.status) },
    { label: "Budget this month", render: (a) => meter(a) },
    !short && { label: "Rate limit", render: (a) => (a.rpm_limit ? h("span.num", `${int(a.rpm_limit)}/min`) : h("span.faint", "None")) },
    !short && { label: "Mandates", render: (a) => chips(a.mandates, { text: (id) => names[id] || id }) },
    !short && { label: "Token", render: (a) => (a.token_issued ? h("span.pill.ok.plain", "Issued") : h("span.pill.warn.plain", "Not issued")) },
    { label: "Last call", render: (a) => h("span.muted", ago(a.last_request_at)) },
    goCell,
  ].filter(Boolean), agents, { onRow: (a) => go(`#/agents/${a.id}`),
    empty: emptyState("No agents yet", "An agent is any program that calls models through the gateway.") });
}

async function pageAgents() {
  const [agents, mandates] = await Promise.all([api("GET", "/admin/agents"), api("GET", "/admin/mandates")]);
  const names = Object.fromEntries(mandates.map((m) => [m.id, m.name]));
  return [
    pageHead({ title: "Agents", actions: h("button.btn.primary", { type: "button", onclick: () => openCreateAgent(mandates) }, icon("plus"), "New agent") }),
    card({ flush: true, body: agentTable(agents, names) }),
  ];
}

function openCreateAgent(mandates) {
  const m = modal({ title: "New agent", sub: "It gets its own access token. You'll see the token once, right after creating." });
  const name = input({ placeholder: "e.g. ResearchOS", maxlength: "200" });
  const id = input({ class: "mono", placeholder: "researchos", maxlength: "64" });
  let idEdited = false;
  name.addEventListener("input", () => { if (!idEdited) id.value = slugify(name.value); });
  id.addEventListener("input", () => { idEdited = true; });
  const budget = budgetInput("10", "USD");
  const rpm = input({ type: "number", min: "1", step: "1", placeholder: "No limit" });
  const checks = mandates.map((md) => h("label.opt", h("span", h("div", md.name), h("div.cs", md.id)),
    h("span.switch", h("input", { type: "checkbox", value: md.id, "aria-label": `Assign to ${md.name}` }))));
  const err = formError();
  const submit = h("button.btn.primary", { type: "submit", form: "create-agent" }, "Create agent");
  append(m.body, h("form.form", { id: "create-agent", onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    if (!name.value.trim()) return err.show("Give the agent a name.");
    if (!/^[a-z0-9-]{1,64}$/.test(id.value)) return err.show("The ID can only use lowercase letters, numbers and dashes.");
    await busy(submit, async () => {
      try {
        await api("POST", "/admin/agents", { id: id.value, name: name.value.trim(), ...budget.get(), rpm_limit: Number(rpm.value) || 0 });
        const chosen = checks.map((c) => c.querySelector("input")).filter((c) => c.checked).map((c) => c.value);
        for (const mid of chosen) await api("PUT", `/admin/agents/${id.value}/mandates/${mid}`);
        const { token } = await api("POST", `/admin/agents/${id.value}/token`);
        m.close();
        go(`#/agents/${id.value}`);
        showToken(id.value, token, chosen);
      } catch (ex) { err.show(ex.message); }
    });
  } },
  err,
  h("div.row", field("Name", name), field("ID", id, "Lowercase, numbers and dashes. Can't be changed later.")),
  h("div.field", h("span.label", "Monthly budget"), budget.el, h("div.help", "Resets on the 1st of every month, India time.")),
  field("Rate limit (calls per minute)", rpm, "Optional. Stops runaway loops."),
  h("div.field", h("span.label", "Mandates"), mandates.length ? h("div.opts", checks) : h("div.help", "No mandates yet. You can assign one later."))));
  append(m.foot, h("button.btn", { type: "button", onclick: m.close }, "Cancel"), submit);
}

function showToken(agentId, token, mandateIds) {
  const m = modal({ title: "Access token", sub: `For agent ${agentId}`, wide: true });
  append(m.body,
    callout("warn", h("b", "Copy it now. "), "It won't be shown again. The gateway only keeps a fingerprint of it."),
    h("div.secret", token),
    h("div", copyButton(token, "Copy token")),
    h("div.field", h("span.label", "How the agent connects"),
      h("div.help", "Use any OpenAI-compatible client. The token is the API key; the base URL picks the mandate."),
      mandateIds.length
        ? mandateIds.map((mid) => h("div.field", h("span.help", `Base URL for mandate ${mid}`), copyRow(`${location.origin}/m/${mid}/v1`)))
        : h("div.help", "Assign the agent to a mandate to get its base URL.")));
  append(m.foot, h("button.btn.primary", { type: "button", onclick: m.close }, "Done"));
}

// ------------------------------------------------------------------ shared detail cards
/** Monthly budget card for an agent or mandate. kind: "agents" | "mandates" */
function budgetCard(kind, v, settings) {
  const rate = settings.usd_inr_rate;
  const body = h("div.form");
  const view = () => body.replaceChildren(...[
    h("div.big", h("strong", countUp(v.spent, (x) => money(x, v.budget_currency))), h("span.muted", `of ${money(v.budget, v.budget_currency)} this month`)),
    meter(v, true),
    h("div.facts",
      h("div.fact", h("div.k", "Remaining"), h("div.v", money(v.remaining, v.budget_currency))),
      h("div.fact", h("div.k", "Held for calls in flight"), h("div.v", money(v.reserved, v.budget_currency))),
      h("div.fact", h("div.k", "Resets on"), h("div.v", dayMonth(settings.budget_resets_on)))),
    v.budget_currency === "INR" && h("div.help", `Billed in USD (${money(v.spent_usd)}) and converted at ₹${rate} per $1. Change the rate in Settings.`),
  ].filter(Boolean));
  const edit = () => {
    const b = budgetInput(String(Number(v.budget)), v.budget_currency);
    const err = formError();
    const save = h("button.btn.primary", { type: "submit" }, "Save budget");
    body.replaceChildren(h("form.form", { onsubmit: async (e) => {
      e.preventDefault();
      err.clear();
      const next = b.get();
      if (!(await ask("Change the monthly budget?", `New budget: ${money(next.budget, next.budget_currency)}. It applies to the next request. Below what's already spent blocks new requests until next month.`, "Save budget"))) return;
      await busy(save, async () => {
        try {
          await api("PATCH", `/admin/${kind}/${v.id}`, next);
          toast("Budget saved. It applies to the next request.");
          refresh();
        } catch (ex) { err.show(ex.message); }
      });
    } }, err, h("div.field", h("span.label", "Monthly budget"), b.el,
      h("div.help", "Lowering it below what's already spent blocks new requests until next month.")),
    h("div.form-actions", h("button.btn", { type: "button", onclick: view }, "Cancel"), save)));
    b.amount.focus();
  };
  view();
  return card({ title: "Monthly budget", hint: `${monthName(settings.budget_month)} · India time`,
    actions: h("button.btn.sm", { type: "button", onclick: edit }, "Edit"), body });
}

const CHANGE_TEXT = {
  agent_created: "Agent created", agent_updated: "Agent updated", token_issued: "Token issued", token_rotated: "Token rotated",
  mandate_created: "Mandate created", mandate_updated: "Mandate updated", assigned: "Assigned to mandate",
  unassigned: "Removed from mandate", policy_updated: "Policy updated", settings_updated: "Settings updated",
  tier_set: "Key tier set", provider_key_added: "Provider key added", provider_key_removed: "Provider key removed",
  model_added: "Model added", model_removed: "Model removed",
};
const FIELD_TEXT = {
  status: "Status", name: "Name", description: "Description", budget_limit_micros: "Budget", budget_currency: "Currency",
  rpm_limit: "Rate limit", allowed_models: "Models", default_max_tokens: "Default max tokens", fallback_enabled: "Fallback",
  usd_inr_rate: "USD→INR rate",
};
const WARN_CHANGES = new Set(["unassigned", "provider_key_removed", "model_removed", "token_rotated"]);
function fieldValue(key, v) {
  if (v == null) return key === "rpm_limit" ? "none" : "—";
  if (key === "budget_limit_micros") return String(Number(v) / 1e6);
  if (key === "allowed_models") return v.length ? v.join(" → ") : "none";
  if (key === "fallback_enabled") return v ? "on" : "off";
  return String(v);
}
function changeDetails(e) {
  const d = e.detail || {};
  if (d.changed) return Object.entries(d.changed).map(([k, c]) =>
    h("div", h("span.faint", `${FIELD_TEXT[k] || k}: `), `${fieldValue(k, c.before)} → ${fieldValue(k, c.after)}`));
  if (e.decision === "policy_updated") return h("div", `${d.from} → ${d.to}`, d.note ? h("span.faint", ` · ${d.note}`) : null);
  if (d.provider_model) return h("div", `${d.name} → ${d.provider_model} (key ${d.provider_key})`);
  if (d.provider) return h("div", `${d.name} · ${d.provider}`);
  if (d.name) return h("div", `${d.name}${d.budget ? ` · budget ${d.budget} ${d.currency || ""}` : ""}`);
  return null;
}
const changeTone = (e) => (WARN_CHANGES.has(e.decision) || ["suspended", "revoked"].includes(e.detail?.changed?.status?.after) ? "warn" : "info");

/** Changes as a timeline, grouped under the day they happened. */
function timeline(events, { links = true } = {}) {
  if (!events.length) return emptyState("No changes yet");
  const days = [];
  for (const e of events) {
    const day = new Date(e.at).toDateString();
    if (days.at(-1)?.day !== day) days.push({ day, events: [] });
    days.at(-1).events.push(e);
  }
  const today = new Date().toDateString();
  return h("div.tl", days.map(({ day, events: evs }) => [
    h("div.day", day === today ? "Today" : dayLong.format(new Date(day))),
    evs.map((e) => h("div.ev", h("span", { class: `dot ${changeTone(e)}` }),
      h("div", h("div.t", CHANGE_TEXT[e.decision] || e.decision), h("div.s", changeDetails(e)),
        links && (e.agent_id || e.mandate_id) && h("div.chips",
          e.agent_id && h("a.chip", { href: `#/agents/${e.agent_id}` }, e.agent_id),
          e.mandate_id && h("a.chip", { href: `#/mandates/${e.mandate_id}` }, e.mandate_id))),
      h("span.when", timeOnly.format(new Date(e.at))))),
  ]));
}

// ------------------------------------------------------------------ agent detail
async function pageAgent(id) {
  const [a, mandates, daily, recent, events, settings] = await Promise.all([
    api("GET", `/admin/agents/${id}`), api("GET", "/admin/mandates"), api("GET", `/admin/usage/daily?days=30&agent=${id}`),
    api("GET", `/admin/usage?agent=${id}&limit=8`), api("GET", `/admin/audit?agent=${id}&limit=12`), api("GET", "/admin/settings")]);
  const names = Object.fromEntries(mandates.map((m) => [m.id, m.name]));

  const STATUS_ASK = {
    active: [`Set ${a.name} active?`, "Its token works again and its requests are checked as normal.", "Set active", false],
    suspended: [`Suspend ${a.name}?`, "Every request it makes is blocked until you set it active again.", "Suspend agent", true],
    revoked: [`Revoke ${a.name}?`, "Its token stops working immediately and every request is blocked. You can restore it later.", "Revoke agent", true],
  };
  const setStatus = async (to) => {
    if (!(await ask(...STATUS_ASK[to]))) return false;
    try {
      await api("PATCH", `/admin/agents/${id}`, { status: to });
      toast(to === "active" ? `${a.name} is active again.` : `${a.name} is ${to}. New requests are blocked.`);
    } catch (ex) { toast(ex.message, "error"); }
    refresh();
  };

  const rotate = async () => {
    if (a.token_issued && !(await ask("Rotate token?", "The current token stops working immediately. Put the new one in the agent's configuration.", "Rotate token", true))) return;
    try {
      const { token } = await api("POST", `/admin/agents/${id}/token`);
      showToken(id, token, a.mandates);
      refresh();
    } catch (ex) { toast(ex.message, "error"); }
  };

  const assignSwitch = (md) => h("span.switch", h("input", { type: "checkbox", role: "switch", checked: a.mandates.includes(md.id), "aria-label": `Assign to ${md.name}`,
    onchange: async (e) => {
      const on = e.target.checked;
      if (!on && !(await ask(`Remove ${a.name} from ${md.name}?`, "Its requests under this mandate are blocked straight away.", "Remove", true))) { e.target.checked = true; return; }
      try {
        await api(on ? "PUT" : "DELETE", `/admin/agents/${id}/mandates/${md.id}`);
        toast(on ? `Assigned to ${md.name}.` : `Removed from ${md.name}. Its requests there are blocked.`);
      } catch (ex) { toast(ex.message, "error"); }
      refresh();
    } }));

  const rpm = input({ type: "number", min: "1", step: "1", placeholder: "No limit", value: a.rpm_limit ?? "" });
  const name = input({ value: a.name, maxlength: "200" });
  const saveLimits = h("button.btn", { type: "submit" }, "Save");

  return [
    pageHead({ back: "agents", title: a.name, badge: statusPill(a.status),
      sub: [h("span.mono", a.id), ` · last call ${ago(a.last_request_at).toLowerCase()}`],
      actions: segmented([["active", "Active", "ok"], ["suspended", "Suspended", "warn"], ["revoked", "Revoked", "danger"]], a.status, setStatus, "Agent status") }),
    a.status !== "active" && callout(a.status === "revoked" ? "danger" : "warn",
      h("b", `This agent is ${a.status}. `), "Every request it makes is blocked before reaching a model."),
    h("div.grid-2",
      h("div.stack",
        budgetCard("agents", a, settings),
        dailyChartCard(daily),
        card({ title: "Recent calls", actions: h("a.btn.sm.ghost", { href: `#/usage?agent=${id}` }, "All calls", icon("arrow")), flush: true,
          body: usageTable(recent, { agent: false, compact: true }) })),
      h("div.stack",
        card({ title: "Access token", actions: h("button.btn.sm", { type: "button", onclick: rotate }, icon("key"), a.token_issued ? "Rotate" : "Issue token"),
          body: [
            a.token_issued ? h("div.muted", h("span.pill.ok.plain", "Issued"), " Only a fingerprint is stored.")
              : callout("warn", "No token yet. Issue one so the agent can make calls."),
            a.mandates.length
              ? a.mandates.map((mid) => h("div.field", h("span.help", `Base URL for ${names[mid] || mid}`), copyRow(`${location.origin}/m/${mid}/v1`)))
              : h("div.help", "Assign a mandate below to get a base URL."),
          ] }),
        card({ title: "Mandates", body: mandates.length
          ? h("div.opts", mandates.map((md) => h("div.opt", h("span", h("a.link", { href: `#/mandates/${md.id}` }, md.name), h("div.cs", md.id)), assignSwitch(md))))
          : h("div.help", "No mandates yet. Create one under Mandates.") }),
        card({ title: "Limits and details", body: h("form.form", { onsubmit: async (e) => {
          e.preventDefault();
          if (!(await ask("Save limits and details?", "The rate limit applies to the next request.", "Save"))) return;
          await busy(saveLimits, async () => {
            try {
              await api("PATCH", `/admin/agents/${id}`, { name: name.value.trim(), rpm_limit: Number(rpm.value) || 0 });
              toast("Saved.");
              refresh();
            } catch (ex) { toast(ex.message, "error"); }
          });
        } }, h("div.row", field("Name", name), field("Calls per minute", rpm, "Includes fallback attempts. Empty = no limit.")),
        h("div.form-actions", saveLimits)) }),
        card({ title: "Recent changes", flush: true, body: timeline(events, { links: false }) }))),
  ];
}

/** Usage records table. opts: { agent: show agent column, compact } */
function usageTable(rows, { agent = true, mandate = true, compact = false, tiers = {} } = {}) {
  const cols = [
    { label: "Time", render: (r) => h("span.num.muted", when(r.at)) },
    agent && { label: "Agent", render: (r) => h("a.mono.link", { href: `#/agents/${r.agent_id}` }, r.agent_id) },
    mandate && !compact && { label: "Mandate", render: (r) => (r.mandate_id ? h("span.mono.muted", r.mandate_id) : "—") },
    { label: "Model or tool", render: (r) => {
      if (r.tool_server) return h("div", h("div.mono", r.tool_name || "tool server"), h("div.cs", `tool · ${r.tool_server}`));
      if (!r.model_requested) return h("span.faint", "—");
      if (r.model_served && r.model_served !== r.model_requested) {
        return h("div", h("div.mono", r.model_served), h("div.cs", `fallback from ${r.model_requested}`));
      }
      return h("span.mono", r.model_requested);
    } },
    { label: "Result", render: (r) => h("div", outcomePill(r.outcome), r.reason_code && h("div.cs.sans", reasonText(r.reason_code))) },
    !compact && { label: "Tokens in / out", align: "r", render: (r) => (r.prompt_tokens == null ? h("span.faint", "—") : h("span.num", `${int(r.prompt_tokens)} / ${int(r.completion_tokens)}`)) },
    !compact && { label: "Held", align: "r", title: "The most the call could cost, reserved before it ran. Never charged; only the charged amount counts against budgets.",
      render: (r) => h("span.num.faint", Number(r.reserved_usd) ? money(r.reserved_usd) : "—") },
    { label: compact ? "Cost" : "Charged", align: "r", title: compact ? null : "The real cost: tokens the provider reported × the model's list price, including cache discounts.",
      render: (r) => h("div",
        h("span.num", Number(r.cost_usd) ? money(r.cost_usd) : "—"),
        tiers[r.model_served] === "free" && Number(r.cost_usd) ? h("div.cs", { title: "Charged at list price; a free-tier key pays $0" }, "free tier") : null) },
    !compact && { label: "Took", align: "r", render: (r) => h("span.num.muted", r.latency_ms == null ? "—" : r.latency_ms < 1000 ? `${r.latency_ms} ms` : `${(r.latency_ms / 1000).toFixed(1)} s`) },
  ].filter(Boolean);
  return table(cols, rows, { empty: emptyState("No calls yet", "Calls appear here as soon as an agent makes them.") });
}

// ------------------------------------------------------------------ mandates
/** Ordered model list editor: drag or arrows. The first model is the primary; the rest are tried in order.
    Fires "change" on el whenever the list changes. */
function modelOrderEditor(initial, models) {
  let list = [...initial];
  let dragFrom = null;
  const priceOf = Object.fromEntries(models.map((m) => [m.model, m]));
  const el = h("div.form");
  const changed = () => { draw(); el.dispatchEvent(new Event("change")); };
  const draw = () => {
    const rest = models.map((m) => m.model).filter((m) => !list.includes(m));
    const pick = select([["", rest.length ? "Choose a model…" : "All models added"], ...rest.map((m) => [m, m])], "", { "aria-label": "Model to add" });
    const move = (i, d) => { [list[i], list[i + d]] = [list[i + d], list[i]]; changed(); };
    el.replaceChildren(
      list.length ? h("ol.order", { "aria-label": "Allowed models in fallback order" }, list.map((m, i) => {
        const p = priceOf[m];
        const li = h("li.oi", { draggable: "true" }, h("span.grip", icon("grip")), h("span.idx", String(i + 1)),
          h("div", h("div.name", m), h("div.role", [i === 0 ? "Primary" : `Fallback ${i}`,
            p ? ` · $${Number(p.input_usd_per_1m)} in / $${Number(p.output_usd_per_1m)} out per 1M` : " · not configured in LiteLLM",
            p?.tier === "free" ? " · free tier" : ""])),
          h("div.tools",
            h("button.btn.ghost.sm.icon", { type: "button", "aria-label": `Move ${m} up`, disabled: i === 0, onclick: () => move(i, -1) }, icon("up")),
            h("button.btn.ghost.sm.icon", { type: "button", "aria-label": `Move ${m} down`, disabled: i === list.length - 1, onclick: () => move(i, 1) }, icon("down")),
            h("button.btn.ghost.sm.icon", { type: "button", "aria-label": `Remove ${m}`, onclick: () => { list = list.filter((x) => x !== m); changed(); } }, icon("x"))));
        li.addEventListener("dragstart", () => { dragFrom = i; li.classList.add("drag"); });
        li.addEventListener("dragend", () => li.classList.remove("drag"));
        li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("over"); });
        li.addEventListener("dragleave", () => li.classList.remove("over"));
        li.addEventListener("drop", (e) => {
          e.preventDefault();
          if (dragFrom == null || dragFrom === i) return draw();
          const [x] = list.splice(dragFrom, 1);
          list.splice(i, 0, x);
          dragFrom = null;
          changed();
        });
        return li;
      })) : h("div.help", "No models yet. Agents can't make calls under this mandate until you add one."),
      h("div.group", pick, h("button.btn", { type: "button", disabled: !rest.length, onclick: () => {
        if (pick.value) { list.push(pick.value); changed(); }
      } }, icon("plus"), "Add")));
  };
  draw();
  return { el, get: () => [...list] };
}

async function pageMandates() {
  const [mandates, models] = await Promise.all([api("GET", "/admin/mandates"), api("GET", "/admin/models")]);
  return [
    pageHead({ title: "Mandates", actions: h("button.btn.primary", { type: "button", onclick: () => openCreateMandate(models) }, icon("plus"), "New mandate") }),
    card({ flush: true, body: table([
      { label: "Mandate", render: (m) => h("div", h("a.ct", { href: `#/mandates/${m.id}` }, m.name), h("div.cs", m.id)) },
      { label: "Status", render: (m) => statusPill(m.status) },
      { label: "Budget this month", render: (m) => meter(m) },
      { label: "Models in fallback order", render: (m) => chips(m.allowed_models, { lead: true, arrows: m.fallback_enabled }) },
      { label: "Agents", align: "r", render: (m) => h("span.num", int(m.agents.length)) },
      goCell,
    ], mandates, { onRow: (m) => go(`#/mandates/${m.id}`), empty: emptyState("No mandates yet", "Create one, then assign agents to it.") }) }),
  ];
}

function openCreateMandate(models) {
  const m = modal({ title: "New mandate", sub: "Agents on it can use only these models, within its monthly budget.", wide: true });
  const name = input({ placeholder: "e.g. ResearchOS development", maxlength: "200" });
  const id = input({ class: "mono", placeholder: "researchos-dev", maxlength: "64" });
  let idEdited = false;
  name.addEventListener("input", () => { if (!idEdited) id.value = slugify(name.value); });
  id.addEventListener("input", () => { idEdited = true; });
  const description = h("textarea.textarea", { maxlength: "2000", rows: "2", placeholder: "What is this mandate for? (optional)" });
  const budget = budgetInput("10", "USD");
  const order = modelOrderEditor([], models);
  const maxTokens = input({ type: "number", min: "1", max: "65536", step: "1", value: "4096" });
  const fallback = toggle("Try the next model when one is busy", true);
  const err = formError();
  const submit = h("button.btn.primary", { type: "submit", form: "create-mandate" }, "Create mandate");
  append(m.body, h("form.form", { id: "create-mandate", onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    if (!name.value.trim()) return err.show("Give the mandate a name.");
    if (!/^[a-z0-9-]{1,64}$/.test(id.value)) return err.show("The ID can only use lowercase letters, numbers and dashes.");
    await busy(submit, async () => {
      try {
        await api("POST", "/admin/mandates", { id: id.value, name: name.value.trim(), description: description.value.trim(),
          ...budget.get(), allowed_models: order.get(), default_max_tokens: Number(maxTokens.value) || 4096,
          fallback_enabled: fallback.input.checked });
        m.close();
        toast("Mandate created. Assign agents to it.");
        go(`#/mandates/${id.value}`);
      } catch (ex) { err.show(ex.message); }
    });
  } },
  err,
  h("div.row", field("Name", name), field("ID", id, "Part of the agent's base URL. Can't be changed later.")),
  field("Description", description),
  h("div.field", h("span.label", "Monthly budget"), budget.el, h("div.help", "Shared by every agent on it. Each agent also has its own budget.")),
  h("div.field", h("span.label", "Models"), h("div.help", "The first is the primary. Drag to reorder."), order.el),
  h("div.row", field("Default answer limit (tokens)", maxTokens, "Used when an agent doesn't set max_tokens."),
    h("div.field", h("span.label", "Fallback"), fallback))));
  append(m.foot, h("button.btn", { type: "button", onclick: m.close }, "Cancel"), submit);
}

async function pageMandate(id) {
  const [md, agents, models, daily, recent, events, settings] = await Promise.all([
    api("GET", `/admin/mandates/${id}`), api("GET", "/admin/agents"), api("GET", "/admin/models"),
    api("GET", `/admin/usage/daily?days=30&mandate=${id}`), api("GET", `/admin/usage?mandate=${id}&limit=8`),
    api("GET", `/admin/audit?mandate=${id}&limit=12`), api("GET", "/admin/settings")]);
  const assigned = agents.filter((a) => md.agents.includes(a.id));
  const unassigned = agents.filter((a) => !md.agents.includes(a.id));

  const setStatus = async (to) => {
    const confirmed = to === "suspended"
      ? await ask(`Suspend ${md.name}?`, `Every request under it is blocked, for all ${assigned.length} assigned agent(s).`, "Suspend mandate", true)
      : await ask(`Set ${md.name} active?`, "Its agents can make calls under it again.", "Set active");
    if (!confirmed) return false;
    try {
      await api("PATCH", `/admin/mandates/${id}`, { status: to });
      toast(to === "active" ? `${md.name} is active again.` : `${md.name} is suspended.`);
    } catch (ex) { toast(ex.message, "error"); }
    refresh();
  };

  const order = modelOrderEditor(md.allowed_models, models);
  const fallback = toggle("Try the next model when one is busy", md.fallback_enabled);
  const maxTokens = input({ type: "number", min: "1", max: "65536", step: "1", value: md.default_max_tokens });
  const saveModels = h("button.btn.primary", { type: "submit", disabled: true }, "Save models");
  const enable = () => { saveModels.disabled = false; };
  order.el.addEventListener("change", enable);
  fallback.input.addEventListener("change", enable);
  maxTokens.addEventListener("input", enable);
  const modelsErr = formError();

  const pick = select([["", unassigned.length ? "Choose an agent…" : "All agents assigned"], ...unassigned.map((a) => [a.id, a.name])], "", { "aria-label": "Agent to assign" });
  const assign = async (agent, on) => {
    if (!on && !(await ask(`Remove ${agent.name} from ${md.name}?`, "Its requests under this mandate are blocked straight away.", "Remove", true))) return;
    try {
      await api(on ? "PUT" : "DELETE", `/admin/agents/${agent.id}/mandates/${id}`);
      toast(on ? `${agent.name} assigned.` : `${agent.name} removed. Its requests under this mandate are blocked.`);
    } catch (ex) { toast(ex.message, "error"); }
    refresh();
  };

  const name = input({ value: md.name, maxlength: "200" });
  const description = h("textarea.textarea", { maxlength: "2000", rows: "2", value: md.description });
  const saveDetails = h("button.btn", { type: "submit" }, "Save details");

  return [
    pageHead({ back: "mandates", title: md.name, badge: statusPill(md.status), sub: [h("span.mono", id), md.description ? ` · ${md.description}` : ""],
      actions: segmented([["active", "Active", "ok"], ["suspended", "Suspended", "warn"]], md.status, setStatus, "Mandate status") }),
    md.status !== "active" && callout("warn", h("b", "This mandate is suspended. "), "Every request under it is blocked."),
    h("div.grid-2",
      h("div.stack",
        budgetCard("mandates", md, settings),
        dailyChartCard(daily),
        card({ title: "Recent calls", actions: h("a.btn.sm.ghost", { href: `#/usage?mandate=${id}` }, "All calls", icon("arrow")), flush: true,
          body: usageTable(recent, { mandate: false, compact: true }) })),
      h("div.stack",
        card({ title: "Models", body: h("form.form", { onsubmit: async (e) => {
          e.preventDefault();
          modelsErr.clear();
          if (!(await ask("Save models?", `Agents on ${md.name} can use only these models, in this order, from the next request.`, "Save models"))) return;
          await busy(saveModels, async () => {
            try {
              await api("PATCH", `/admin/mandates/${id}`, { allowed_models: order.get(), fallback_enabled: fallback.input.checked,
                default_max_tokens: Number(maxTokens.value) || 4096 });
              toast("Models saved. They apply to the next request.");
              refresh();
            } catch (ex) { modelsErr.show(ex.message); }
          });
        } }, modelsErr, order.el, fallback,
        field("Default answer limit (tokens)", maxTokens, "Caps answer length and the budget hold when an agent doesn't set max_tokens."),
        h("div.form-actions", saveModels)) }),
        card({ title: "Agents", hint: `${assigned.length} assigned`, flush: true, body: [
          assigned.length ? h("div.list", assigned.map((a) => h("div.li", h("span", { class: `dot ${STATUS_TONE[a.status]}` }),
            h("div", h("a.t", { href: `#/agents/${a.id}` }, a.name), h("div.s", `${titleCase(a.status)} · ${money(a.spent, a.budget_currency)} spent this month`)),
            h("button.btn.sm.ghost", { type: "button", onclick: () => assign(a, false) }, "Remove")))) : emptyState("No agents assigned"),
          h("div.card-pad", h("div.group", pick, h("button.btn", { type: "button", disabled: !unassigned.length,
            onclick: () => { const a = unassigned.find((x) => x.id === pick.value); if (a) assign(a, true); } }, icon("plus"), "Assign"))),
        ] }),
        card({ title: "Details", body: h("form.form", { onsubmit: async (e) => {
          e.preventDefault();
          if (!(await ask("Save details?", "The name and description change everywhere in the console.", "Save details"))) return;
          await busy(saveDetails, async () => {
            try {
              await api("PATCH", `/admin/mandates/${id}`, { name: name.value.trim(), description: description.value.trim() });
              toast("Details saved.");
              refresh();
            } catch (ex) { toast(ex.message, "error"); }
          });
        } }, field("Name", name), field("Description", description), h("div.form-actions", saveDetails)) }),
        card({ title: "Recent changes", flush: true, body: timeline(events, { links: false }) }))),
  ];
}

// ------------------------------------------------------------------ usage
function setQuery(q, changes) {
  for (const [k, v] of Object.entries(changes)) v ? q.set(k, v) : q.delete(k);
  go(`#/usage${q.toString() ? `?${q}` : ""}`);
}

async function pageUsage(q) {
  const tab = q.get("tab") === "monthly" ? "monthly" : "requests";
  return [
    pageHead({ title: "Usage" }),
    tabs([["requests", "Calls"], ["monthly", "Monthly totals"]], tab, (t) => setQuery(q, { tab: t === "requests" ? "" : t })),
    tab === "monthly" ? await usageMonthly(q) : await usageRequests(q),
  ];
}

async function usageRequests(q) {
  const filters = { agent: q.get("agent") || "", mandate: q.get("mandate") || "", outcome: q.get("outcome") || "", model: q.get("model") || "" };
  const params = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
  const [rows, agents, mandates, models] = await Promise.all([
    api("GET", `/admin/usage?limit=50&${params}`), api("GET", "/admin/agents"), api("GET", "/admin/mandates"), api("GET", "/admin/models")]);
  const pick = (label, key, options) => field(label, select([["", "All"], ...options], filters[key], { onchange: (e) => setQuery(q, { [key]: e.target.value }) }));
  const bar = h("div.toolbar",
    pick("Agent", "agent", agents.map((a) => [a.id, a.name])),
    pick("Mandate", "mandate", mandates.map((m) => [m.id, m.name])),
    pick("Result", "outcome", [["ok", "Answered"], ["denied", "Blocked"], ["error", "Failed"], ["timeout", "Timed out"]]),
    pick("Model", "model", models.map((m) => [m.model, m.model])),
    Object.values(filters).some(Boolean) && h("button.btn.ghost", { type: "button", onclick: () => go("#/usage") }, icon("x"), "Clear filters"));
  const tiers = Object.fromEntries(models.map((m) => [m.model, m.tier]));
  const holder = h("div", usageTable(rows, { tiers }));
  let last = rows.length ? rows[rows.length - 1].id : null;
  const more = h("button.btn.sm", { type: "button", onclick: () => busy(more, async () => {
    const next = await api("GET", `/admin/usage?limit=50&before_id=${last}&${params}`);
    rows.push(...next);
    last = rows.length ? rows[rows.length - 1].id : null;
    holder.replaceChildren(usageTable(rows, { tiers }));
    if (next.length < 50) more.remove();
  }) }, "Load older calls");
  return card({ flush: true, body: [bar, holder], foot: rows.length === 50 && more });
}

async function usageMonthly(q) {
  const by = ["agent", "mandate", "model"].includes(q.get("by")) ? q.get("by") : "agent";
  const params = new URLSearchParams({ by, ...(q.get("month") ? { month: q.get("month") } : {}) });
  const data = await api("GET", `/admin/usage/monthly?${params}`);
  const href = { agent: (k) => `#/agents/${k}`, mandate: (k) => `#/mandates/${k}`, model: null }[by];
  const share = (r) => {
    const fill = h("div.fill");
    fill.style.width = `${r.share_pct}%`;
    return h("div.meter.share", { title: `${r.share_pct}% of the month's cost` }, h("div.track", fill));
  };
  const t = data.totals;
  return card({ flush: true, body: [
    h("div.toolbar",
      field("Month", select(data.months.map((m) => [m, monthName(m) + (m === data.current_month ? " (current)" : "")]), data.month,
        { onchange: (e) => setQuery(q, { month: e.target.value === data.current_month ? "" : e.target.value }) })),
      h("div.field", h("span.label", "Group by"), segmented([["agent", "Agent"], ["mandate", "Mandate"], ["model", "Model"]], by,
        (v) => setQuery(q, { by: v === "agent" ? "" : v }), "Group by"))),
    table([
      { label: titleCase(by), render: (r) => (href ? h("a.mono.link", { href: href(r.key) }, r.key) : h("span.mono", r.key)) },
      { label: "Model calls", align: "r", render: (r) => h("span.num", int(r.requests)) },
      { label: "Input tokens", align: "r", render: (r) => h("span.num", int(r.prompt_tokens)) },
      { label: "Output tokens", align: "r", render: (r) => h("span.num", int(r.completion_tokens)) },
      { label: "Cost", align: "r", render: (r) => h("span.num", money(r.cost_usd)) },
      { label: "Share of cost", render: share },
    ], data.rows, {
      empty: emptyState(`Nothing recorded for ${monthName(data.month)}`),
      foot: ["Total", int(t.requests), int(t.prompt_tokens), int(t.completion_tokens), money(t.cost_usd), ""],
    }),
  ] });
}

// ------------------------------------------------------------------ policy
const BUILTIN_TEXT = {
  AGENT_REVOKED: "The agent is revoked", AGENT_INACTIVE: "The agent is suspended",
  MANDATE_NOT_ASSIGNED: "The agent isn't on the mandate, or the mandate doesn't exist",
  MANDATE_INACTIVE: "The mandate is suspended", MODEL_NOT_ALLOWED: "The model isn't on the mandate's list",
  TOOL_SERVER_NOT_ALLOWED: "The tool server isn't allowed under the mandate", MCP_TOOL_NOT_ALLOWED: "The tool isn't on the mandate's list for that server",
};
const FACT_TEXT = { "agent.id": "Agent", "mandate.id": "Mandate", "model.name": "Model", "tool.server": "Tool server" };

async function pagePolicy() {
  const [p, agents, mandates, models, tools] = await Promise.all([
    api("GET", "/admin/policy"), api("GET", "/admin/agents"), api("GET", "/admin/mandates"), api("GET", "/admin/models"),
    api("GET", "/admin/tool-servers")]);
  const copy = () => p.custom.map((r) => ({ ...r, when: { ...r.when } }));
  let draft = copy();
  const dirty = () => JSON.stringify(draft) !== JSON.stringify(p.custom);
  const seen = new Set();
  const builtin = p.builtin.filter((r) => !seen.has(r.reason) && seen.add(r.reason));
  const nextVersion = p.history.length ? p.history[0].version + 1 : 1;

  const rulesHost = h("div");
  const note = input({ placeholder: "What changed and why (optional)", maxlength: "300", "aria-label": "Change note" });
  const saveBtn = h("button.btn.primary.sm", { type: "button" }, `Save as version ${nextVersion}`);
  const unsaved = h("div.unsaved", { hidden: true }, h("b", "Unsaved changes"), note,
    h("button.btn.ghost.sm", { type: "button", onclick: async () => {
      if (!(await ask("Discard your changes?", "Your rules go back to the active version.", "Discard", true))) return;
      draft = copy();
      drawRules();
    } }, "Discard"), saveBtn);
  saveBtn.addEventListener("click", async () => {
    if (!(await ask(`Save policy version ${nextVersion}?`, "It applies to the next request.", "Save"))) return;
    await busy(saveBtn, async () => {
      try {
        const res = await api("PUT", "/admin/policy", { rules: draft, note: note.value.trim() });
        toast(`Saved. Policy ${res.version} applies to the next request.`);
        refresh();
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  function drawRules() {
    rulesHost.replaceChildren(table([
      { label: "#", render: (r) => h("span.num.faint", String(draft.indexOf(r) + 1)) },
      { label: "Blocks a request when", render: (r) => h("div.chips", Object.entries(r.when).map(([k, v], i) =>
        [i ? h("span.sep", "and") : null, h("span.chip.text", `${FACT_TEXT[k] || k} is ${v}`)])) },
      { label: "Reason and message", render: (r) => h("div", h("span.code", r.reason), h("div.help", r.message)) },
      { label: "", align: "r", render: (r) => h("button.btn.ghost.sm.icon", { type: "button", "aria-label": `Remove rule ${r.reason}`,
        onclick: async () => {
          if (!(await ask(`Remove rule ${r.reason}?`, "It comes off your draft. Nothing changes until you save a new version.", "Remove rule", true))) return;
          draft = draft.filter((x) => x !== r);
          drawRules();
        } }, icon("x")) },
    ], draft, { empty: emptyState("No rules of your own yet", "Add one below, for example to keep one agent off an expensive model.") }));
    unsaved.hidden = !dirty();
  }
  drawRules();

  const cond = (fact, options) => select([["", `any ${FACT_TEXT[fact].toLowerCase()}`], ...options], "", { "aria-label": FACT_TEXT[fact] });
  const cAgent = cond("agent.id", agents.map((a) => [a.id, a.name]));
  const cMandate = cond("mandate.id", mandates.map((m) => [m.id, m.name]));
  const cModel = cond("model.name", models.map((m) => [m.model, m.model]));
  const cTool = cond("tool.server", tools.servers.map((s) => [s.id, s.name]));
  const reason = input({ class: "mono", placeholder: "NO_PRO_MODELS_FOR_TESTS", maxlength: "48" });
  reason.addEventListener("input", () => { reason.value = reason.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"); });
  const message = input({ placeholder: "Shown to the agent in the error", maxlength: "300" });
  const addErr = formError();
  const addForm = h("form.form", { onsubmit: (e) => {
    e.preventDefault();
    addErr.clear();
    const when = Object.fromEntries([["agent.id", cAgent.value], ["mandate.id", cMandate.value], ["model.name", cModel.value],
      ["tool.server", cTool.value]].filter(([, v]) => v));
    if (!Object.keys(when).length) return addErr.show("Pick at least one condition. A rule with none would block everything.");
    if (when["model.name"] && when["tool.server"]) return addErr.show("A rule blocks a model or a tool server, not both. Make two rules.");
    if (!/^[A-Z][A-Z0-9_]{2,47}$/.test(reason.value)) return addErr.show("The reason code needs 3–48 capital letters, numbers or underscores, starting with a letter.");
    if (p.builtin.some((r) => r.reason === reason.value)) return addErr.show("That reason code belongs to a built-in rule. Pick another.");
    draft.push({ reason: reason.value, when, message: message.value.trim() || reason.value });
    [cAgent, cMandate, cModel, cTool].forEach((s) => { s.value = ""; });
    reason.value = ""; message.value = "";
    drawRules();
  } }, h("h3.subhead", "Add a rule"), addErr,
  h("div.sentence", "Block when", cAgent, "uses", cModel, "or", cTool, "under", cMandate),
  h("div.help", "A rule with a model blocks model calls; with a tool server, tool calls; with neither, both."),
  h("div.row", field("Reason code", reason), field("Message", message)),
  h("div.form-actions", h("button.btn", { type: "submit" }, icon("plus"), "Add rule")));

  return [
    pageHead({ title: "Policy", badge: h("span.pill.info.plain", p.version) }),
    h("div.grid-2",
      h("div.stack",
        card({ title: "Your rules", flush: true, body: [rulesHost, h("div.card-pad", addForm)] }),
        unsaved),
      h("div.stack",
        card({ title: "Built-in rules", hint: "Always on, checked first", flush: true,
          body: h("div.list", builtin.map((r) => h("div.li", h("span.faint", icon("lock")),
            h("div", h("div.t", BUILTIN_TEXT[r.reason] || r.message), h("div.cs", r.reason)), null))) }),
        card({ title: "History", flush: true, body: p.history.length
          ? h("div.list", p.history.map((v) => h("div.li", h("span", { class: `dot ${v.version === p.number ? "ok" : ""}` }),
            h("div", h("div.t", `Version ${v.version}`, v.version === p.number ? h("span.pill.ok.plain", { style: "margin-left:8px" }, "Active") : null),
              h("div.s", `${v.rule_count} rule${v.rule_count === 1 ? "" : "s"}${v.note ? ` · ${v.note}` : ""}`)),
            h("span.faint.num", when(v.created_at)))))
          : emptyState("No versions saved yet", "Only the built-in rules are active.") }))),
    card({ title: "Cedar policy", hint: `What the gateway evaluates for ${p.version}. Generated from the rules above, read-only.`,
      body: h("pre.cedar", { tabindex: "0", "aria-label": "Cedar policy text" }, p.cedar) }),
  ];
}

// ------------------------------------------------------------------ activity
async function pageActivity() {
  const events = await api("GET", "/admin/audit?limit=300");
  return [pageHead({ title: "Activity" }), card({ flush: true, body: timeline(events) })];
}

// ------------------------------------------------------------------ settings
async function pageSettings() {
  const [s, models, health] = await Promise.all([api("GET", "/admin/settings"), api("GET", "/admin/models"),
    fetch("/health/ready", { cache: "no-store" }).then((r) => r.json()).catch(() => ({}))]);
  // The rate changes rarely and moves every INR budget: a value with an explicit Edit step, not an always-live input.
  const rateBody = h("div.form");
  const rateView = () => rateBody.replaceChildren(
    h("div.big", h("strong", `₹${Number(s.usd_inr_rate).toFixed(2)}`), h("span.muted", "per 1 US dollar"),
      h("button.btn.sm", { type: "button", onclick: rateEdit }, "Edit")),
    h("div.help", s.usd_inr_rate_updated_at ? `Last changed ${when(s.usd_inr_rate_updated_at)}. Used for every INR budget.` : "Used for every INR budget."));
  function rateEdit() {
    const rate = input({ type: "number", min: "1.0001", max: "1000", step: "0.01", value: s.usd_inr_rate, "aria-label": "Rupees per US dollar" });
    const save = h("button.btn.primary", { type: "submit" }, "Save rate");
    const err = formError();
    rateBody.replaceChildren(h("form.form", { onsubmit: async (e) => {
      e.preventDefault();
      err.clear();
      if (!(await ask("Change the exchange rate?", `Every INR budget uses ₹${rate.value} per $1 from the next request.`, "Save rate"))) return;
      await busy(save, async () => {
        try { await api("PUT", "/admin/settings", { usd_inr_rate: rate.value }); toast("Rate saved. INR budgets use it from the next request."); refresh(); }
        catch (ex) { err.show(ex.message); }
      });
    } }, err, h("div.affix", h("span", "₹"), rate), h("div.help", "Rupees for 1 US dollar. Applies to the next request."),
    h("div.form-actions", h("button.btn", { type: "button", onclick: rateView }, "Cancel"), save)));
    rate.focus();
  }
  rateView();
  const ready = (ok, yes, no) => h("span", { class: `pill ${ok ? "ok" : "danger"}` }, ok ? yes : no);
  return [
    pageHead({ title: "Settings" }),
    h("div.grid-2",
      h("div.stack",
        card({ title: "Exchange rate", body: rateBody }),
        card({ title: "Models and prices", hint: "Per 1 million tokens", flush: true,
          body: table([
            { label: "Model", render: (m) => h("div", h("div.mono", m.model), m.provider && h("div.cs", m.provider)) },
            { label: "Input", align: "r", render: (m) => h("span.num", money(m.input_usd_per_1m)) },
            { label: "Output", align: "r", render: (m) => h("span.num", money(m.output_usd_per_1m)) },
            { label: "Max answer", align: "r", render: (m) => h("span.num", `${int(m.max_output_tokens)} tokens`) },
            { label: "Used by", render: (m) => chips(m.mandates, { href: (x) => `#/mandates/${x}` }) },
          ], models, { empty: emptyState("No models", "Add one on the Providers page.") }),
          foot: h("a.link", { href: "#/providers" }, "Add or remove keys and models on Providers") })),
      h("div.stack",
        card({ title: "System", body: h("div.facts",
          h("div.fact", h("div.k", "Database"), h("div.v", ready(health.db, "Ready", "Down"))),
          h("div.fact", h("div.k", "LiteLLM"), h("div.v", ready(health.litellm, "Ready", "Down"))),
          h("div.fact", h("div.k", "Prices"), h("div.v", ready(health.prices, "Loaded", "Missing"))),
          h("div.fact", h("div.k", "Budget month"), h("div.v", s.budget_timezone)),
          h("div.fact", h("div.k", "Calls kept"), h("div.v", `${s.usage_retention_days} days`)),
          h("div.fact", h("div.k", "Log level"), h("div.v", s.log_level))) }),
        card({ title: "This session", body: [h("div.help", "Signed in with the admin token, in this browser tab only."),
          h("div", h("button.btn", { type: "button", onclick: confirmSignOut }, icon("logout"), "Sign out"))] }))),
  ];
}

// ------------------------------------------------------------------ providers (keys + models)
// Keys are write-only: typed once, sent to LiteLLM (which encrypts them); only a masked hint ever comes back, so there
// is no "show key" button anywhere. Removing is refused while something depends on it, so nothing breaks silently.
const MODEL_HINTS = { gemini: "gemini-2.5-flash", openai: "gpt-5-mini", anthropic: "claude-sonnet-4-5", mistral: "mistral-large-latest",
  deepseek: "deepseek-chat", openrouter: "meta-llama/llama-3.3-70b-instruct", openai_compatible: "my-model" };

async function pageProviders() {
  const p = await api("GET", "/admin/providers");
  const removeKey = async (k) => {
    if (!(await ask(`Remove key ${k.name}?`, "LiteLLM deletes it. Models that use it must be removed first.", "Remove key", true))) return;
    try { await api("DELETE", `/admin/providers/${k.name}`); toast("Key removed."); refresh(); } catch (ex) { toast(ex.message, "error"); }
  };
  const removeModel = async (m) => {
    if (!(await ask(`Remove model ${m.name}?`, "Agents can no longer use it. It must not be on any mandate.", "Remove model", true))) return;
    try { await api("DELETE", `/admin/models/${encodeURIComponent(m.name)}`); toast("Model removed."); refresh(); } catch (ex) { toast(ex.message, "error"); }
  };
  const tierChoice = (k) => (k.tier_source === "admin" ? k.tier : "auto");
  const setTier = async (k, tier, sel) => {
    const message = tier === "auto" ? "The gateway goes back to detecting it from the provider's errors." : `It will be tagged ${TIER[tier][0].toLowerCase()} until you change it.`;
    if (!(await ask(`Change the tier for ${k.name}?`, message, "Change tier"))) { sel.value = tierChoice(k); return; }
    try { await api("PUT", `/admin/providers/${k.name}/tier`, { tier }); toast(tier === "auto" ? "Back to automatic detection." : `Tagged as ${TIER[tier][0].toLowerCase()}.`); }
    catch (ex) { toast(ex.message, "error"); }
    refresh();
  };
  const tierCell = (k) => h("div", { style: "display:grid;gap:4px;justify-items:start" }, tierPill(k.tier),
    select([["auto", k.tier_source === "detected" ? `Detected ${when(k.tier_updated_at)}` : "Detect automatically"], ["free", "Set: free tier"], ["paid", "Set: paid"]], tierChoice(k),
      { class: "select sm", "aria-label": `Tier for ${k.name}`, onchange: (e) => setTier(k, e.target.value, e.target) }));
  return [
    pageHead({ title: "Providers",
      actions: [h("button.btn", { type: "button", onclick: () => openAddModel(p) }, icon("plus"), "Add model"),
        h("button.btn.primary", { type: "button", onclick: () => openAddKey(p) }, icon("plus"), "Add key")] }),
    card({ title: "Keys", hint: "Encrypted by LiteLLM; only a masked hint is ever shown", flush: true, body: table([
      { label: "Name", render: (k) => h("div", h("span.ct", k.name), h("div.cs", k.source === "env" ? "from .env" : "added here")) },
      { label: "Provider", render: (k) => h("div", k.label, k.api_base && h("div.cs", k.api_base)) },
      { label: "Key", render: (k) => (k.key_hint ? h("span.code", k.key_hint) : h("span.faint", "in .env")) },
      { label: "Tier", title: "No AI company reports free or paid for a key. The gateway tags a key as free tier when the provider's own rate-limit error says so; you can override it.", render: tierCell },
      { label: "Models", render: (k) => chips(k.models) },
      { label: "", align: "r", render: (k) => (k.source === "console"
        ? h("button.btn.sm.danger", { type: "button", onclick: () => removeKey(k) }, "Remove") : h("span.faint", "In files")) },
    ], p.keys, { empty: emptyState("No keys yet", "Add a key, then add the models it unlocks.") }) }),
    card({ title: "Models", flush: true, body: table([
      { label: "Model", render: (m) => h("div", h("div.mono", m.name), h("div.cs", m.provider_model)) },
      { label: "Key", render: (m) => (m.source === "config" ? h("span.pill.plain", "config file") : h("span.chip", m.provider_key)) },
      { label: "Tier", render: (m) => tierPill(m.tier) },
      { label: "Per 1M tokens", align: "r", render: (m) => (m.priced ? h("span.num", `${money(m.input_usd_per_1m)} in · ${money(m.output_usd_per_1m)} out`) : h("span.pill.warn.plain", "No price")) },
      { label: "Used by", render: (m) => chips(m.mandates, { href: (x) => `#/mandates/${x}` }) },
      { label: "", align: "r", render: (m) => (m.source === "console"
        ? h("button.btn.sm.danger", { type: "button", onclick: () => removeModel(m) }, "Remove")
        : h("span.faint", { title: "Defined in config/litellm.yaml" }, "In files")) },
    ], p.models, { empty: emptyState("No models yet") }) }),
  ];
}

function openAddKey(p) {
  const m = modal({ title: "Add a provider key", sub: "Paste the key from the AI company's website. You can't see it again after saving." });
  const provider = select(p.provider_choices.map((c) => [c.id, c.label]), "gemini");
  const name = input({ class: "mono", value: "gemini-main", maxlength: "64" });
  let nameEdited = false;
  name.addEventListener("input", () => { nameEdited = true; });
  const key = input({ type: "password", autocomplete: "off", class: "mono", placeholder: "Paste the API key" });
  const base = input({ class: "mono", placeholder: "https://my-server.example.com/v1" });
  const baseField = field("Base URL", base, "Only for OpenAI-compatible servers (self-hosted or other companies).");
  const sync = () => {
    baseField.hidden = provider.value !== "openai_compatible";
    if (!nameEdited) name.value = `${provider.value.replace("_", "-")}-main`;
  };
  provider.addEventListener("change", sync);
  sync();
  const err = formError();
  const submit = h("button.btn.primary", { type: "submit", form: "add-key" }, icon("lock"), "Save key");
  append(m.body, h("form.form", { id: "add-key", onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    if (key.value.trim().length < 8) return err.show("Paste the full API key.");
    await busy(submit, async () => {
      try {
        await api("POST", "/admin/providers", { name: name.value, provider: provider.value, api_key: key.value.trim(),
          ...(provider.value === "openai_compatible" ? { api_base: base.value.trim() } : {}) });
        key.value = "";
        m.close();
        toast("Key saved and encrypted. Now add the models it unlocks.");
        refresh();
      } catch (ex) { err.show(ex.message); }
    });
  } }, err, h("div.row", field("Provider", provider), field("Name", name, "Lowercase, numbers and dashes. Used to pick this key for models.")),
  field("API key", key), baseField));
  append(m.foot, h("button.btn", { type: "button", onclick: m.close }, "Cancel"), submit);
}

function openAddModel(p) {
  const m = modal({ title: "Add a model", sub: "Pick which key pays for it and the model's ID at that company." });
  if (!p.keys.length) {
    append(m.body, callout("warn", "Add a provider key first."));
    append(m.foot, h("button.btn.primary", { type: "button", onclick: () => { m.close(); openAddKey(p); } }, "Add key"));
    return;
  }
  const keyOf = Object.fromEntries(p.keys.map((k) => [k.name, k]));
  const key = select(p.keys.map((k) => [k.name, `${k.name} (${k.label})`]), p.keys[0].name);
  const pm = input({ class: "mono" });
  const name = input({ class: "mono", maxlength: "128" });
  let nameEdited = false;
  name.addEventListener("input", () => { nameEdited = true; });
  pm.addEventListener("input", () => { if (!nameEdited) name.value = pm.value.split("/").pop(); });
  const hint = () => { pm.placeholder = `e.g. ${MODEL_HINTS[keyOf[key.value].provider] || "model-id"}`; };
  key.addEventListener("change", hint);
  hint();
  const pin = input({ type: "number", min: "0", step: "0.01", placeholder: "Automatic" });
  const pout = input({ type: "number", min: "0", step: "0.01", placeholder: "Automatic" });
  const err = formError();
  const submit = h("button.btn.primary", { type: "submit", form: "add-model" }, "Add model");
  append(m.body, h("form.form", { id: "add-model", onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    if (!pm.value.trim() || !name.value.trim()) return err.show("Enter the model ID and a name.");
    if (!!pin.value !== !!pout.value) return err.show("Enter both prices, or leave both empty.");
    await busy(submit, async () => {
      try {
        await api("POST", "/admin/models", { name: name.value.trim(), provider_key: key.value, provider_model: pm.value.trim(),
          ...(pin.value ? { input_usd_per_1m: pin.value, output_usd_per_1m: pout.value } : {}) });
        m.close();
        toast("Model added. Put it on a mandate so agents can use it.");
        refresh();
      } catch (ex) { err.show(ex.code === "PRICE_UNKNOWN" ? `${ex.message}.` : ex.message); }
    });
  } }, err, field("Key", key), h("div.row", field("Model ID at the provider", pm, "Exactly as the provider names it."),
    field("Name in the gateway", name, "What agents and mandates use.")),
  h("div.field", h("span.label", "Price per 1 million tokens (USD, optional)"),
    h("div.row", h("div.affix", h("span", "In $"), pin), h("div.affix", h("span", "Out $"), pout)),
    h("div.help", "Leave empty to use LiteLLM's price list. Without a price the gateway can't budget the model."))));
  append(m.foot, h("button.btn", { type: "button", onclick: m.close }, "Cancel"), submit);
}

// ------------------------------------------------------------------ tools (MCP tool servers)
async function pageTools() {
  const [t, mandates] = await Promise.all([api("GET", "/admin/tool-servers"), api("GET", "/admin/mandates")]);
  const names = Object.fromEntries(mandates.map((m) => [m.id, m.name]));
  const act = async (method, path, body, done) => {
    try { await api(method, path, body); toast(done); } catch (ex) { toast(ex.message, "error"); }
    refresh();
  };
  const setStatus = async (s, to) => {
    const confirmed = to === "suspended"
      ? await ask(`Suspend ${s.name}?`, "Every agent's calls to it are blocked and its open streams end now.", "Suspend server", true)
      : await ask(`Set ${s.name} active?`, "Agents on the mandates it's allowed under can use it again.", "Set active");
    if (!confirmed) return false;
    await act("PATCH", `/admin/tool-servers/${s.id}`, { status: to }, to === "active" ? `${s.name} is active again.` : `${s.name} is suspended.`);
  };
  const remove = async (s) => {
    if (!(await ask(`Remove ${s.name}?`, "Agents can no longer reach it. Take it off every mandate first.", "Remove server", true))) return;
    await act("DELETE", `/admin/tool-servers/${s.id}`, undefined, "Tool server removed.");
  };
  const disallow = async (s, g) => {
    if (!(await ask(`Take ${s.name} off ${names[g.mandate_id] || g.mandate_id}?`, "Its agents' calls to this server are blocked and their open streams end now.", "Take off", true))) return;
    await act("DELETE", `/admin/mandates/${g.mandate_id}/tool-servers/${s.id}`, undefined, "Done. Calls under that mandate are blocked.");
  };

  function serverCard(s) {
    const free = mandates.filter((m) => !s.mandates.some((g) => g.mandate_id === m.id));
    const pick = select([["", free.length ? "Choose a mandate…" : "Allowed under every mandate"], ...free.map((m) => [m.id, m.name])], "", { "aria-label": "Mandate" });
    const toolList = input({ class: "mono", placeholder: "All tools (or: get_current_time, search)", "aria-label": "Allowed tools" });
    const allow = async () => {
      if (!pick.value) return;
      const tools = toolList.value.split(",").map((x) => x.trim()).filter(Boolean);
      if (!(await ask(`Allow ${s.name} under ${names[pick.value]}?`, tools.length ? `Its agents may call only: ${tools.join(", ")}.` : "Its agents may call every tool on this server.", "Allow"))) return;
      await act("PUT", `/admin/mandates/${pick.value}/tool-servers/${s.id}`, { tools }, "Allowed. It applies to the next call.");
    };
    const authz = JSON.stringify(s.toolhive.authz_config, null, 2);
    return card({ title: h("span", s.name, " ", statusPill(s.status)), hint: h("span.mono", `${s.id} → ${s.url}`),
      actions: [segmented([["active", "Active", "ok"], ["suspended", "Suspended", "warn"]], s.status, (to) => setStatus(s, to), `Status of ${s.name}`),
        h("button.btn.sm.danger", { type: "button", onclick: () => remove(s) }, "Remove")], flush: true,
      body: [
        s.mandates.length ? h("div.list", s.mandates.map((g) => h("div.li", h("span", { class: "dot ok" }),
          h("div", h("a.t", { href: `#/mandates/${g.mandate_id}` }, names[g.mandate_id] || g.mandate_id),
            h("div.s", g.tools.length ? chips(g.tools) : "Every tool on the server")),
          h("button.btn.sm.ghost", { type: "button", onclick: () => disallow(s, g) }, "Take off"))))
          : emptyState("Not allowed under any mandate", "No agent can reach it until you allow it below."),
        h("div.card-pad", h("div.group", pick, toolList, h("button.btn", { type: "button", disabled: !free.length, onclick: allow }, icon("plus"), "Allow"))),
        h("div.card-pad", h("div.field", h("span.label", "Run it with ToolHive on the gateway's host"),
          copyRow(s.toolhive.command),
          h("div.help", `Save the rules below as ${s.id}-authz.json, put the port you choose in the server's URL, and replace <server> with the MCP server (e.g. fetch, or an image).`)),
          h("div.field", h("span.label", "ToolHive per-tool rules (Cedar)"), h("pre.cedar", { tabindex: "0", "aria-label": `ToolHive rules for ${s.name}` }, authz),
            h("div", copyButton(authz, "Copy rules")))),
      ] });
  }

  return [
    pageHead({ title: "Tools", sub: "MCP tool servers agents reach only through the gateway",
      actions: h("button.btn.primary", { type: "button", onclick: openAddToolServer }, icon("plus"), "Add tool server") }),
    callout("info", "An agent points its MCP client at ", h("span.code", `${location.origin}/m/<mandate>/mcp/<server>`),
      " with its own token. Each message is checked like a model call (agent, mandate, policy, budget); the tool server gets a 60-second pass signed by the gateway, never the agent's token."),
    ...(t.servers.length ? t.servers.map(serverCard) : [card({ body: emptyState("No tool servers yet", "Add one, then allow it under a mandate.") })]),
  ];
}

function openAddToolServer() {
  const m = modal({ title: "Add a tool server", sub: "An MCP endpoint the gateway forwards to, usually a ToolHive proxy on this machine." });
  const name = input({ maxlength: "200", placeholder: "Web fetch" });
  const id = input({ class: "mono", maxlength: "64", placeholder: "fetch" });
  let idEdited = false;
  id.addEventListener("input", () => { idEdited = true; });
  name.addEventListener("input", () => { if (!idEdited) id.value = slugify(name.value); });
  const url = input({ class: "mono", value: "http://host.docker.internal:18111/mcp" });
  const err = formError();
  const submit = h("button.btn.primary", { type: "submit", form: "add-tool-server" }, "Add tool server");
  append(m.body, h("form.form", { id: "add-tool-server", onsubmit: async (e) => {
    e.preventDefault();
    err.clear();
    if (!name.value.trim() || !id.value) return err.show("Enter a name and an id.");
    await busy(submit, async () => {
      try {
        await api("POST", "/admin/tool-servers", { id: id.value, name: name.value.trim(), url: url.value.trim() });
        m.close();
        toast("Tool server added. Allow it under a mandate so agents can use it.");
        refresh();
      } catch (ex) { err.show(ex.message); }
    });
  } }, err, h("div.row", field("Name", name), field("Id", id, "Lowercase, numbers and dashes. Part of the agents' URL.")),
  field("MCP URL", url, "Where the gateway reaches it. host.docker.internal is this machine as seen from the gateway's container.")));
  append(m.foot, h("button.btn", { type: "button", onclick: m.close }, "Cancel"), submit);
}

// ------------------------------------------------------------------ boot
window.addEventListener("hashchange", () => render());
window.addEventListener("resize", moveIndicator);
render();
