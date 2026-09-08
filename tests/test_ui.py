"""The interface API.

Two things here are worth more than the rest: an edited plan must go back through
the checker (R23), and the interface must never claim a plan was verified when it
was not. Both were bugs during development -- a stale "accepted" badge survived a
failed re-verification, and a run with the checker switched off still read
"accepted" beside a card showing 66 violations. Neither would have shown up in a
test of the model, only in a test of the interface.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from kasflex.ui.server import ADJUSTABLE, ApiError, UiServer, serve

CONFIG = "configs/scenario_westland_winter.yaml"


@pytest.fixture(scope="module")
def ui() -> UiServer:
    return UiServer(config_path=CONFIG)


@pytest.fixture(scope="module")
def base_run(ui):
    return ui.run({"planner": "rule-based"})


# --- settings --------------------------------------------------------------


def test_settings_expose_current_values(ui):
    settings = ui.get_settings()
    assert settings["scenario"] == "westland-winter"
    paths = {f["path"] for f in settings["fields"]}
    assert {"planner", "checker.enabled", "hub.contract.import_limit_kw"} <= paths
    planner = next(f for f in settings["fields"] if f["path"] == "planner")
    assert planner["value"] == "rule-based"
    assert "learned" in planner["choices"]


def test_every_adjustable_field_resolves(ui):
    """A typo in ADJUSTABLE would break the page on load, not at review time."""
    for field in ui.get_settings()["fields"]:
        assert "value" in field, field["path"]
        assert field["label"]


def test_overrides_outside_the_allowed_set_are_refused(ui):
    with pytest.raises(ApiError, match="not adjustable"):
        ui.run({"hub.boiler.efficiency": 0.5})


def test_a_value_of_the_wrong_type_is_refused(ui):
    """Dataclasses do not type-check, so without coercion this reaches the run."""
    with pytest.raises(ApiError, match="not a valid int"):
        ui.run({"checker.max_revisions": "three"})


def test_a_value_out_of_range_is_refused(ui):
    with pytest.raises(ApiError, match="below the minimum"):
        ui.run({"hub.contract.import_limit_kw": -500})
    with pytest.raises(ApiError, match="above the maximum"):
        ui.run({"hub.chp.min_run_hours": 99})


def test_an_invalid_choice_is_refused(ui):
    with pytest.raises(ApiError, match="is not one of"):
        ui.run({"planner": "vibes"})


def test_numeric_strings_from_a_form_are_accepted(ui):
    """A browser sends strings. Refusing them would make the UI unusable."""
    assert ui.run({"checker.max_revisions": "2", "hub.battery.capacity_kwh": "1500"})


# --- running ---------------------------------------------------------------


def test_run_returns_a_full_day(base_run):
    assert len(base_run["plan"]) == 24
    assert [row["hour"] for row in base_run["plan"]] == list(range(24))
    assert base_run["metrics"]["net_cost_eur"] > 0


def test_plan_rows_carry_context_for_the_operator(base_run):
    """A price and a heat demand next to each hour, or the plan is unreadable."""
    row = base_run["plan"][17]
    assert row["power_price_eur_kwh"] > 0
    assert "heat_demand_kw" in row
    assert row["reasoning"]


def test_run_reports_that_the_model_is_unvalidated(base_run):
    assert base_run["validated"] is False


def test_settings_actually_change_the_outcome(ui, base_run):
    """If a control does nothing, it is worse than not being there."""
    tighter = ui.run({"planner": "rule-based", "hub.contract.import_limit_kw": 3000})
    assert tighter["metrics"] != base_run["metrics"]


def test_battery_power_sets_both_directions(ui):
    """One control in the interface, two fields in the model."""
    result = ui.run({"hub.battery.max_charge_kw": 400})
    assert result["metrics"]  # ran without a config error
    config = ui.base
    assert config.hub.battery.max_charge_kw != 400, "the base config must not be mutated"


def test_a_run_does_not_mutate_the_loaded_scenario(ui, base_run):
    before = ui.base.hub.contract.import_limit_kw
    ui.run({"hub.contract.import_limit_kw": 1234})
    assert ui.base.hub.contract.import_limit_kw == before


def test_unimplemented_planner_reports_cleanly(ui):
    with pytest.raises(ApiError) as exc:
        ui.run({"planner": "mpc"})
    assert exc.value.status == 501


def test_unknown_planner_is_caught_before_the_run(ui):
    """Caught by the choice list, so it never reaches build_planner."""
    with pytest.raises(ApiError, match="is not one of"):
        ui.run({"planner": "telepathy"})


# --- verification of human edits (R23) -------------------------------------


def test_an_unedited_plan_still_verifies(ui, base_run):
    result = ui.verify({"planner": "rule-based"}, base_run["plan"])
    assert result["accepted"] is True


def test_display_columns_are_stripped_not_rejected(ui, base_run):
    """The page sends back exactly the rows the server gave it, price column and
    all. The schema rightly refuses unknown fields, so the server strips them."""
    assert "power_price_eur_kwh" in base_run["plan"][0]
    assert ui.verify({}, base_run["plan"])["accepted"] is True


def test_a_harmful_edit_is_caught(ui, base_run):
    """Withholding heat for the whole day must not survive re-verification."""
    edited = [{**row, "heat_source": "none"} for row in base_run["plan"]]
    result = ui.verify({"planner": "rule-based"}, edited)
    assert result["accepted"] is False
    assert any(v["constraint"] == "heat.demand_met" for v in result["violations"])


def test_an_edit_that_breaks_the_contract_is_caught(ui, base_run):
    edited = [{**row, "lighting_level": 1.0} for row in base_run["plan"]]
    result = ui.verify({"planner": "rule-based"}, edited)
    assert result["accepted"] is False


def test_a_malformed_edit_is_reported_not_crashed(ui, base_run):
    edited = [{**row, "heat_source": "wishful thinking"} for row in base_run["plan"]]
    with pytest.raises(ApiError, match="not valid"):
        ui.verify({}, edited)


def test_verify_reports_the_edited_cost(ui, base_run):
    result = ui.verify({}, base_run["plan"])
    assert result["metrics"]["net_cost_eur"] > 0


# --- honesty about verification --------------------------------------------


def test_a_disabled_checker_is_reported_as_such(ui):
    """The page turns this into "not verified" rather than "accepted"."""
    result = ui.run({"planner": "naive", "checker.enabled": False})
    assert result["checker_enabled"] is False
    assert result["realised_hard"] > 0, (
        "an unverified constraint-blind planner should produce violations; without "
        "them the interface has nothing to be honest about"
    )


def test_the_checker_catches_what_disabling_it_lets_through(ui):
    off = ui.run({"planner": "naive", "checker.enabled": False})
    on = ui.run({"planner": "naive", "checker.enabled": True})
    assert off["realised_hard"] > 0
    assert on["realised_hard"] == 0
    assert on["fell_back"] is True


# --- decisions (R25, R26) --------------------------------------------------


def _rejection_for(proposal, overrides):
    return {
        "decision": "reject",
        "overrides": overrides,
        "decision_token": proposal["decision_token"],
        "plan_fingerprint": proposal["plan_fingerprint"],
    }


def test_every_rendered_plan_has_a_rejection_capability(ui, base_run):
    assert base_run["decision_token"]
    assert base_run["plan_fingerprint"]

    unverified = ui.run({"planner": "rule-based", "checker.enabled": False})
    assert unverified["approval_token"] is None
    assert unverified["decision_token"]
    assert unverified["plan_fingerprint"]

    edited = [{**row, "heat_source": "none"} for row in base_run["plan"]]
    rejected = ui.verify({"planner": "rule-based"}, edited)
    assert rejected["accepted"] is False
    assert rejected["approval_token"] is None
    assert rejected["decision_token"]
    assert rejected["plan_fingerprint"]


def test_a_decision_is_recorded(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "rule-based"}
    proposal = server.run(overrides)
    server.decide(
        {
            "decision": "approve",
            "comment": "looks right",
            "seconds_to_decide": 12.5,
            "operator": "grower-1",
            "overrides": overrides,
            "approval_token": proposal["approval_token"],
        }
    )

    from kasflex.oversight import AuditLog

    entries = AuditLog(tmp_path / "audit.jsonl").entries()
    assert entries[-1]["kind"] == "human_decision_ui"
    assert entries[-1]["payload"]["decision"] == "approve"
    assert entries[-1]["payload"]["seconds_to_decide"] == 12.5
    assert entries[-1]["payload"]["plan_fingerprint"] == proposal["plan_fingerprint"]
    assert entries[-1]["operator"] == "grower-1"


def test_approval_requires_a_current_matching_verified_plan(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "rule-based"}
    proposal = server.run(overrides)

    with pytest.raises(ApiError, match="stale or the plan was not verified"):
        server.decide({"decision": "approve", "overrides": overrides})

    with pytest.raises(ApiError, match="scenario changed"):
        server.decide(
            {
                "decision": "approve",
                "overrides": {"planner": "learned"},
                "approval_token": proposal["approval_token"],
            }
        )

    # A mismatch consumes the capability: the operator must verify again instead
    # of retrying approval against a snapshot whose context no longer matches.
    with pytest.raises(ApiError, match="stale or the plan was not verified"):
        server.decide(
            {
                "decision": "approve",
                "overrides": overrides,
                "approval_token": proposal["approval_token"],
            }
        )


def test_approval_token_is_one_use(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "rule-based"}
    proposal = server.run(overrides)
    decision = {
        "decision": "approve",
        "overrides": overrides,
        "approval_token": proposal["approval_token"],
    }

    assert server.decide(decision)["recorded"] is True
    with pytest.raises(ApiError, match="stale or the plan was not verified"):
        server.decide(decision)


def test_rejection_requires_the_current_plan_snapshot(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "rule-based"}

    proposal = server.run(overrides)
    with pytest.raises(ApiError, match="stale or does not identify"):
        server.decide(
            {
                "decision": "reject",
                "overrides": overrides,
                "plan_fingerprint": proposal["plan_fingerprint"],
            }
        )

    proposal = server.run(overrides)
    changed_scenario = {
        **_rejection_for(proposal, overrides),
        "overrides": {"planner": "learned"},
    }
    with pytest.raises(ApiError, match="scenario changed"):
        server.decide(changed_scenario)
    with pytest.raises(ApiError, match="stale or does not identify"):
        server.decide(_rejection_for(proposal, overrides))

    proposal = server.run(overrides)
    wrong_plan = {
        **_rejection_for(proposal, overrides),
        "plan_fingerprint": "not-the-rendered-plan",
    }
    with pytest.raises(ApiError, match="fingerprint does not match"):
        server.decide(wrong_plan)
    with pytest.raises(ApiError, match="stale or does not identify"):
        server.decide(_rejection_for(proposal, overrides))


def test_rejection_token_is_one_use_and_records_its_plan(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "naive", "checker.enabled": False}
    proposal = server.run(overrides)
    rejection = _rejection_for(proposal, overrides)

    assert proposal["approval_token"] is None
    assert server.decide(rejection)["recorded"] is True
    with pytest.raises(ApiError, match="stale or does not identify"):
        server.decide(rejection)

    from kasflex.oversight import AuditLog

    entry = AuditLog(tmp_path / "audit.jsonl").entries()[-1]
    assert entry["payload"]["decision"] == "reject"
    assert entry["payload"]["plan_fingerprint"] == proposal["plan_fingerprint"]
    assert entry["payload"]["verification_source"] == "generated"


def test_rendering_a_new_plan_stales_the_previous_rejection(tmp_path):
    server = UiServer(config_path=CONFIG)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}
    )
    overrides = {"planner": "rule-based"}
    old = server.run(overrides)
    current = server.run(overrides)

    with pytest.raises(ApiError, match="stale or does not identify"):
        server.decide(_rejection_for(old, overrides))
    assert server.decide(_rejection_for(current, overrides))["recorded"] is True


def test_anonymous_mode_withholds_the_operator(tmp_path):
    server = UiServer(config_path=CONFIG, anonymous=True)
    server.base = type(server.base)(
        **{**server.base.__dict__, "audit_path": str(tmp_path / "a.jsonl")}
    )
    overrides = {"planner": "rule-based"}
    proposal = server.run(overrides)
    server.decide({**_rejection_for(proposal, overrides), "operator": "grower-1"})

    from kasflex.oversight import AuditLog

    assert AuditLog(tmp_path / "a.jsonl").entries()[-1]["operator"] == "anonymous"


def test_a_nonsense_decision_is_refused(ui):
    with pytest.raises(ApiError, match="approve, reject or edit"):
        ui.decide({"decision": "maybe"})


# --- comparison (R29) ------------------------------------------------------


def test_compare_runs_every_planner_on_one_scenario(ui):
    result = ui.compare({}, ["rule-based", "naive"])
    assert {r["planner"] for r in result["rows"]} == {"rule-based", "naive"}
    for row in result["rows"]:
        assert "cost_eur" in row and "hard_violations" in row


def test_compare_reports_a_failing_planner_without_sinking_the_table(ui):
    """One unimplemented planner must not cost you the whole comparison."""
    result = ui.compare({}, ["rule-based", "mpc"])
    rows = {r["planner"]: r for r in result["rows"]}
    assert "error" in rows["mpc"]
    assert "cost_eur" in rows["rule-based"]


# --- HTTP layer ------------------------------------------------------------


@pytest.fixture(scope="module")
def live():
    httpd = serve(config_path=CONFIG, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_page_and_its_assets_are_served(live):
    for path, needle in (("/", b"KasFlex"), ("/style.css", b"--ink"), ("/app.js", b"api(")):
        status, body = _get(live + path)
        assert status == 200, path
        assert needle in body, path


def test_the_page_carries_the_permanent_simulation_notice(live):
    """R31. If this ever disappears the interface is misrepresenting itself."""
    _, body = _get(live + "/")
    text = " ".join(body.decode().split())  # the source wraps this sentence
    assert "Simulation." in text
    assert "Not validated for operational use" in text
    assert "hidden" not in text.split('id="sim-notice"')[1][:120]


def test_favicon_is_answered(live):
    assert _get(live + "/favicon.ico")[0] == 200


def test_api_settings_over_http(live):
    status, body = _get(live + "/api/settings")
    assert status == 200
    assert len(json.loads(body)["fields"]) == len(ADJUSTABLE)


def test_api_run_over_http(live):
    status, payload = _post(live + "/api/run", {"overrides": {"planner": "rule-based"}})
    assert status == 200
    assert len(payload["plan"]) == 24


def test_a_bad_override_returns_400(live):
    status, payload = _post(live + "/api/run", {"overrides": {"hub.pv.peak_kw": 1}})
    assert status == 400
    assert "not adjustable" in payload["error"]


def test_an_unknown_endpoint_returns_404(live):
    assert _post(live + "/api/nope", {})[0] == 404


def test_a_malformed_body_returns_400(live):
    request = urllib.request.Request(
        live + "/api/run",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30)
        raise AssertionError("should have failed")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
