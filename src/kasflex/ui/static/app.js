"use strict";

/* KasFlex Decision Cockpit.
 *
 * The page deliberately has no framework or build step. Its safety state is small
 * enough to audit in one file, and the server remains the authority: approval is
 * accepted only with a one-use token tied to the exact verified plan and scenario.
 */

const $ = (id) => document.getElementById(id);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const clone = (value) => JSON.parse(JSON.stringify(value));

const api = async (path, body) => {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({
    error: `${response.status} ${response.statusText}`,
  }));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
};

const HEAT = ["boiler", "chp", "buffer", "none"];
const BATT = ["idle", "charge", "discharge"];
const CHP = ["off", "heat_led", "max_export"];
const CO2 = ["none", "chp", "liquid"];
const CONDITIONS = ["rule-based", "learned", "ai-unverified", "ai-verified", "mpc"];

const LABELS = {
  "rule-based": "Rule baseline",
  learned: "Learned scheduler",
  naive: "Fixture planner",
  llm: "Language model",
  mpc: "MPC reference",
  "ai-unverified": "AI · unverified",
  "ai-verified": "AI · verified",
  heat_led: "Heat led",
  max_export: "Max export",
  chp: "CHP",
  co2: "CO₂",
};

const state = {
  fields: [],
  defaults: {},
  settingsPayload: null,
  result: null,
  plan: [],
  originalPlan: [],
  editedHours: new Set(),
  approvalToken: null,
  decisionToken: null,
  dirty: false,
  stale: false,
  decided: false,
  shownAt: null,
  settingsReady: false,
  settingsRevision: 0,
  requestPending: false,
  requestSerial: 0,
  activeRequest: null,
  drawerReturnFocus: null,
};

const finite = (value, fallback = 0) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
};
const titleCase = (value) => LABELS[value] || String(value)
  .replace(/[_-]/g, " ")
  .replace(/\b\w/g, (character) => character.toUpperCase());
const hourLabel = (hour) => `${String(hour).padStart(2, "0")}:00`;
const eur = (value, digits = 0) => new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "EUR",
  minimumFractionDigits: digits,
  maximumFractionDigits: digits,
}).format(finite(value));
const num = (value, digits = 0) => new Intl.NumberFormat("en-GB", {
  minimumFractionDigits: digits,
  maximumFractionDigits: digits,
}).format(finite(value));
const percent = (value, digits = 0) => `${num(finite(value) * 100, digits)}%`;
const formatArea = (value) => finite(value) >= 10_000
  ? `${num(finite(value) / 10_000, 1)} ha`
  : `${num(value)} m²`;

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value;
}

function clearChildren(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------------------------------------------------------------- settings */

const GROUPS = [
  {
    id: "planning",
    title: "Planning intent",
    icon: "P",
    open: true,
    paths: ["planner", "date", "winter", "brief"],
  },
  {
    id: "grid",
    title: "Grid contract",
    icon: "G",
    open: true,
    prefix: "hub.contract.",
  },
  {
    id: "assets",
    title: "Energy assets",
    icon: "E",
    prefixes: ["hub.battery.", "hub.chp.", "hub.buffer."],
  },
  {
    id: "crop",
    title: "Crop & scale",
    icon: "C",
    paths: ["hub.crop.dli_target_mol_m2", "hub.floor_area_m2"],
  },
  {
    id: "research",
    title: "Research controls",
    icon: "R",
    advanced: true,
    paths: ["checker.enabled", "checker.explain", "checker.max_revisions", "seed"],
  },
];

function groupFor(path) {
  return GROUPS.find((group) =>
    group.paths?.includes(path) ||
    group.prefix && path.startsWith(group.prefix) ||
    group.prefixes?.some((prefix) => path.startsWith(prefix))
  ) || GROUPS[GROUPS.length - 1];
}

function displayFieldChoice(value) {
  if (value === "naive") return "Fixture AI (stress test)";
  return titleCase(value);
}

function buildField(spec) {
  const wrap = element("div", `field${spec.kind === "bool" ? " bool" : ""}`);
  wrap.dataset.field = spec.path;

  const label = element("label", "", spec.label);
  label.htmlFor = `f-${spec.path}`;
  if (spec.unit) {
    label.append(" ");
    const unit = element("span", "unit", `(${spec.unit.replace("m2", "m²")})`);
    label.appendChild(unit);
  }

  let input;
  if (spec.kind === "choice") {
    input = document.createElement("select");
    for (const choice of spec.choices) {
      const option = element("option", "", displayFieldChoice(choice));
      option.value = choice;
      input.appendChild(option);
    }
    input.value = spec.value;
  } else if (spec.kind === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(spec.value);
    input.setAttribute("role", "switch");
  } else if (spec.kind === "textarea") {
    input = document.createElement("textarea");
    input.value = spec.value ?? "";
  } else {
    input = document.createElement("input");
    input.type = spec.kind === "text" ? "text" : "number";
    if (spec.min !== undefined) input.min = String(spec.min);
    if (spec.max !== undefined) input.max = String(spec.max);
    if (spec.step !== undefined) input.step = String(spec.step);
    input.value = spec.value ?? "";
  }

  input.id = `f-${spec.path}`;
  input.dataset.path = spec.path;
  input.dataset.kind = spec.kind;
  input.addEventListener("input", () => {
    clearFieldError(spec);
    settingChanged(spec, wrap);
  });
  input.addEventListener("change", () => {
    settingChanged(spec, wrap);
    validateField(spec);
  });

  if (spec.kind === "bool") {
    wrap.append(label, input);
  } else {
    wrap.append(label, input);
  }

  const describedBy = [];
  if (spec.help) {
    const help = element("p", "help", spec.help);
    help.id = `help-${spec.path}`;
    describedBy.push(help.id);
    wrap.appendChild(help);
  }
  if (spec.kind === "int" || spec.kind === "number") {
    const error = element("p", "field-error");
    error.id = `error-${spec.path}`;
    error.hidden = true;
    describedBy.push(error.id);
    input.setAttribute("aria-errormessage", error.id);
    wrap.appendChild(error);
  }
  if (describedBy.length) input.setAttribute("aria-describedby", describedBy.join(" "));
  return wrap;
}

function buildSettings(payload, { invalidate = false } = {}) {
  state.settingsRevision += 1;
  state.settingsPayload = payload;
  state.fields = payload.fields;
  state.defaults = {};

  const scenarioLabel = titleCase(payload.scenario);
  setText("scenario-title", scenarioLabel);
  setText("scenario-name", String(payload.config_path).split(/[\\/]/).pop());
  setText("header-scenario", scenarioLabel);

  const form = $("settings-form");
  clearChildren(form);
  const bodies = new Map();
  for (const group of GROUPS) {
    const details = element("details", `settings-group${group.advanced ? " advanced" : ""}`);
    if (group.open) details.open = true;
    const summary = document.createElement("summary");
    const icon = element("span", "group-icon", group.icon);
    const title = element("span", "group-title", group.title);
    const count = element("span", "group-count", "0");
    summary.append(icon, title, count);
    const body = element("div", "settings-group-body");
    details.append(summary, body);
    form.appendChild(details);
    bodies.set(group.id, { body, count });
  }

  const counts = {};
  for (const spec of payload.fields) {
    state.defaults[spec.path] = spec.value;
    const group = groupFor(spec.path);
    bodies.get(group.id).body.appendChild(buildField(spec));
    counts[group.id] = (counts[group.id] || 0) + 1;
  }
  for (const group of GROUPS) {
    bodies.get(group.id).count.textContent = String(counts[group.id] || 0);
  }
  state.settingsReady = true;
  updateSettingsState();
  syncControlState();
  if (invalidate && state.result) markScenarioStale();
}

function readField(spec) {
  const input = $(`f-${spec.path}`);
  if (!input) return undefined;
  if (spec.kind === "bool") return input.checked;
  if (spec.kind === "int" || spec.kind === "number") {
    const raw = input.value.trim();
    return raw === "" ? Number.NaN : Number(raw);
  }
  return input.value;
}

function valuesEqual(left, right) {
  if (typeof left === "number" || typeof right === "number") {
    return Number(left) === Number(right);
  }
  return left === right;
}

function overrides() {
  const changed = {};
  for (const spec of state.fields) {
    const value = readField(spec);
    if ((spec.kind === "int" || spec.kind === "number") && !Number.isFinite(value)) {
      throw new Error(`${spec.label} must be a finite number.`);
    }
    if (!valuesEqual(value, state.defaults[spec.path])) changed[spec.path] = value;
  }
  return changed;
}

function clearFieldError(spec) {
  const input = $(`f-${spec.path}`);
  const error = $(`error-${spec.path}`);
  if (!input || !error) return;
  input.removeAttribute("aria-invalid");
  error.hidden = true;
  error.textContent = "";
}

function numericFieldError(spec) {
  if (spec.kind !== "int" && spec.kind !== "number") return "";
  const value = readField(spec);
  if (!Number.isFinite(value)) return `${spec.label} is required and must be a finite number.`;
  if (spec.kind === "int" && !Number.isInteger(value)) return `${spec.label} must be a whole number.`;
  if (spec.min !== undefined && value < Number(spec.min)) {
    return `${spec.label} must be at least ${spec.min}.`;
  }
  if (spec.max !== undefined && value > Number(spec.max)) {
    return `${spec.label} must be no more than ${spec.max}.`;
  }
  return "";
}

function validateField(spec) {
  const message = numericFieldError(spec);
  if (!message) {
    clearFieldError(spec);
    return true;
  }
  const input = $(`f-${spec.path}`);
  const error = $(`error-${spec.path}`);
  if (input && error) {
    input.setAttribute("aria-invalid", "true");
    error.textContent = message;
    error.hidden = false;
  }
  return false;
}

function validatedOverrides() {
  let firstInvalid = null;
  let invalidCount = 0;
  for (const spec of state.fields) {
    if (!validateField(spec)) {
      invalidCount += 1;
      if (!firstInvalid) firstInvalid = $(`f-${spec.path}`);
    }
  }
  if (firstInvalid) {
    showError(new Error(
      `${invalidCount} scenario value${invalidCount === 1 ? " needs" : "s need"} attention. Correct the highlighted field${invalidCount === 1 ? "" : "s"} before continuing.`
    ));
    const group = firstInvalid.closest("details");
    if (group) group.open = true;
    if (isDrawerMode() && !$("settings").classList.contains("open")) {
      openScenario({ focus: false });
    }
    setTimeout(() => firstInvalid.focus(), 0);
    return null;
  }
  return overrides();
}

function settingChanged(spec, wrap) {
  state.settingsRevision += 1;
  const changed = !valuesEqual(readField(spec), state.defaults[spec.path]);
  wrap.classList.toggle("changed", changed);
  updateSettingsState();
  if (state.result && !state.stale) markScenarioStale();
}

function updateSettingsState() {
  let count = 0;
  for (const spec of state.fields) {
    const wrap = document.querySelector(`[data-field="${CSS.escape(spec.path)}"]`);
    const changed = !valuesEqual(readField(spec), state.defaults[spec.path]);
    if (wrap) wrap.classList.toggle("changed", changed);
    if (changed) count += 1;
  }
  setText("settings-state", count ? `${count} setting${count === 1 ? "" : "s"} changed` : "Default scenario");
  $("settings-state").style.color = count ? "var(--amber)" : "";
}

function markScenarioStale() {
  state.stale = true;
  state.approvalToken = null;
  state.decisionToken = null;
  setFreshness("Settings changed", "stale");
  $("outcome").classList.add("stale-result");
  setText("decision-note", "This preview belongs to earlier settings. Generate the day plan again before deciding.");
  const card = $("verdict-card");
  card.className = "verdict-card warning";
  setText("verdict-symbol", "↻");
  setChip("verdict-badge", "settings changed", "warning");
  setText("verdict-lead", "The displayed verdict no longer applies to the scenario controls now shown.");
  syncControlState();
}

/* --------------------------------------------------------------- UI state */

const DRAWER_MEDIA = window.matchMedia("(max-width: 1100px)");

function isDrawerMode() {
  return DRAWER_MEDIA.matches;
}

function serialiseOverrides(value) {
  return JSON.stringify(Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right))));
}

function currentSettingsMatch(snapshot) {
  if (!snapshot) return true;
  if (state.settingsRevision !== snapshot.settingsRevision) return false;
  if (snapshot.overrides !== null) {
    try {
      if (serialiseOverrides(overrides()) !== snapshot.overrides) return false;
    } catch (_error) {
      return false;
    }
  }
  if (snapshot.plan !== undefined && JSON.stringify(state.plan) !== snapshot.plan) return false;
  return true;
}

function syncControlState() {
  const requestLocked = state.requestPending;
  const scenarioUnavailable = requestLocked || !state.settingsReady;
  qsa("#settings-form input, #settings-form select, #settings-form textarea").forEach((control) => {
    control.disabled = requestLocked;
  });
  for (const id of ["reset", "run", "empty-run", "compare", "show-comparison"]) {
    $(id).disabled = scenarioUnavailable;
  }
  $("empty-settings").disabled = requestLocked;
  $("scenario-toggle").disabled = requestLocked;
  $("comparison-back").disabled = requestLocked;

  const planLocked = requestLocked || state.decided || state.stale;
  qsa("#plan select, #plan input").forEach((control) => {
    control.disabled = planLocked;
  });
  $("comment").disabled = requestLocked || state.decided || state.stale;
  $("revert-edits").disabled = requestLocked || !state.result || !state.editedHours.size || state.stale || state.decided;
  $("reverify").disabled = requestLocked || !state.result || !state.dirty || state.stale || state.decided;
  $("reject").disabled = requestLocked || !state.result || !state.decisionToken || state.dirty || state.stale || state.decided;
  $("approve").disabled = requestLocked || !state.result || !state.approvalToken || state.dirty || state.stale || state.decided
    || !state.result.accepted || !state.result.checker_enabled;
}

function beginRequest(button, message, { requestOverrides = null, includePlan = false } = {}) {
  if (state.requestPending || !state.settingsReady) return null;
  const request = {
    id: ++state.requestSerial,
    settingsRevision: state.settingsRevision,
    overrides: requestOverrides === null ? null : serialiseOverrides(requestOverrides),
    plan: includePlan ? JSON.stringify(state.plan) : undefined,
  };
  state.requestPending = true;
  state.activeRequest = request.id;
  $("workspace").setAttribute("aria-busy", "true");
  button.classList.add("is-loading");
  button.dataset.hadAriaLabel = String(button.hasAttribute("aria-label"));
  button.dataset.previousAriaLabel = button.getAttribute("aria-label") || "";
  button.setAttribute("aria-label", message || "Working");
  syncControlState();
  return request;
}

function finishRequest(request, button) {
  if (!request || state.activeRequest !== request.id) return;
  state.requestPending = false;
  state.activeRequest = null;
  $("workspace").setAttribute("aria-busy", "false");
  button.classList.remove("is-loading");
  if (button.dataset.hadAriaLabel === "true") {
    button.setAttribute("aria-label", button.dataset.previousAriaLabel);
  } else {
    button.removeAttribute("aria-label");
  }
  delete button.dataset.hadAriaLabel;
  delete button.dataset.previousAriaLabel;
  syncControlState();
}

function responseIsCurrent(request) {
  return Boolean(request && state.activeRequest === request.id && currentSettingsMatch(request));
}

function reportDiscardedResponse() {
  if (state.result && !state.stale) markScenarioStale();
  showError(new Error("The scenario or plan changed while the request was running. Its response was ignored; generate again from the current state."));
}

function showError(error) {
  setText("error-text", String(error?.message || error));
  $("error").hidden = false;
  $("error").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearError() {
  $("error").hidden = true;
  setText("error-text", "");
}

function setChip(id, text, kind = "neutral") {
  const chip = $(id);
  chip.textContent = text;
  chip.className = `status-chip status-${kind}`;
}

function setFreshness(text, kind) {
  setChip("freshness-chip", text, kind === "fresh" ? "fresh" : kind);
}

function setSettingsInert(inert) {
  const settings = $("settings");
  settings.inert = inert;
  if (inert) settings.setAttribute("inert", "");
  else settings.removeAttribute("inert");
}

function openScenario({ focus = true } = {}) {
  const settings = $("settings");
  if (!isDrawerMode()) {
    const firstControl = settings.querySelector("input:not([disabled]), select:not([disabled]), textarea:not([disabled])");
    settings.scrollIntoView({ behavior: "smooth", block: "start" });
    if (focus && firstControl) setTimeout(() => firstControl.focus(), 0);
    return;
  }

  // The notice can wrap when the viewport changes. Measure immediately before
  // opening so the fixed drawer never uses a stale top offset.
  syncChromeOffsets();
  if (!settings.classList.contains("open")) {
    state.drawerReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : $("scenario-toggle");
  }
  setSettingsInert(false);
  settings.removeAttribute("aria-hidden");
  settings.setAttribute("role", "dialog");
  settings.setAttribute("aria-modal", "true");
  settings.setAttribute("aria-labelledby", "scenario-title");
  settings.classList.add("open");
  $("drawer-scrim").hidden = false;
  $("scenario-toggle").setAttribute("aria-expanded", "true");
  document.body.style.overflow = "hidden";
  if (focus) setTimeout(() => $("scenario-close").focus(), 0);
}

function closeScenario({ returnFocus = true } = {}) {
  const settings = $("settings");
  if (!isDrawerMode()) return;
  const returnTarget = state.drawerReturnFocus;
  settings.classList.remove("open");
  $("drawer-scrim").hidden = true;
  $("scenario-toggle").setAttribute("aria-expanded", "false");
  document.body.style.overflow = "";
  settings.removeAttribute("role");
  settings.removeAttribute("aria-modal");
  settings.removeAttribute("aria-labelledby");
  settings.setAttribute("aria-hidden", "true");
  setSettingsInert(true);
  state.drawerReturnFocus = null;
  if (returnFocus && returnTarget?.isConnected) returnTarget.focus();
}

function syncDrawerMode() {
  const settings = $("settings");
  if (isDrawerMode()) {
    if (!settings.classList.contains("open")) {
      settings.setAttribute("aria-hidden", "true");
      setSettingsInert(true);
    }
    return;
  }
  settings.classList.remove("open");
  settings.removeAttribute("role");
  settings.removeAttribute("aria-modal");
  settings.removeAttribute("aria-labelledby");
  settings.removeAttribute("aria-hidden");
  setSettingsInert(false);
  $("drawer-scrim").hidden = true;
  $("scenario-toggle").setAttribute("aria-expanded", "false");
  document.body.style.overflow = "";
  state.drawerReturnFocus = null;
}

function focusableDrawerElements() {
  return qsa(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
    $("settings")
  ).filter((control) => control.getClientRects().length > 0);
}

function syncChromeOffsets() {
  const noticeHeight = Math.ceil($("sim-notice").getBoundingClientRect().height) || 38;
  const headerHeight = Math.ceil(document.querySelector(".app-header").getBoundingClientRect().height) || 74;
  document.documentElement.style.setProperty("--notice-height", `${noticeHeight}px`);
  document.documentElement.style.setProperty("--header-height", `${headerHeight}px`);
}

function focusViewHeading(id) {
  const heading = $(id);
  if (!heading) return;
  setTimeout(() => {
    heading.focus({ preventScroll: true });
    heading.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 0);
}

function showPlanView() {
  $("comparison").hidden = true;
  if (state.result) {
    $("outcome").hidden = false;
    $("empty").hidden = true;
  } else {
    $("empty").hidden = false;
  }
}

function showComparisonView() {
  $("empty").hidden = true;
  $("outcome").hidden = true;
  $("comparison").hidden = false;
}

/* ------------------------------------------------------------- main render */

function renderOutcome(result, { preserveOriginal = false } = {}) {
  state.result = result;
  state.plan = clone(result.plan || []);
  if (!preserveOriginal) state.originalPlan = clone(state.plan);
  state.approvalToken = result.approval_token || null;
  state.decisionToken = result.decision_token || null;
  state.editedHours.clear();
  state.dirty = false;
  state.stale = false;
  state.decided = false;
  state.shownAt = performance.now();

  clearError();
  $("empty").hidden = true;
  $("comparison").hidden = true;
  $("outcome").hidden = false;
  $("outcome").classList.remove("stale-result");

  setText("elapsed", `${num(result.elapsed_s, 2)} s`);
  setText("run-date", result.date || "");
  setText("run-eyebrow", `${titleCase(result.planner || "planner")} · generated operating intent`);
  setText(
    "run-summary",
    result.fell_back
      ? `The requested planner exhausted ${result.revisions_used} revision${result.revisions_used === 1 ? "" : "s"}; the verified baseline fallback is shown.`
      : "Review the simulated outcome, verifier evidence and hourly intent before recording a decision."
  );

  const metrics = result.metrics || {};
  setText("stat-cost", eur(metrics.net_cost_eur));
  setText("stat-cost-m2", `${eur(metrics.net_cost_eur_per_m2, 3)} per m² · simulated`);
  setText("stat-growth", `${num(metrics.fruit_growth_kg_m2, 3)} kg/m²`);
  setText("stat-band", `${num(metrics.temperature_band_hours)} of 24 h in crop band`);

  const hard = finite(result.realised_hard, 0);
  const projected = finite(result.realised_projected, 0);
  setText("stat-violations", num(hard));
  setText("stat-projected", projected ? `${num(projected)} projected climate warning${projected === 1 ? "" : "s"}` : "No projected climate warnings");
  $("card-violations").className = `metric-card ${hard ? "bad" : result.checker_enabled ? "good" : "warn"}`;

  const headrooms = state.plan
    .filter((row) => Number.isFinite(Number(row.import_limit_kw)) && Number.isFinite(Number(row.grid_import_kw)))
    .map((row) => finite(row.import_limit_kw) - finite(row.grid_import_kw));
  const headroom = headrooms.length ? Math.min(...headrooms) : 0;
  setText("stat-headroom", `${headroom < 0 ? "−" : ""}${num(Math.abs(headroom))} kW`);
  setText("stat-peak", `${num(metrics.peak_import_kw)} kW peak import · minimum hourly margin`);
  const headroomCard = $("stat-headroom").closest(".metric-card");
  headroomCard.classList.toggle("bad", headroom < 0);
  headroomCard.classList.toggle("good", headroom >= 0);

  renderContext(result);
  renderVerdict(result);
  renderProfile(state.plan);
  renderIntentLanes(state.plan);
  renderKeyMoments(state.plan);
  renderCostBreakdown(metrics);
  renderPlan(state.plan);
  renderMetrics(metrics);
  updateEditState();

  setFreshness(result.accepted && result.checker_enabled ? "Verified preview" : "Review required", result.accepted && result.checker_enabled ? "fresh" : "warning");
  if (!result.checker_enabled) {
    setText("decision-note", "No approval is possible: the verifier was disabled. You may reject this exact snapshot or generate a verified condition.");
  } else if (!result.accepted) {
    setText("decision-note", "This plan failed verification. Edit the intent and re-verify, or reject it.");
  } else {
    setText("decision-note", `Approval is bound to plan ${result.plan_fingerprint || "shown"} and the exact settings above.`);
  }
  syncControlState();

  const context = result.context || {};
  setText("header-data", `${titleCase(context.data_source || "synthetic")} data`);
}

function renderContext(result) {
  const context = result.context || {};
  setText("context-scenario", titleCase(state.settingsPayload?.scenario || "Scenario"));
  setText("context-scale", `${titleCase(context.scale || "unknown")} · ${formatArea(context.area_m2)}`);
  setText("context-period", titleCase(context.period || "unknown"));
  setText("context-data", `${titleCase(context.data_source || "unknown")} · offline`);
  setText("context-fingerprint", result.plan_fingerprint || "not issued");
  setText("period-note", context.period_note || "Study window not reported.");
  setText(
    "model-note",
    result.validated
      ? `Greenhouse model: ${result.greenhouse_model}. Validation flag is present.`
      : `Greenhouse model “${result.greenhouse_model || "unknown"}” is not validated against measured AGC data. These values are apparatus, not findings.`
  );
}

function renderVerdict(result) {
  const card = $("verdict-card");
  const facts = $("verdict-facts");
  const violations = result.violations || [];
  clearChildren(facts);
  clearChildren($("violation-list"));
  $("verdict-details").open = violations.length > 0;

  let kind;
  if (!result.checker_enabled) {
    kind = "warning";
    card.className = "verdict-card warning";
    setText("verdict-symbol", "!");
    setChip("verdict-badge", "not verified", "warning");
    setText("verdict-lead", "The checker was switched off. This condition exists to measure what verification prevents.");
  } else if (result.accepted) {
    kind = "ok";
    card.className = "verdict-card";
    setText("verdict-symbol", "✓");
    setChip("verdict-badge", result.fell_back ? "verified fallback" : "verified", "ok");
    setText(
      "verdict-lead",
      result.fell_back
        ? "The proposed plan did not clear the safety case; the rule-based fallback shown here did."
        : "No blocking constraint was found for this exact plan under the forecast scenario."
    );
  } else {
    kind = "no";
    card.className = "verdict-card no";
    setText("verdict-symbol", "×");
    setChip("verdict-badge", "rejected", "no");
    setText("verdict-lead", "One or more hard constraints must be corrected before this plan can be approved.");
  }

  const factValues = [
    ["Checks run", String((result.checks_run || []).length)],
    ["Hard findings", String(violations.filter((item) => item.severity === "hard").length)],
    ["Projected", String(violations.filter((item) => item.severity === "projected").length)],
    ["Revisions", String(result.revisions_used ?? 0)],
  ];
  for (const [label, value] of factValues) {
    const fact = element("span", "fact");
    fact.append(document.createTextNode(`${label} `), element("strong", "", value));
    facts.appendChild(fact);
  }
  setText("verdict-text", result.feedback || "No checker feedback was returned.");

  for (const violation of violations.slice(0, 6)) {
    const item = element("article", "violation-item");
    const when = element("span", "violation-hour", violation.hour == null ? "DAY" : hourLabel(violation.hour));
    const copy = element("div");
    copy.append(
      element("strong", "", titleCase(violation.constraint || "Constraint")),
      element("p", "", violation.message || `${violation.actual} exceeds ${violation.bound}.`)
    );
    item.append(when, copy);
    $("violation-list").appendChild(item);
  }

  const notes = [];
  if (result.fell_back) notes.push(`Fallback after ${result.revisions_used} rejected revision${result.revisions_used === 1 ? "" : "s"}.`);
  if (!result.validated) notes.push("The greenhouse response remains unvalidated.");
  if (kind === "warning") notes.push("No plan-level approval token was issued.");
  setText("verdict-note", notes.join(" "));
}

/* ----------------------------------------------------------- visual profile */

function renderProfile(plan) {
  const target = $("profile-chart");
  if (!plan.length) {
    target.textContent = "No hourly profile available.";
    return;
  }

  const width = 960;
  const height = 278;
  const left = 48;
  const right = 18;
  const plotWidth = width - left - right;
  const step = plotWidth / 24;
  const x = (hour) => left + (hour + .5) * step;
  const prices = plan.map((row) => finite(row.power_price_eur_kwh));
  const heat = plan.map((row) => Math.max(0, finite(row.heat_demand_kw)));
  const imports = plan.map((row) => Math.max(0, finite(row.grid_import_kw)));
  const exports = plan.map((row) => Math.max(0, finite(row.grid_export_kw)));
  const limits = plan.map((row) => Math.max(0, finite(row.import_limit_kw)));
  const maxPrice = Math.max(...prices, .001);
  const maxHeat = Math.max(...heat, 1);
  const maxGrid = Math.max(...imports, ...exports, ...limits, 1);
  const topBase = 132;
  const topHeight = 82;
  const gridBase = 235;
  const gridHeight = 70;
  const topYPrice = (value) => topBase - value / maxPrice * topHeight;
  const topYHeat = (value) => topBase - value / maxHeat * topHeight;
  const gridY = (value) => gridBase - value / maxGrid * gridHeight;

  const path = (values, y) => values.map((value, index) =>
    `${index ? "L" : "M"}${x(index).toFixed(1)} ${y(value).toFixed(1)}`
  ).join(" ");
  const heatPath = `${path(heat, topYHeat)} L${x(23).toFixed(1)} ${topBase} L${x(0).toFixed(1)} ${topBase} Z`;
  const pricePath = path(prices, topYPrice);
  const limitPath = path(limits, gridY);
  const maxDefaultLimit = Math.max(...limits);

  const zones = limits.map((limit, index) => limit < maxDefaultLimit
    ? `<rect class="chart-zone" x="${(left + index * step).toFixed(1)}" y="151" width="${step.toFixed(1)}" height="91"/>`
    : "").join("");
  const gridLines = [0, 6, 12, 18, 24].map((hour) => {
    const gx = left + hour * step;
    return `<line class="chart-grid" x1="${gx}" y1="35" x2="${gx}" y2="242"/>`;
  }).join("");
  const labels = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((hour) => {
    const gx = left + hour * step;
    return `<text class="chart-axis" x="${gx}" y="265" text-anchor="middle">${String(hour).padStart(2, "0")}</text>`;
  }).join("");
  const bars = imports.map((value, index) => {
    const barWidth = Math.max(3, step * .42);
    const y = gridY(value);
    return `<rect class="chart-gridbar" x="${(x(index) - barWidth / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${(gridBase - y).toFixed(1)}" rx="2"/>`;
  }).join("");
  const exportBars = exports.map((value, index) => {
    if (!value) return "";
    const barWidth = Math.max(3, step * .28);
    const barHeight = value / maxGrid * 18;
    return `<rect class="chart-gridbar export" x="${(x(index) - barWidth / 2).toFixed(1)}" y="${gridBase}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="2"/>`;
  }).join("");
  const priceDots = prices.map((value, index) =>
    `<circle class="chart-dot" cx="${x(index).toFixed(1)}" cy="${topYPrice(value).toFixed(1)}" r="2.3"><title>${hourLabel(index)} · ${eur(value, 3)}/kWh</title></circle>`
  ).join("");

  target.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false">
      <text class="chart-title" x="${left}" y="19">FORECAST SIGNALS</text>
      <text class="chart-note" x="${width - right}" y="19" text-anchor="end">max ${eur(maxPrice, 3)}/kWh · max ${num(maxHeat)} kWth</text>
      ${gridLines}
      <line class="chart-grid" x1="${left}" y1="${topBase}" x2="${width - right}" y2="${topBase}"/>
      <path class="chart-demand" d="${heatPath}"/>
      <path class="chart-price" d="${pricePath}"/>
      ${priceDots}
      <text class="chart-title" x="${left}" y="158">SIMULATED GRID POSITION</text>
      <text class="chart-note" x="${width - right}" y="158" text-anchor="end">bars import · purple below axis export</text>
      ${zones}
      <line class="chart-grid" x1="${left}" y1="${gridBase}" x2="${width - right}" y2="${gridBase}"/>
      ${bars}${exportBars}
      <path class="chart-limit" d="${limitPath}"/>
      ${labels}
    </svg>`;

  const pricePeak = prices.indexOf(maxPrice);
  const heatPeak = heat.indexOf(maxHeat);
  const minHeadroom = Math.min(...plan.map((row) => finite(row.import_limit_kw) - finite(row.grid_import_kw)));
  setText(
    "profile-summary",
    `Electricity price peaks at ${hourLabel(pricePeak)}. Heat demand peaks at ${hourLabel(heatPeak)}. Minimum grid import headroom is ${num(minHeadroom)} kilowatts.`
  );
}

function laneCell(kind, label, hour, extraClass = "") {
  const cell = element("button", `lane-cell ${kind} ${extraClass}`);
  cell.type = "button";
  cell.dataset.tip = `${hourLabel(hour)} · ${label}`;
  cell.setAttribute("aria-label", `${label} at ${hourLabel(hour)}`);
  cell.addEventListener("click", () => focusHour(hour));
  return cell;
}

function renderIntentLanes(plan) {
  const target = $("intent-lanes");
  clearChildren(target);
  target.appendChild(element("span", "lane-label", "Hour"));
  for (const row of plan) target.appendChild(element("span", "lane-hour", String(row.hour).padStart(2, "0")));

  const lanes = [
    {
      label: "Lighting",
      build: (row) => {
        const cell = laneCell("light", `Lighting ${percent(row.lighting_level)}`, row.hour);
        cell.style.setProperty("--level", `${finite(row.lighting_level) * 100}%`);
        return cell;
      },
    },
    {
      label: "Battery",
      build: (row) => laneCell(row.battery, `${titleCase(row.battery)} ${num(row.battery_power_kw)} kW`, row.hour),
    },
    {
      label: "Heat",
      build: (row) => laneCell(row.heat_source, `Heat from ${titleCase(row.heat_source)}`, row.hour),
    },
    {
      label: "CHP",
      build: (row) => laneCell(row.chp_mode === "off" ? "off" : "chp", `CHP ${titleCase(row.chp_mode)}`, row.hour),
    },
  ];

  for (const lane of lanes) {
    target.appendChild(element("span", "lane-label", lane.label));
    for (const row of plan) target.appendChild(lane.build(row));
  }
}

function renderKeyMoments(plan) {
  const target = $("key-moments");
  clearChildren(target);
  if (!plan.length) return;
  const prices = plan.map((row) => finite(row.power_price_eur_kwh));
  const highestPrice = prices.indexOf(Math.max(...prices));
  const headrooms = plan.map((row) => finite(row.import_limit_kw) - finite(row.grid_import_kw));
  const tightest = headrooms.indexOf(Math.min(...headrooms));
  const transitions = plan.findIndex((row, index) => index > 0 && (
    row.battery !== plan[index - 1].battery || row.chp_mode !== plan[index - 1].chp_mode
  ));

  const moments = [
    {
      hour: highestPrice,
      title: `Price peak · ${eur(prices[highestPrice], 3)}/kWh`,
      copy: plan[highestPrice].reasoning || "Review the selected assets against the market peak.",
    },
    {
      hour: tightest,
      title: `Tightest grid margin · ${num(headrooms[tightest])} kW`,
      copy: `${num(plan[tightest].grid_import_kw)} kW import against a ${num(plan[tightest].import_limit_kw)} kW hourly limit.`,
    },
  ];
  if (transitions > 0) {
    moments.push({
      hour: transitions,
      title: "Asset strategy changes",
      copy: plan[transitions].reasoning || "The planner switches its storage or CHP intent here.",
    });
  }

  for (const moment of moments) {
    const row = element("div", "moment");
    row.appendChild(element("time", "", hourLabel(moment.hour)));
    const copy = element("div");
    copy.append(element("strong", "", moment.title), element("p", "", moment.copy));
    row.appendChild(copy);
    target.appendChild(row);
  }
}

function renderCostBreakdown(metrics) {
  const target = $("cost-breakdown");
  clearChildren(target);
  const parts = [
    ["Grid purchases", finite(metrics.grid_import_cost_eur), ""],
    ["Gas", finite(metrics.gas_cost_eur), ""],
    ["Liquid CO₂", finite(metrics.co2_cost_eur), ""],
    ["Market revenue", finite(metrics.market_revenue_eur), "revenue"],
  ];
  const max = Math.max(...parts.map((part) => part[1]), 1);
  for (const [label, value, className] of parts) {
    const row = element("div", `cost-row ${className}`);
    const bar = element("div", "cost-bar");
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(2, value / max * 100)}%`;
    bar.appendChild(fill);
    row.append(element("span", "", label), bar, element("strong", "", `${className ? "−" : ""}${eur(value)}`));
    target.appendChild(row);
  }
  const net = element("div", "cost-row net");
  net.append(element("span", "", "Net cost"), element("div", "cost-bar"), element("strong", "", eur(metrics.net_cost_eur)));
  target.appendChild(net);
}

function renderMetrics(metrics) {
  const body = document.querySelector("#metrics tbody");
  clearChildren(body);
  const units = {
    net_cost_eur: "€",
    net_cost_eur_per_m2: "€/m²",
    grid_import_cost_eur: "€",
    gas_cost_eur: "€",
    market_revenue_eur: "€",
    co2_cost_eur: "€",
    peak_import_kw: "kW",
    peak_export_kw: "kW",
    grid_import_kwh: "kWh",
    grid_export_kwh: "kWh",
    gas_input_kwh: "kWh",
    dli_mol_m2: "mol/m²",
    heat_shortfall_kwh: "kWh",
    final_soc_kwh: "kWh",
    final_buffer_kwh: "kWh",
    fruit_growth_kg_m2: "kg/m²",
    natural_dli_mol_m2: "mol/m²",
    supplemental_dli_mol_m2: "mol/m²",
    temperature_band_hours: "h",
    heat_dumped_kwh: "kWh",
  };
  for (const [key, value] of Object.entries(metrics)) {
    const row = document.createElement("tr");
    row.append(
      element("td", "", key.replace(/_/g, " ")),
      element("td", "", `${num(value, 2)}${units[key] ? ` ${units[key]}` : ""}`)
    );
    body.appendChild(row);
  }
}

/* ------------------------------------------------------------- plan editor */

function makeSelect(options, value, label, onChange) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", label);
  for (const optionValue of options) {
    const option = element("option", "", titleCase(optionValue));
    option.value = optionValue;
    select.appendChild(option);
  }
  select.value = value;
  select.addEventListener("change", (event) => onChange(event.target.value));
  return select;
}

function renderPlan(plan) {
  state.plan = clone(plan);
  const body = document.querySelector("#plan tbody");
  clearChildren(body);

  state.plan.forEach((row) => {
    const tr = document.createElement("tr");
    tr.id = `row-${row.hour}`;
    tr.classList.toggle("edited", state.editedHours.has(row.hour));

    const hour = element("th", "hour-cell");
    hour.scope = "row";
    hour.append(document.createTextNode(String(row.hour).padStart(2, "0")), element("small", "", row.hour < 6 ? "night" : row.hour < 12 ? "morning" : row.hour < 18 ? "day" : "evening"));
    tr.appendChild(hour);

    const market = element("td", "market-cell");
    market.append(element("strong", "", `${eur(row.power_price_eur_kwh, 3)}/kWh`), element("small", "", `${num(row.heat_demand_kw)} kWth heat`));
    tr.appendChild(market);

    const headroom = finite(row.import_limit_kw) - finite(row.grid_import_kw);
    const grid = element("td", "grid-cell");
    const gridMain = element("strong", headroom < 0 ? "value-risk" : "value-safe", `${num(row.grid_import_kw)} kW`);
    grid.append(gridMain, element("small", "", `${num(headroom)} kW margin`));
    tr.appendChild(grid);

    const touched = () => touchHour(row.hour, tr);
    const heatCell = document.createElement("td");
    heatCell.appendChild(makeSelect(HEAT, row.heat_source, `Heat source at ${hourLabel(row.hour)}`, (value) => { row.heat_source = value; touched(); }));
    tr.appendChild(heatCell);

    const lightCell = document.createElement("td");
    const lightWrap = element("div", "lighting-editor");
    const light = document.createElement("input");
    light.type = "number";
    light.min = "0";
    light.max = "100";
    light.step = "5";
    light.value = String(Math.round(finite(row.lighting_level) * 100));
    light.setAttribute("aria-label", `Lighting percentage at ${hourLabel(row.hour)}`);
    const lightUnit = element("span", "", "%");
    light.addEventListener("change", () => {
      const value = Math.max(0, Math.min(100, finite(light.value)));
      light.value = String(value);
      row.lighting_level = value / 100;
      touched();
    });
    lightWrap.append(light, lightUnit);
    lightCell.appendChild(lightWrap);
    tr.appendChild(lightCell);

    const batteryCell = document.createElement("td");
    const batteryWrap = element("div", "battery-editor");
    batteryWrap.appendChild(makeSelect(BATT, row.battery, `Battery action at ${hourLabel(row.hour)}`, (value) => { row.battery = value; touched(); }));
    const power = document.createElement("input");
    power.type = "number";
    power.min = "0";
    power.step = "50";
    power.value = String(finite(row.battery_power_kw));
    power.setAttribute("aria-label", `Battery power in kilowatts at ${hourLabel(row.hour)}`);
    power.addEventListener("change", () => {
      row.battery_power_kw = Math.max(0, finite(power.value));
      power.value = String(row.battery_power_kw);
      touched();
    });
    batteryWrap.appendChild(power);
    batteryCell.appendChild(batteryWrap);
    tr.appendChild(batteryCell);

    const chpCell = document.createElement("td");
    chpCell.appendChild(makeSelect(CHP, row.chp_mode, `CHP mode at ${hourLabel(row.hour)}`, (value) => { row.chp_mode = value; touched(); }));
    tr.appendChild(chpCell);

    const co2Cell = document.createElement("td");
    co2Cell.appendChild(makeSelect(CO2, row.co2_source, `CO2 source at ${hourLabel(row.hour)}`, (value) => { row.co2_source = value; touched(); }));
    tr.appendChild(co2Cell);

    tr.appendChild(element("td", "rationale", row.reasoning || "No rationale supplied."));
    body.appendChild(tr);
  });
}

function touchHour(hour, rowElement) {
  state.editedHours.add(hour);
  rowElement.classList.add("edited");
  state.dirty = true;
  state.approvalToken = null;
  state.decisionToken = null;
  state.decided = false;
  updateEditState();
  renderIntentLanes(state.plan);
  setFreshness("Unverified draft", "warning");
  $("verdict-card").className = "verdict-card warning";
  setText("verdict-symbol", "↻");
  setChip("verdict-badge", "needs verification", "warning");
  setText("verdict-lead", `${state.editedHours.size} hour${state.editedHours.size === 1 ? " has" : "s have"} local edits. The previous verdict no longer applies.`);
  setText("decision-note", "Re-verify every edit before recording approval or rejection.");
  syncControlState();
}

function updateEditState() {
  const count = state.editedHours.size;
  setChip("edit-count", count ? `${count} edited hour${count === 1 ? "" : "s"}` : "No edits", count ? "warning" : "neutral");
  syncControlState();
}

function focusHour(hour) {
  const row = $(`row-${hour}`);
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  const control = row.querySelector("select, input");
  if (control) setTimeout(() => control.focus(), 250);
}

function revertEdits() {
  state.plan = clone(state.originalPlan);
  state.editedHours.clear();
  state.dirty = true;
  state.approvalToken = null;
  state.decisionToken = null;
  state.decided = false;
  renderPlan(state.plan);
  renderIntentLanes(state.plan);
  updateEditState();
  setFreshness("Needs re-verification", "warning");
  $("verdict-card").className = "verdict-card warning";
  setText("verdict-symbol", "↻");
  setChip("verdict-badge", "needs verification", "warning");
  setText("verdict-lead", "The generated plan has been restored locally. Re-verify it to issue a new approval token.");
  setText("decision-note", "Re-verify the restored plan before recording approval or rejection.");
  syncControlState();
}

/* ---------------------------------------------------------------- actions */

async function run(event) {
  const button = event?.currentTarget || $("run");
  clearError();
  const requestOverrides = validatedOverrides();
  if (requestOverrides === null) return;
  const request = beginRequest(button, "Generating the day plan", { requestOverrides });
  if (!request) return;
  try {
    const result = await api("/api/run", { overrides: requestOverrides });
    if (!responseIsCurrent(request)) {
      reportDiscardedResponse();
      return;
    }
    renderOutcome(result);
    closeScenario({ returnFocus: false });
    focusViewHeading("outcome-heading");
  } catch (error) {
    showError(error);
  } finally {
    finishRequest(request, button);
  }
}

async function reverify() {
  if (state.stale) {
    showError(new Error("Scenario settings changed. Generate the day plan again before re-verifying edits."));
    return;
  }
  const button = $("reverify");
  const editedBeforeVerification = new Set(state.editedHours);
  clearError();
  const requestOverrides = validatedOverrides();
  if (requestOverrides === null) return;
  const requestPlan = clone(state.plan);
  const request = beginRequest(button, "Re-verifying edited plan", {
    requestOverrides,
    includePlan: true,
  });
  if (!request) return;
  try {
    const update = await api("/api/verify", {
      overrides: requestOverrides,
      plan: requestPlan,
    });
    if (!responseIsCurrent(request)) {
      reportDiscardedResponse();
      return;
    }
    const hard = (update.violations || []).filter((item) => item.severity === "hard").length;
    const projected = (update.violations || []).filter((item) => item.severity === "projected").length;
    const merged = {
      ...state.result,
      ...update,
      checker_enabled: state.result.checker_enabled,
      realised_hard: hard,
      realised_projected: projected,
      elapsed_s: state.result.elapsed_s,
      revisions_used: state.result.revisions_used,
      fell_back: state.result.fell_back,
    };
    renderOutcome(merged, { preserveOriginal: !update.accepted });
    if (update.accepted) {
      state.originalPlan = clone(state.plan);
    } else {
      // Keep the rejected draft visibly editable and revertible.  The server's
      // response contains the same submitted intent plus fresh outcome data;
      // renderOutcome resets transient edit state, so restore it here.
      state.editedHours = editedBeforeVerification;
      state.dirty = false;
      renderPlan(state.plan);
      updateEditState();
    }
    setFreshness(update.accepted ? "Re-verified" : "Rejected", update.accepted ? "fresh" : "no");
    setText(
      "decision-note",
      update.accepted
        ? `Edited plan ${update.plan_fingerprint} is verified and ready for a human decision.`
        : "The edited plan is rejected. Correct the highlighted intent and re-verify, or record a rejection."
    );
  } catch (error) {
    state.approvalToken = null;
    state.decisionToken = null;
    setFreshness("Verification failed", "no");
    $("verdict-card").className = "verdict-card no";
    setText("verdict-symbol", "×");
    setChip("verdict-badge", "not verified", "no");
    setText("verdict-lead", "Re-verification failed, so no earlier verdict applies to this draft.");
    showError(error);
    syncControlState();
  } finally {
    finishRequest(request, button);
  }
}

async function decide(decision) {
  clearError();
  if (state.stale || state.dirty || state.decided) {
    showError(new Error("This plan is not a current, unchanged decision snapshot. Generate or re-verify it before recording a decision."));
    return;
  }
  if (decision === "approve" && !state.approvalToken) {
    showError(new Error("Approval is unavailable because this exact plan does not have a current verification token."));
    return;
  }
  if (!state.decisionToken) {
    showError(new Error("This exact plan does not have a current decision token. Generate or re-verify it before rejecting."));
    return;
  }
  const requestOverrides = validatedOverrides();
  if (requestOverrides === null) return;
  const button = decision === "approve" ? $("approve") : $("reject");
  const request = beginRequest(button, `Recording ${decision}`, {
    requestOverrides,
    includePlan: true,
  });
  if (!request) return;
  try {
    const response = await api("/api/decision", {
      decision,
      approval_token: state.approvalToken,
      decision_token: state.decisionToken,
      plan_fingerprint: state.result.plan_fingerprint,
      comment: $("comment").value,
      seconds_to_decide: state.shownAt
        ? Math.round((performance.now() - state.shownAt) / 100) / 10
        : null,
      overrides: requestOverrides,
    });
    if (!responseIsCurrent(request)) {
      reportDiscardedResponse();
      return;
    }
    state.approvalToken = null;
    state.decisionToken = null;
    state.decided = true;
    $("comment").value = "";
    setFreshness(decision === "approve" ? "Approval recorded" : "Rejection recorded", decision === "approve" ? "fresh" : "no");
    setText(
      "decision-note",
      `${titleCase(response.decision)} recorded in the append-only audit log for plan ${state.result.plan_fingerprint}. Generate a new plan to make another decision.`
    );
    syncControlState();
  } catch (error) {
    // A decision request can succeed server-side even if its response is lost.
    // Discard both one-use capabilities rather than risk presenting a replay.
    state.approvalToken = null;
    state.decisionToken = null;
    syncControlState();
    showError(error);
  } finally {
    finishRequest(request, button);
  }
}

function comparisonInterpretation(row, baseline) {
  if (row.error) return "Visible capability gap; this condition did not run.";
  if (row.planner === "rule-based") return "Conventional intent baseline for paired comparison.";
  if (row.planner === "ai-unverified") return row.hard_violations
    ? `Verifier off: ${row.hard_violations} hard violation${row.hard_violations === 1 ? "" : "s"} reached the realised audit.`
    : "Verifier off; no hard violation appeared in this particular synthetic day.";
  if (row.planner === "ai-verified") return row.fell_back
    ? "Unsafe fixture plan was rejected; baseline fallback carried the day."
    : "Fixture plan passed deterministic verification.";
  if (row.planner === "learned" && baseline) {
    const costDelta = (row.cost_eur - baseline.cost_eur) / Math.max(Math.abs(baseline.cost_eur), 1);
    const growthDelta = row.growth_kg_m2 - baseline.growth_kg_m2;
    return `${costDelta <= 0 ? "Lower" : "Higher"} cost (${percent(Math.abs(costDelta), 1)}); growth delta ${growthDelta >= 0 ? "+" : ""}${num(growthDelta, 3)} kg/m².`;
  }
  return "Read cost, crop and safety together; no single metric defines a winner.";
}

function renderComparison(rows) {
  const cards = $("compare-cards");
  const body = document.querySelector("#compare-table tbody");
  clearChildren(cards);
  clearChildren(body);
  const baseline = rows.find((row) => row.planner === "rule-based" && !row.error);

  for (const row of rows) {
    const interpretation = comparisonInterpretation(row, baseline);
    const card = element("article", `compare-card${row.planner === "ai-verified" ? " featured" : ""}${row.error ? " error" : ""}`);
    const title = element("div");
    title.append(
      element("h3", "", titleCase(row.planner)),
      element("span", "condition-sub", row.error ? "Condition unavailable" : `${titleCase(row.engine)} engine`)
    );
    const badge = element("span", `condition-badge${row.checker_enabled === false ? " off" : ""}`, row.error ? "Gap" : row.checker_enabled ? "Verifier on" : "Verifier off");
    card.append(title, badge);

    if (row.error) {
      card.append(element("p", "interpretation", row.error));
    } else {
      card.appendChild(element("strong", "compare-cost", eur(row.cost_eur)));
      const stats = element("div", "compare-stats");
      const values = [
        ["Hard violations", num(row.hard_violations)],
        ["Crop growth", `${num(row.growth_kg_m2, 3)} kg/m²`],
        ["Peak import", `${num(row.peak_import_kw)} kW`],
      ];
      for (const [label, value] of values) {
        const stat = element("div", "compare-stat");
        stat.append(element("span", "", label), element("strong", "", value));
        stats.appendChild(stat);
      }
      card.append(stats, element("p", "interpretation", interpretation));
    }
    cards.appendChild(card);

    const tr = document.createElement("tr");
    if (row.error) {
      const name = element("td", "", titleCase(row.planner));
      const error = element("td", "", row.error);
      error.colSpan = 7;
      error.className = "value-risk";
      tr.append(name, error);
    } else {
      const values = [
        titleCase(row.planner),
        row.checker_enabled ? "On" : "Off",
        eur(row.cost_eur),
        num(row.hard_violations),
        `${num(row.growth_kg_m2, 3)} kg/m²`,
        `${num(row.band_hours)} h`,
        `${num(row.peak_import_kw)} kW`,
        interpretation,
      ];
      values.forEach((value, index) => {
        const td = element("td", index === 3 && row.hard_violations ? "value-risk" : "", value);
        tr.appendChild(td);
      });
    }
    body.appendChild(tr);
  }
}

async function compare(event) {
  const button = event?.currentTarget || $("compare");
  clearError();
  const requestOverrides = validatedOverrides();
  if (requestOverrides === null) return;
  const request = beginRequest(button, "Comparing research conditions", { requestOverrides });
  if (!request) return;
  showComparisonView();
  setText("compare-status", "Running the paired conditions on the exact same scenario…");
  try {
    const response = await api("/api/compare", {
      overrides: requestOverrides,
      planners: CONDITIONS,
    });
    if (!responseIsCurrent(request)) {
      setText("compare-status", "The scenario changed during comparison; the response was ignored.");
      reportDiscardedResponse();
      return;
    }
    renderComparison(response.rows || []);
    const completed = (response.rows || []).filter((row) => !row.error).length;
    setText("compare-status", `${completed} of ${CONDITIONS.length} conditions completed. Unimplemented conditions remain visible as gaps.`);
    closeScenario({ returnFocus: false });
    focusViewHeading("comparison-heading");
  } catch (error) {
    showError(error);
    setText("compare-status", "Comparison did not complete.");
  } finally {
    finishRequest(request, button);
  }
}

async function resetSettings(event) {
  const button = event?.currentTarget || $("reset");
  clearError();
  const request = beginRequest(button, "Resetting scenario settings");
  if (!request) return;
  try {
    const payload = await api("/api/settings");
    if (state.activeRequest !== request.id) {
      reportDiscardedResponse();
      return;
    }
    buildSettings(payload, { invalidate: true });
  } catch (error) {
    showError(error);
  } finally {
    finishRequest(request, button);
  }
}

/* -------------------------------------------------------------------- init */

$("run").addEventListener("click", run);
$("empty-run").addEventListener("click", run);
$("compare").addEventListener("click", compare);
$("show-comparison").addEventListener("click", compare);
$("comparison-back").addEventListener("click", showPlanView);
$("reverify").addEventListener("click", reverify);
$("revert-edits").addEventListener("click", revertEdits);
$("approve").addEventListener("click", () => decide("approve"));
$("reject").addEventListener("click", () => decide("reject"));
$("error-close").addEventListener("click", clearError);
$("scenario-toggle").addEventListener("click", () => {
  if ($("settings").classList.contains("open")) closeScenario();
  else openScenario();
});
$("scenario-close").addEventListener("click", () => closeScenario());
$("drawer-scrim").addEventListener("click", () => closeScenario());
$("empty-settings").addEventListener("click", () => openScenario());
$("reset").addEventListener("click", resetSettings);

document.addEventListener("keydown", (event) => {
  const drawerOpen = isDrawerMode() && $("settings").classList.contains("open");
  if (event.key === "Escape" && drawerOpen) {
    event.preventDefault();
    closeScenario();
    return;
  }
  if (event.key !== "Tab" || !drawerOpen) return;
  const controls = focusableDrawerElements();
  if (!controls.length) {
    event.preventDefault();
    return;
  }
  const first = controls[0];
  const last = controls[controls.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !$("settings").contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
});

if (typeof DRAWER_MEDIA.addEventListener === "function") {
  DRAWER_MEDIA.addEventListener("change", syncDrawerMode);
} else {
  DRAWER_MEDIA.addListener(syncDrawerMode);
}
window.addEventListener("resize", () => requestAnimationFrame(syncChromeOffsets));
if ("ResizeObserver" in window) {
  const chromeObserver = new ResizeObserver(syncChromeOffsets);
  chromeObserver.observe($("sim-notice"));
  chromeObserver.observe(document.querySelector(".app-header"));
}

syncChromeOffsets();
syncDrawerMode();
syncControlState();

api("/api/settings")
  .then((payload) => buildSettings(payload))
  .catch(showError);
