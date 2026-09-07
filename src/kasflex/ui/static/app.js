"use strict";

/* KasFlex interface.
 *
 * No framework and no build step: this is one page talking to a local server, and
 * a toolchain would sit between a researcher and a one-line change.
 *
 * Two rules the code enforces rather than trusts:
 *   - An edited plan is never approvable until it has been re-verified. The
 *     Approve button disables the moment a cell changes.
 *   - The server decides feasibility. Nothing here judges a plan; it only shows
 *     what came back.
 */

const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
};

const HEAT = ["boiler", "chp", "buffer", "none"];
const BATT = ["idle", "charge", "discharge"];
const CHP = ["off", "heat_led", "max_export"];
const CO2 = ["none", "chp", "liquid"];

const state = {
  fields: [],
  defaults: {},
  plan: [],
  dirty: false,
  verified: true,
  shownAt: null,   // when the plan appeared, for the decision-time metric (R26)
};

/* ------------------------------------------------------------- settings */

function buildSettings(payload) {
  state.fields = payload.fields;
  $("scenario-name").textContent = `${payload.scenario} · ${payload.config_path}`;
  const form = $("settings-form");
  form.innerHTML = "";

  for (const f of payload.fields) {
    state.defaults[f.path] = f.value;
    const wrap = document.createElement("div");
    wrap.className = "field" + (f.kind === "bool" ? " bool" : "");

    const label = document.createElement("label");
    label.htmlFor = `f-${f.path}`;
    label.textContent = f.label + (f.unit ? " " : "");
    if (f.unit) {
      const u = document.createElement("span");
      u.className = "unit";
      u.textContent = `(${f.unit})`;
      label.appendChild(u);
    }

    let input;
    if (f.kind === "choice") {
      input = document.createElement("select");
      for (const c of f.choices) {
        const o = document.createElement("option");
        o.value = c; o.textContent = c;
        input.appendChild(o);
      }
      input.value = f.value;
    } else if (f.kind === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(f.value);
    } else if (f.kind === "textarea") {
      input = document.createElement("textarea");
      input.value = f.value ?? "";
    } else if (f.kind === "text") {
      input = document.createElement("input");
      input.type = "text";
      input.value = f.value ?? "";
    } else {
      input = document.createElement("input");
      input.type = "number";
      if (f.min !== undefined) input.min = f.min;
      if (f.max !== undefined) input.max = f.max;
      if (f.step !== undefined) input.step = f.step;
      input.value = f.value;
    }
    input.id = `f-${f.path}`;
    input.dataset.path = f.path;
    input.dataset.kind = f.kind;

    if (f.kind === "bool") { wrap.appendChild(input); wrap.appendChild(label); }
    else { wrap.appendChild(label); wrap.appendChild(input); }

    if (f.help) {
      const h = document.createElement("p");
      h.className = "help";
      h.textContent = f.help;
      wrap.appendChild(h);
    }
    form.appendChild(wrap);
  }
}

/** Only send what the user actually changed, so a run is reproducible from the
 *  scenario file plus a short list of overrides. */
function overrides() {
  const out = {};
  for (const f of state.fields) {
    const el = $(`f-${f.path}`);
    if (!el) continue;
    let v;
    if (f.kind === "bool") v = el.checked;
    else if (f.kind === "int") v = parseInt(el.value, 10);
    else if (f.kind === "number") v = parseFloat(el.value);
    else v = el.value;
    if (f.kind === "int" && Number.isNaN(v)) continue;
    if (f.kind === "number" && Number.isNaN(v)) continue;
    if (v !== state.defaults[f.path]) out[f.path] = v;
  }
  return out;
}

/* -------------------------------------------------------------- helpers */

const eur = (v) => "€ " + Number(v).toLocaleString("en-GB", { maximumFractionDigits: 0 });
const num = (v, d = 0) => Number(v).toLocaleString("en-GB",
  { minimumFractionDigits: d, maximumFractionDigits: d });

function busy(on) {
  document.body.classList.toggle("busy", on);
  $("run").textContent = on ? "Running…" : "Plan the day";
}

function showError(err) {
  const box = $("error");
  box.textContent = String(err.message || err);
  box.hidden = false;
  $("empty").hidden = true;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* --------------------------------------------------------------- render */

function renderOutcome(r) {
  $("empty").hidden = true;
  $("error").hidden = true;
  $("outcome").hidden = false;
  $("elapsed").textContent = `ran in ${r.elapsed_s}s`;

  const m = r.metrics;
  $("stat-cost").textContent = eur(m.net_cost_eur);
  $("stat-cost-m2").textContent = `€ ${num(m.net_cost_eur_per_m2, 3)} per m²`;
  $("stat-growth").textContent = num(m.fruit_growth_kg_m2, 3) + " kg/m²";
  $("stat-band").textContent = `${num(m.temperature_band_hours)} of 24 h in the crop band`;

  $("stat-violations").textContent = r.realised_hard;
  $("stat-projected").textContent = r.realised_projected
    ? `${r.realised_projected} projected (climate bands)`
    : "none projected";
  $("card-violations").className = "card stat " + (r.realised_hard ? "bad" : "good");

  // With the checker off, nothing accepted anything. Saying "accepted" in green
  // beside a card reporting 66 violations invites exactly the wrong reading, and
  // this screen is the one place a person decides whether to trust the plan.
  const badge = $("verdict-badge");
  if (!r.checker_enabled) {
    badge.textContent = "not verified";
    badge.className = "badge no";
  } else {
    badge.textContent = r.accepted ? "accepted" : "rejected";
    badge.className = "badge " + (r.accepted ? "ok" : "no");
  }
  $("verdict-text").textContent = r.checker_enabled
    ? r.feedback
    : "The safety checker was switched off. This plan was executed without being checked against any limit.";

  const notes = [];
  if (!r.checker_enabled) notes.push("Turn the checker back on to see what it would have caught.");
  if (r.fell_back) notes.push(`The planner was rejected ${r.revisions_used} time(s); the rule-based baseline took over.`);
  $("verdict-note").textContent = notes.join(" ");

  $("model-note").textContent = r.validated
    ? `Greenhouse model: ${r.greenhouse_model}.`
    : `Greenhouse model “${r.greenhouse_model}” has not been validated against measured data. These figures are not results.`;

  const rows = Object.entries(m)
    .map(([k, v]) => `<tr><td>${k.replace(/_/g, " ")}</td><td class="num">${num(v, 2)}</td></tr>`)
    .join("");
  $("metrics").innerHTML = rows;

  renderPlan(r.plan);
  state.shownAt = performance.now();
  markVerified(true);
  if (!r.checker_enabled) {
    $("approve").disabled = true;
    $("decision-note").textContent =
      "Nothing to approve: this plan was never verified. Approval exists to record a " +
      "human decision on a checked plan.";
  }
}

function select(options, value, onChange) {
  const el = document.createElement("select");
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o.replace(/_/g, " ");
    el.appendChild(opt);
  }
  el.value = value;
  el.addEventListener("change", onChange);
  return el;
}

function renderPlan(plan) {
  state.plan = plan.map((p) => ({ ...p }));
  const body = document.querySelector("#plan tbody");
  body.innerHTML = "";

  state.plan.forEach((row, i) => {
    const tr = document.createElement("tr");
    const touched = () => {
      tr.classList.add("edited");
      markVerified(false);
    };

    const cell = (html, cls) => {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      if (typeof html === "string") td.textContent = html;
      else td.appendChild(html);
      tr.appendChild(td);
      return td;
    };

    cell(String(row.hour).padStart(2, "0"));
    cell("€ " + row.power_price_eur_kwh.toFixed(3), "num");
    cell(num(row.heat_demand_kw) + " kW", "num");

    cell(select(HEAT, row.heat_source, (e) => { row.heat_source = e.target.value; touched(); }));

    const lamp = document.createElement("input");
    lamp.type = "number"; lamp.min = 0; lamp.max = 1; lamp.step = 0.05;
    // Round for display: an optimiser happily emits 0.1157, which is noise to a
    // grower and makes the column unreadable.
    const lamps = (v) => Math.round(Math.min(1, Math.max(0, v)) * 100) / 100;
    row.lighting_level = lamps(row.lighting_level);
    lamp.value = row.lighting_level.toFixed(2);
    lamp.addEventListener("change", (e) => {
      row.lighting_level = lamps(parseFloat(e.target.value) || 0);
      e.target.value = row.lighting_level.toFixed(2);
      touched();
    });
    cell(lamp, "num");

    const batWrap = document.createElement("span");
    batWrap.appendChild(select(BATT, row.battery, (e) => { row.battery = e.target.value; touched(); }));
    const kw = document.createElement("input");
    kw.type = "number"; kw.min = 0; kw.step = 50; kw.value = row.battery_power_kw;
    kw.addEventListener("change", (e) => {
      row.battery_power_kw = Math.max(0, parseFloat(e.target.value) || 0);
      e.target.value = row.battery_power_kw;
      touched();
    });
    batWrap.appendChild(kw);
    cell(batWrap);

    cell(select(CHP, row.chp_mode, (e) => { row.chp_mode = e.target.value; touched(); }));
    cell(select(CO2, row.co2_source, (e) => { row.co2_source = e.target.value; touched(); }));
    cell(row.reasoning || "", "why");

    body.appendChild(tr);
  });
}

/** An edited plan cannot be approved until the checker has seen it (R23). */
function markVerified(ok) {
  state.verified = ok;
  state.dirty = !ok;
  $("reverify").disabled = ok;
  $("approve").disabled = !ok;
  $("decision-note").textContent = ok
    ? ""
    : "Plan edited. Re-verify before approving — an edit is checked like any other plan.";
}

/* ------------------------------------------------------------- actions */

async function run() {
  busy(true);
  try {
    renderOutcome(await api("/api/run", { overrides: overrides() }));
    $("comparison").hidden = true;
  } catch (e) { showError(e); } finally { busy(false); }
}

async function reverify() {
  busy(true);
  try {
    const r = await api("/api/verify", { overrides: overrides(), plan: state.plan });
    const badge = $("verdict-badge");
    badge.textContent = r.accepted ? "accepted" : "rejected";
    badge.className = "badge " + (r.accepted ? "ok" : "no");
    $("verdict-text").textContent = r.feedback;
    $("stat-cost").textContent = eur(r.metrics.net_cost_eur);
    $("stat-cost-m2").textContent = `€ ${num(r.metrics.net_cost_eur_per_m2, 3)} per m²`;
    $("verdict-note").textContent = "Re-verified after your edits.";
    markVerified(true);
    $("approve").disabled = !r.accepted;
    if (!r.accepted) {
      $("decision-note").textContent = "The edited plan breaks a limit, so it cannot be approved.";
    }
  } catch (e) {
    // Never leave the previous verdict standing after a failed re-verification:
    // an operator seeing "accepted" for a plan that was not re-checked is the
    // worst possible failure mode for this screen.
    const badge = $("verdict-badge");
    badge.textContent = "not verified";
    badge.className = "badge no";
    $("verdict-text").textContent =
      "Re-verification failed, so the previous verdict no longer applies to this plan.";
    markVerified(false);
    showError(e);
  } finally { busy(false); }
}

async function decide(decision) {
  try {
    await api("/api/decision", {
      decision,
      comment: $("comment").value,
      seconds_to_decide: state.shownAt
        ? Math.round((performance.now() - state.shownAt) / 100) / 10
        : null,
      overrides: overrides(),
    });
    $("decision-note").textContent =
      decision === "approve"
        ? "Approved and written to the audit log."
        : "Rejected and written to the audit log.";
    $("comment").value = "";
  } catch (e) { showError(e); }
}

async function compare() {
  busy(true);
  $("compare").textContent = "Comparing…";
  try {
    const r = await api("/api/compare", {
      overrides: overrides(),
      planners: ["rule-based", "learned", "naive"],
    });
    const body = document.querySelector("#compare-table tbody");
    body.innerHTML = "";
    const ok = r.rows.filter((x) => !x.error).map((x) => x.cost_eur);
    const best = ok.length ? Math.min(...ok) : null;

    for (const row of r.rows) {
      const tr = document.createElement("tr");
      if (row.error) {
        tr.innerHTML = `<td>${row.planner}</td><td colspan="6" class="muted">${row.error}</td>`;
      } else {
        const flags = [];
        if (row.fell_back) flags.push("fell back to baseline");
        if (row.cost_eur === best) flags.push("cheapest");
        tr.innerHTML =
          `<td>${row.planner}</td>` +
          `<td class="num">${eur(row.cost_eur)}</td>` +
          `<td class="num">${row.hard_violations}</td>` +
          `<td class="num">${num(row.growth_kg_m2, 3)}</td>` +
          `<td class="num">${num(row.band_hours)} h</td>` +
          `<td class="num">${num(row.peak_import_kw)} kW</td>` +
          `<td class="muted small">${flags.join(" · ")}</td>`;
      }
      body.appendChild(tr);
    }
    $("comparison").hidden = false;
    $("comparison").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) { showError(e); } finally {
    busy(false);
    $("compare").textContent = "Compare planners";
  }
}

/* ----------------------------------------------------------------- init */

$("run").addEventListener("click", run);
$("compare").addEventListener("click", compare);
$("reverify").addEventListener("click", reverify);
$("approve").addEventListener("click", () => decide("approve"));
$("reject").addEventListener("click", () => decide("reject"));
$("reset").addEventListener("click", async () => {
  buildSettings(await api("/api/settings"));
});

api("/api/settings").then(buildSettings).catch(showError);
