"use strict";
// Admin panel for the Meetings API. Plain JS, served from the API's own origin at /panel.
// All user content is inserted as text (never as HTML).

const store = {
  get access() { return sessionStorage.getItem("access"); },
  get refresh() { return sessionStorage.getItem("refresh"); },
  set(tokens) {
    sessionStorage.setItem("access", tokens.access_token);
    sessionStorage.setItem("refresh", tokens.refresh_token);
    sessionStorage.setItem("me", JSON.stringify(tokens.user));
  },
  get me() { return JSON.parse(sessionStorage.getItem("me") || "null"); },
  clear() { sessionStorage.clear(); },
};

// ------------------------------------------------------------------ helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}
const text = (value) => h("span", { dir: "auto" }, value ?? "—");
const fmtNum = (n, digits = 0) => (n === null || n === undefined ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: digits }));
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const fmtDay = (iso) => (iso ? new Date(iso).toLocaleDateString() : "—");
const fmtDuration = (s) => {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(sec).padStart(2, "0")}`;
};
function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3500);
}
function statusBadge(status) {
  const cls = { completed: "good", failed: "critical" }[status] || "accent";
  return h("span", { class: `badge ${cls}` }, status);
}

// --------------------------------------------------------------------- API

class ApiError extends Error {}

async function api(path, { method = "GET", body, raw = false, retry = true } = {}) {
  const headers = {};
  if (store.access) headers.Authorization = `Bearer ${store.access}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401 && retry && store.refresh) {
    if (await refreshTokens()) return api(path, { method, body, raw, retry: false });
  }
  if (res.status === 401) { signOut(); throw new ApiError("Your session has ended. Sign in again."); }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map((d) => `${d.loc.slice(-1)[0]}: ${d.msg}`).join("; ") : detail;
    } catch { /* not JSON */ }
    throw new ApiError(detail);
  }
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

async function refreshTokens() {
  const res = await fetch("/auth/refresh", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: store.refresh }),
  });
  if (!res.ok) return false;
  store.set(await res.json());
  return true;
}

function signOut() {
  const refresh = store.refresh;
  if (refresh) fetch("/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: refresh }) });
  store.clear();
  showLogin();
}

async function download(path, fallbackName) {
  const res = await api(path, { raw: true });
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
  const name = match ? decodeURIComponent(match[1]) : fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ------------------------------------------------------------------- shell

function showLogin() {
  document.getElementById("shell").hidden = true;
  document.getElementById("login").hidden = false;
}

function showShell() {
  document.getElementById("login").hidden = true;
  document.getElementById("shell").hidden = false;
  document.getElementById("me").textContent = store.me?.email || "";
  route();
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const error = document.getElementById("login-error");
  error.textContent = "";
  const res = await fetch("/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: form.email.value, password: form.password.value }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { error.textContent = data.detail || "Sign-in failed"; return; }
  if (data.user.role !== "admin") {
    error.textContent = "This account is not an admin.";
    fetch("/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: data.refresh_token }) });
    return;
  }
  store.set(data);
  form.reset();
  if (!location.hash) location.hash = "#/dashboard";
  showShell();
});
document.getElementById("logout").addEventListener("click", signOut);

const routes = [
  [/^#\/dashboard$/, viewDashboard, "dashboard"],
  [/^#\/users$/, viewUsers, "users"],
  [/^#\/users\/([\w-]+)$/, viewUser, "users"],
  [/^#\/meetings$/, viewMeetings, "meetings"],
  [/^#\/meetings\/([\w-]+)$/, viewMeeting, "meetings"],
  [/^#\/settings$/, viewSettings, "settings"],
  [/^#\/audit$/, viewAudit, "audit"],
];

async function route() {
  if (!store.access) return showLogin();
  const [hash, query = ""] = (location.hash || "#/dashboard").split("?");
  const found = routes.find(([re]) => re.test(hash));
  if (!found) { location.hash = "#/dashboard"; return; }
  const [re, view, nav] = found;
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  const main = document.getElementById("view");
  main.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const content = await view(new URLSearchParams(query), ...hash.match(re).slice(1));
    main.replaceChildren(content);
  } catch (err) {
    main.replaceChildren(h("div", { class: "card" }, h("p", { class: "error" }, err.message)));
  }
  main.focus();
}
window.addEventListener("hashchange", route);

function setQuery(base, params) {
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined));
  const qs = new URLSearchParams(clean).toString();
  location.hash = qs ? `${base}?${qs}` : base;
}

function pager(page, base, params) {
  const { total, limit, offset } = page;
  return h("div", { class: "pager" },
    h("span", { class: "muted" }, total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : "No results"),
    h("button", { class: "secondary", disabled: offset === 0, onclick: () => setQuery(base, { ...params, offset: Math.max(0, offset - limit) }) }, "Previous"),
    h("button", { class: "secondary", disabled: offset + limit >= total, onclick: () => setQuery(base, { ...params, offset: offset + limit }) }, "Next"),
  );
}

// --------------------------------------------------------------- dashboard

function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
}

function dailyChart(points, key, unit) {
  const W = 760, H = 220, P = { l: 40, r: 8, t: 10, b: 24 };
  const max = Math.max(1, ...points.map((p) => p[key]));
  const step = (W - P.l - P.r) / points.length;
  const barW = Math.max(2, Math.min(28, step - 2));
  const y = (v) => H - P.b - (v / max) * (H - P.t - P.b);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Daily ${key.replace("_", " ")}`);
  const add = (tag, attrs, parent = svg) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    parent.append(el);
    return el;
  };
  const axis = add("g", { class: "axis" });
  for (const frac of [0, 0.5, 1]) {
    const v = max * frac, yy = y(v);
    add("line", { class: "gridline", x1: P.l, x2: W - P.r, y1: yy, y2: yy });
    const t = add("text", { x: P.l - 6, y: yy + 4, "text-anchor": "end" }, axis);
    t.textContent = fmtNum(v, 1);
  }
  const tip = document.getElementById("tooltip");
  const labelEvery = Math.ceil(points.length / 8);
  points.forEach((p, i) => {
    const x = P.l + i * step + (step - barW) / 2;
    const v = p[key];
    const top = y(v), height = H - P.b - top;
    const r = Math.min(4, barW / 2, height);
    // Bar with a rounded data end, square on the baseline.
    const d = height <= 0 ? "" : `M${x},${H - P.b}V${top + r}Q${x},${top} ${x + r},${top}H${x + barW - r}Q${x + barW},${top} ${x + barW},${top + r}V${H - P.b}Z`;
    if (d) add("path", { class: "bar", d });
    const hit = add("rect", { x: P.l + i * step, y: P.t, width: step, height: H - P.t - P.b, fill: "transparent" });
    hit.addEventListener("mousemove", (e) => {
      tip.textContent = `${fmtDay(p.date)}: ${fmtNum(v, 1)} ${unit}`;
      tip.hidden = false;
      tip.style.left = `${e.clientX + 12}px`;
      tip.style.top = `${e.clientY - 28}px`;
    });
    hit.addEventListener("mouseleave", () => (tip.hidden = true));
    if (i % labelEvery === 0) {
      const t = add("text", { x: x + barW / 2, y: H - 6, "text-anchor": "middle" }, axis);
      t.textContent = new Date(p.date).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
  });
  return h("div", { class: "chart" }, svg);
}

function hbars(items, emptyText) {
  if (!items.length) return h("p", { class: "muted" }, emptyText);
  const max = Math.max(...items.map((i) => i.count));
  return h("div", { class: "hbars" }, items.map((item) =>
    h("div", { class: "hbar", title: `${item.label}: ${item.count}` },
      h("span", { class: "name", dir: "auto" }, item.label),
      h("span", { class: "track" }, h("span", { class: "fill", style: `display:block;width:${(item.count / max) * 100}%` })),
      h("span", { class: "count" }, fmtNum(item.count)))));
}

async function viewDashboard(params) {
  const days = params.get("days") || "30";
  const d = await api(`/admin/dashboard?days=${encodeURIComponent(days)}`);
  const u = d.usage;
  const metric = params.get("metric") || "uploads";
  const metrics = { uploads: "uploads", audio_minutes: "minutes", new_users: "sign-ups", failed_jobs: "failed jobs" };
  return h("div", {},
    h("div", { class: "toolbar" },
      h("h1", { style: "margin:0" }, "Dashboard"), h("span", { class: "spacer" }),
      h("label", {}, "Period ",
        h("select", { onchange: (e) => setQuery("#/dashboard", { days: e.target.value, metric }) },
          ["7", "30", "90", "365"].map((v) => h("option", { value: v, selected: v === days }, `Last ${v} days`))))),
    h("h3", {}, "Users"),
    h("div", { class: "kpis" },
      kpi("Users", fmtNum(d.users.total), `${fmtNum(d.users.new)} new`),
      kpi("Active", fmtNum(d.users.active), "used the app in the period"),
      kpi("Disabled", fmtNum(d.users.disabled)),
      kpi("Admins", fmtNum(d.users.admins))),
    h("h3", {}, "Usage"),
    h("div", { class: "kpis" },
      kpi("Uploads", fmtNum(u.uploads), `${fmtNum(u.summaries_regenerated)} summaries regenerated`),
      kpi("Audio transcribed", `${fmtNum(u.audio_minutes, 1)} min`, u.average_recording_minutes !== null ? `avg ${fmtNum(u.average_recording_minutes, 1)} min / recording` : null),
      kpi("Failure rate", u.failure_rate === null ? "—" : `${fmtNum(u.failure_rate * 100, 1)}%`, `${fmtNum(u.failed_jobs)} of ${fmtNum(u.jobs)} jobs`),
      kpi("Avg processing", u.average_processing_seconds === null ? "—" : `${fmtNum(u.average_processing_seconds, 0)} s`),
      kpi("LLM tokens", fmtNum(u.llm_prompt_tokens + u.llm_completion_tokens), `${fmtNum(u.llm_prompt_tokens)} in · ${fmtNum(u.llm_completion_tokens)} out`),
      kpi("Estimated cost", u.estimated_cost_usd === null ? "—" : `$${fmtNum(u.estimated_cost_usd, 2)}`,
        u.estimated_cost_usd === null ? "set prices in Settings" : `speech $${fmtNum(u.estimated_transcription_cost_usd ?? 0, 2)} · LLM $${fmtNum(u.estimated_llm_cost_usd ?? 0, 2)}`)),
    h("h3", {}, "Meetings"),
    h("div", { class: "kpis" },
      kpi("Meetings", fmtNum(d.meetings.total), `${fmtNum(d.meetings.created)} created in the period`),
      kpi("Completed", fmtNum(d.meetings.completed)),
      kpi("Failed", fmtNum(d.meetings.failed)),
      kpi("Processing", fmtNum(d.meetings.processing))),
    h("div", { class: "card", style: "margin-bottom:16px" },
      h("div", { class: "row" }, h("h2", { style: "margin:0" }, "Per day"), h("span", { class: "spacer" }),
        h("select", { style: "width:auto", "aria-label": "Metric", onchange: (e) => setQuery("#/dashboard", { days, metric: e.target.value }) },
          Object.entries(metrics).map(([k, label]) => h("option", { value: k, selected: k === metric }, label)))),
      dailyChart(d.daily, metric, metrics[metric])),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "What it's used for: categories"), hbars(d.top_categories, "No meetings in this period.")),
      h("div", { class: "card" }, h("h2", {}, "Topics in the summaries"), hbars(d.top_topics, "No summaries in this period.")),
      h("div", { class: "card" }, h("h2", {}, "Languages"), hbars(d.languages, "No summaries in this period.")),
      h("div", { class: "card" }, h("h2", {}, "Heaviest users"),
        d.top_users.length ? h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "User"), h("th", { class: "num" }, "Uploads"), h("th", { class: "num" }, "Minutes"))),
          h("tbody", {}, d.top_users.map((t) => h("tr", { class: "clickable", onclick: () => (location.hash = `#/users/${t.user_id}`) },
            h("td", {}, t.email), h("td", { class: "num" }, fmtNum(t.uploads)), h("td", { class: "num" }, fmtNum(t.audio_minutes, 1))))))
          : h("p", { class: "muted" }, "No uploads in this period."))));
}

// ------------------------------------------------------------------- users

async function viewUsers(params) {
  const q = params.get("q") || "", role = params.get("role") || "", active = params.get("is_active") || "";
  const offset = Number(params.get("offset") || 0);
  const qs = new URLSearchParams({ limit: 50, offset });
  if (q) qs.set("q", q);
  if (role) qs.set("role", role);
  if (active) qs.set("is_active", active);
  const page = await api(`/admin/users?${qs}`);
  const filters = { q, role, is_active: active };
  const form = h("form", { class: "toolbar", onsubmit: (e) => {
    e.preventDefault();
    const f = e.target;
    setQuery("#/users", { q: f.q.value.trim(), role: f.role.value, is_active: f.is_active.value });
  } },
    h("input", { type: "search", name: "q", placeholder: "Search email or name", value: q }),
    h("select", { name: "role" }, h("option", { value: "" }, "All roles"), ["user", "admin"].map((r) => h("option", { value: r, selected: r === role }, r))),
    h("select", { name: "is_active" }, h("option", { value: "" }, "Any status"),
      h("option", { value: "true", selected: active === "true" }, "Active"), h("option", { value: "false", selected: active === "false" }, "Disabled")),
    h("button", { type: "submit" }, "Search"));
  return h("div", {}, h("h1", {}, "Users"), form,
    h("div", { class: "card table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {}, ["Email", "Name", "Role", "Status", "Meetings", "Uploads (month)", "Minutes (month)", "Last active"].map((c, i) => h("th", { class: i >= 4 && i <= 6 ? "num" : null }, c)))),
        h("tbody", {}, page.items.map((u) => h("tr", { class: "clickable", onclick: () => (location.hash = `#/users/${u.id}`) },
          h("td", {}, u.email), h("td", {}, text(u.full_name)), h("td", {}, u.role === "admin" ? h("span", { class: "badge accent" }, "admin") : "user"),
          h("td", {}, u.is_active ? h("span", { class: "badge good" }, "active") : h("span", { class: "badge critical" }, "disabled")),
          h("td", { class: "num" }, fmtNum(u.meeting_count)),
          h("td", { class: "num" }, `${fmtNum(u.uploads_this_month)}${u.quota_uploads_per_month ? ` / ${u.quota_uploads_per_month}` : ""}`),
          h("td", { class: "num" }, fmtNum(u.audio_minutes_this_month, 1)),
          h("td", {}, fmtDate(u.last_active_at)))))),
      pager(page, "#/users", filters)));
}

async function viewUser(_params, id) {
  const u = await api(`/admin/users/${id}`);
  const settings = await api("/admin/settings");
  const save = async (body, message) => {
    try { await api(`/admin/users/${id}`, { method: "PATCH", body }); toast(message); route(); }
    catch (err) { toast(err.message); }
  };
  const quotaInput = (name, value, globalValue) => h("label", {},
    name === "quota_uploads_per_month" ? "Uploads per month" : "Audio minutes per month",
    h("input", { type: "number", min: 1, name, value: value ?? "", placeholder: globalValue ? `default: ${globalValue}` : "default: unlimited" }));

  const access = u.is_active
    ? h("form", { class: "grid", onsubmit: (e) => {
        e.preventDefault();
        if (!confirm(`Revoke access for ${u.email}? They are signed out everywhere immediately.`)) return;
        save({ is_active: false, disabled_reason: e.target.reason.value.trim() || null }, "Access revoked");
      } },
        h("label", {}, "Reason (shown to the user at login)", h("input", { name: "reason", maxlength: 1000, placeholder: "e.g. Recording people without their consent" })),
        h("div", {}, h("button", { class: "danger", type: "submit" }, "Revoke access")))
    : h("div", { class: "grid" },
        h("div", { class: "notice" }, `Access revoked ${fmtDate(u.disabled_at)}`, u.disabled_reason ? h("div", { dir: "auto" }, `Reason: ${u.disabled_reason}`) : null),
        h("div", {}, h("button", { onclick: () => save({ is_active: true }, "Access restored") }, "Restore access")));

  return h("div", {},
    h("p", {}, h("a", { href: "#/users" }, "← Users")),
    h("h1", {}, u.email),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Account"),
        h("dl", { class: "props" },
          h("dt", {}, "Name"), h("dd", {}, text(u.full_name)),
          h("dt", {}, "Role"), h("dd", {}, u.role),
          h("dt", {}, "Status"), h("dd", {}, u.is_active ? "active" : "disabled"),
          h("dt", {}, "Registered"), h("dd", {}, fmtDate(u.created_at)),
          h("dt", {}, "Last sign-in"), h("dd", {}, fmtDate(u.last_login_at)),
          h("dt", {}, "Last active"), h("dd", {}, fmtDate(u.last_active_at)),
          h("dt", {}, "Meetings"), h("dd", {}, h("a", { href: `#/meetings?user_id=${u.id}` }, `${fmtNum(u.meeting_count)} — view`)),
          h("dt", {}, "This month"), h("dd", {}, `${fmtNum(u.uploads_this_month)} uploads, ${fmtNum(u.audio_minutes_this_month, 1)} min`)),
        h("h3", {}, "Role"),
        u.role === "admin"
          ? h("button", { class: "secondary", onclick: () => confirm("Remove admin rights?") && save({ role: "user" }, "Admin rights removed") }, "Make regular user")
          : h("button", { class: "secondary", onclick: () => confirm(`Make ${u.email} an admin?`) && save({ role: "admin" }, "Promoted to admin") }, "Make admin")),
      h("div", { class: "grid" },
        h("div", { class: "card" }, h("h2", {}, "Access"), access),
        h("form", { class: "card grid", onsubmit: (e) => {
          e.preventDefault();
          const f = e.target;
          const num = (v) => (v.trim() === "" ? null : Number(v));
          save({ quota_uploads_per_month: num(f.quota_uploads_per_month.value), quota_audio_minutes_per_month: num(f.quota_audio_minutes_per_month.value) }, "Quotas saved");
        } },
          h("h2", { style: "margin:0" }, "Monthly quotas"),
          h("p", { class: "hint" }, "Leave empty to use the global default from Settings."),
          h("div", { class: "fieldset" },
            quotaInput("quota_uploads_per_month", u.quota_uploads_per_month, settings.quota_uploads_per_month),
            quotaInput("quota_audio_minutes_per_month", u.quota_audio_minutes_per_month, settings.quota_audio_minutes_per_month)),
          h("div", {}, h("button", { type: "submit" }, "Save quotas"))))));
}

// ---------------------------------------------------------------- meetings

async function viewMeetings(params) {
  const fields = ["q", "status", "user_id", "date_from", "date_to"];
  const values = Object.fromEntries(fields.map((f) => [f, params.get(f) || ""]));
  const offset = Number(params.get("offset") || 0);
  const qs = new URLSearchParams({ limit: 25, offset });
  for (const f of fields) {
    if (!values[f]) continue;
    qs.set(f, f.startsWith("date_") ? new Date(values[f] + (f === "date_to" ? "T23:59:59" : "T00:00:00")).toISOString() : values[f]);
  }
  const page = await api(`/admin/meetings?${qs}`);
  const form = h("form", { class: "toolbar", onsubmit: (e) => {
    e.preventDefault();
    const f = e.target;
    setQuery("#/meetings", { ...Object.fromEntries(fields.map((n) => [n, f[n].value.trim()])) });
  } },
    h("input", { type: "search", name: "q", placeholder: "Search titles and transcripts", value: values.q }),
    h("select", { name: "status" }, h("option", { value: "" }, "Any status"),
      ["queued", "transcribing", "summarizing", "completed", "failed"].map((s) => h("option", { value: s, selected: s === values.status }, s))),
    h("input", { type: "date", name: "date_from", value: values.date_from, "aria-label": "From" }),
    h("input", { type: "date", name: "date_to", value: values.date_to, "aria-label": "To" }),
    h("input", { type: "hidden", name: "user_id", value: values.user_id }),
    h("button", { type: "submit" }, "Search"),
    values.user_id ? h("button", { type: "button", class: "secondary", onclick: () => setQuery("#/meetings", { ...values, user_id: "" }) }, "All users") : null);
  return h("div", {}, h("h1", {}, "Meetings"),
    h("p", { class: "hint" }, "Listing and opening meetings is recorded in the audit log."),
    form,
    h("div", { class: "card table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {}, ["Title", "Owner", "Date", "Duration", "Status", "Category"].map((c) => h("th", {}, c)))),
        h("tbody", {}, page.items.map((m) => h("tr", { class: "clickable", onclick: () => (location.hash = `#/meetings/${m.id}`) },
          h("td", {}, text(m.title || "(untitled)")), h("td", {}, m.owner.email, m.owner.is_active ? null : h("span", { class: "badge critical", style: "margin-inline-start:6px" }, "disabled")),
          h("td", {}, fmtDate(m.meeting_date)), h("td", {}, fmtDuration(m.duration_seconds)), h("td", {}, statusBadge(m.status)),
          h("td", {}, text(m.category?.name)))))),
      pager(page, "#/meetings", values)));
}

function section(title, items) {
  if (!items || !items.length) return null;
  return h("div", {}, h("h3", {}, title), h("ul", { class: "plain", dir: "auto" }, items.map((i) => h("li", {}, i))));
}

async function viewMeeting(_params, id) {
  const m = await api(`/admin/meetings/${id}`);
  const s = m.summary;
  const remove = async () => {
    if (!confirm(`Delete "${m.title || "this meeting"}" by ${m.owner.email}? This cannot be undone.`)) return;
    try { await api(`/admin/meetings/${id}`, { method: "DELETE" }); toast("Meeting deleted"); location.hash = "#/meetings"; }
    catch (err) { toast(err.message); }
  };
  const exportAs = (format) => download(`/admin/meetings/${id}/export?format=${format}&include_transcript=true`, `meeting.${format}`).catch((err) => toast(err.message));
  return h("div", {},
    h("p", {}, h("a", { href: "#/meetings" }, "← Meetings")),
    h("div", { class: "row" }, h("h1", { dir: "auto", style: "margin:0" }, m.title || "(untitled)"), statusBadge(m.status), h("span", { class: "spacer" }),
      h("button", { class: "secondary", onclick: () => exportAs("pdf") }, "Export PDF"),
      h("button", { class: "secondary", onclick: () => exportAs("docx") }, "Export Word"),
      h("button", { class: "danger", onclick: remove }, "Delete")),
    m.error_message ? h("div", { class: "notice", style: "margin-top:12px" }, m.error_message) : null,
    h("div", { class: "grid cols-2", style: "margin-top:16px" },
      h("div", { class: "card" }, h("h2", {}, "Details"),
        h("dl", { class: "props" },
          h("dt", {}, "Owner"), h("dd", {}, h("a", { href: `#/users/${m.owner.id}` }, m.owner.email)),
          h("dt", {}, "Date"), h("dd", {}, fmtDate(m.meeting_date)),
          h("dt", {}, "Duration"), h("dd", {}, fmtDuration(m.duration_seconds)),
          h("dt", {}, "Category"), h("dd", {}, text(m.category?.name)),
          h("dt", {}, "Attendees"), h("dd", { dir: "auto" }, m.attendees.length ? m.attendees.map((a) => (a.role ? `${a.name} (${a.role})` : a.name)).join(", ") : "—"),
          h("dt", {}, "File"), h("dd", {}, text(m.source_filename)),
          h("dt", {}, "Uploaded"), h("dd", {}, fmtDate(m.created_at)))),
      h("div", { class: "card" }, h("h2", {}, "Action items"),
        m.action_items.length ? h("ul", { class: "plain", dir: "auto" }, m.action_items.map((a) => h("li", {},
          a.is_done ? "✓ " : "", a.description,
          a.assignee ? h("span", { class: "muted" }, ` — ${a.assignee}`) : null,
          a.due_date || a.due_text ? h("span", { class: "muted" }, ` (${a.due_date || a.due_text})`) : null)))
          : h("p", { class: "muted" }, "None"))),
    h("div", { class: "card", style: "margin-top:16px" }, h("h2", {}, "Summary"),
      s ? h("div", { dir: "auto" },
        s.overview ? h("p", {}, s.overview) : null,
        section("What happened", s.what_happened), section("Decisions", s.decisions),
        section("Parties", (s.parties || []).map((p) => (p.role ? `${p.name}: ${p.role}` : p.name))),
        section("Open questions", s.open_questions),
        s.topics?.length ? h("p", { class: "muted" }, `Topics: ${s.topics.join(", ")}`) : null)
        : h("p", { class: "muted" }, "No summary yet.")),
    h("div", { class: "card", style: "margin-top:16px" }, h("h2", {}, "Transcript"),
      m.transcript ? h("div", { class: "transcript", dir: "auto" }, m.transcript) : h("p", { class: "muted" }, "No transcript.")));
}

// ---------------------------------------------------------------- settings

const SETTINGS_GROUPS = [
  ["Speech-to-text", [
    ["transcription_base_url", "Base URL", "text"],
    ["transcription_api_key", "API key", "secret"],
    ["transcription_model", "Model", "text"],
    ["transcription_language", "Default language (ISO code or \"auto\")", "text"],
    ["transcription_chunk_seconds", "Chunk length (seconds)", "number"],
  ]],
  ["Summaries (LLM)", [
    ["llm_base_url", "Base URL", "text"],
    ["llm_api_key", "API key", "secret"],
    ["llm_model", "Model", "text"],
    ["llm_temperature", "Temperature", "number", "0.1"],
    ["llm_max_tokens", "Max output tokens", "number"],
    ["summary_max_transcript_chars", "Condense transcripts longer than (characters)", "number"],
  ]],
  ["Limits and quotas", [
    ["max_upload_mb", "Max upload size (MB, up to 32)", "number"],
    ["max_audio_minutes", "Max recording length (minutes)", "number"],
    ["quota_uploads_per_month", "Uploads per user per month (empty = unlimited)", "optional"],
    ["quota_audio_minutes_per_month", "Audio minutes per user per month (empty = unlimited)", "optional"],
  ]],
  ["Prices for cost estimates (USD, empty = not shown)", [
    ["price_transcription_per_hour_usd", "Speech-to-text per audio hour", "optional", "0.0001"],
    ["price_llm_input_per_million_tokens_usd", "LLM input per million tokens", "optional", "0.0001"],
    ["price_llm_output_per_million_tokens_usd", "LLM output per million tokens", "optional", "0.0001"],
  ]],
];

// One click fills the provider fields; paste that provider's key and save.
const LLM_PRESETS = [
  ["Google Gemini (free tier)", { llm_base_url: "https://generativelanguage.googleapis.com/v1beta/openai/", llm_model: "gemini-flash-latest", llm_max_tokens: 32000 }],
  ["DeepSeek (paid)", { llm_base_url: "https://api.deepseek.com", llm_model: "deepseek-chat", llm_max_tokens: 8000 }],
];

function presetRow(presets) {
  return h("div", { class: "row", style: "margin-bottom:12px" }, h("span", { class: "hint" }, "Provider:"),
    presets.map(([label, values]) => h("button", { type: "button", class: "secondary", onclick: (e) => {
      const form = e.target.closest("form");
      for (const [name, value] of Object.entries(values)) form.elements[name].value = value;
      toast(`${label}: paste its API key, then Save changes`);
    } }, label)));
}

async function viewSettings() {
  const current = await api("/admin/settings");
  const input = (name, label, kind, step) => {
    if (kind === "secret") {
      const status = current[name];
      return h("label", {}, label,
        h("input", { type: "password", name, autocomplete: "off", placeholder: status.configured ? `set (${status.hint || "hidden"}) — type to replace` : "not set" }),
        status.configured ? h("label", { class: "row hint" }, h("input", { type: "checkbox", name: `${name}__remove`, style: "width:auto" }), "Remove this key") : null);
    }
    const value = current[name] ?? "";
    return h("label", {}, label, h("input", { type: kind === "text" ? "text" : "number", name, value, step: step || (kind === "text" ? null : "1"), dir: kind === "text" ? "auto" : null }));
  };
  const form = h("form", { class: "grid", onsubmit: async (e) => {
    e.preventDefault();
    const f = e.target;
    const changes = {};
    for (const [, items] of SETTINGS_GROUPS) for (const [name, , kind] of items) {
      const el = f.elements[name];
      if (kind === "secret") {
        if (f.elements[`${name}__remove`]?.checked) changes[name] = "";
        else if (el.value.trim()) changes[name] = el.value.trim();
        continue;
      }
      const raw = el.value.trim();
      const value = kind === "text" ? raw : raw === "" ? null : Number(raw);
      if (value !== (current[name] ?? (kind === "text" ? "" : null))) changes[name] = kind === "number" && value === null ? null : value;
    }
    for (const name of ["summary_prompt", "notes_prompt"]) if (f.elements[name].value !== current[name]) changes[name] = f.elements[name].value;
    if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
    try { await api("/admin/settings", { method: "PATCH", body: changes }); toast("Settings saved"); route(); }
    catch (err) { toast(err.message); }
  } },
    SETTINGS_GROUPS.map(([title, items]) => h("div", { class: "card" }, h("h2", {}, title),
      title === "Summaries (LLM)" ? presetRow(LLM_PRESETS) : null,
      h("div", { class: "fieldset" }, items.map(([name, label, kind, step]) => input(name, label, kind, step))))),
    h("div", { class: "card" }, h("h2", {}, "Summary format (system prompt)"),
      h("p", { class: "hint" }, "Sent before every transcript. It must ask for a json object with the keys the app expects. Existing meetings pick up a change when their summary is regenerated."),
      h("textarea", { name: "summary_prompt", dir: "auto", value: current.summary_prompt, rows: 18 }),
      h("h3", {}, "Notes prompt (condenses very long transcripts)"),
      h("textarea", { name: "notes_prompt", dir: "auto", value: current.notes_prompt, rows: 8 })),
    h("div", {}, h("button", { type: "submit" }, "Save changes")));
  return h("div", {}, h("h1", {}, "Settings"),
    h("p", { class: "hint" }, "Changes apply to the next upload. API keys are stored encrypted and never shown."), form);
}

// --------------------------------------------------------------- audit log

function auditDetails(entry) {
  const d = entry.details || {};
  switch (entry.action) {
    case "meeting.list": return `filters ${JSON.stringify(d.filters || {})}, ${(d.returned || []).length} shown`;
    case "settings.update": return `changed ${(d.fields || []).join(", ")}`;
    default: return Object.entries(d).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
  }
}

async function viewAudit(params) {
  const action = params.get("action") || "";
  const offset = Number(params.get("offset") || 0);
  const qs = new URLSearchParams({ limit: 50, offset });
  if (action) qs.set("action", action);
  const page = await api(`/admin/audit-log?${qs}`);
  const actions = ["meeting.list", "meeting.view", "meeting.export", "meeting.delete", "user.update", "settings.update"];
  const targetLink = (e) => (e.target_type === "meeting" ? h("a", { href: `#/meetings/${e.target_id}` }, "meeting")
    : e.target_type === "user" ? h("a", { href: `#/users/${e.target_id}` }, "user") : "—");
  return h("div", {}, h("h1", {}, "Audit log"),
    h("div", { class: "toolbar" },
      h("select", { onchange: (e) => setQuery("#/audit", { action: e.target.value }) },
        h("option", { value: "" }, "All actions"), actions.map((a) => h("option", { value: a, selected: a === action }, a)))),
    h("div", { class: "card table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {}, ["When", "Admin", "Action", "Target", "Details"].map((c) => h("th", {}, c)))),
        h("tbody", {}, page.items.map((e) => h("tr", {},
          h("td", {}, fmtDate(e.created_at)), h("td", {}, e.admin_email), h("td", {}, e.action),
          h("td", {}, targetLink(e)), h("td", { dir: "auto" }, auditDetails(e)))))),
      pager(page, "#/audit", { action })));
}

// -------------------------------------------------------------------- boot

if (store.access && store.me?.role === "admin") showShell();
else showLogin();
