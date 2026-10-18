from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.models import OutageRequest, SimulationConfig
from app.simulator import generate_profiles, run_outage_test, run_simulation


def sample_config(**overrides) -> SimulationConfig:
    data = SimulationConfig().model_dump()
    data.update(overrides)
    return SimulationConfig.model_validate(data)


def test_profile_generation_is_deterministic_and_15_minute():
    cfg = sample_config(days=1, random_seed=123)
    first = generate_profiles(cfg)
    assert first == generate_profiles(cfg)
    assert len(first) == 96
    assert first[0]["timestamp"].endswith("00:00")
    assert first[1]["timestamp"].endswith("00:15")


def test_result_contains_all_strategy_paths_and_benchmark():
    result = run_simulation(sample_config())
    assert set(result["strategies"]) == {
        "solar_only", "rule_based", "forecast_mpc", "perfect_foresight", "global_optimizer"
    }
    assert result["interval_count"] == 96
    assert len(result["series"]) == 96
    assert "forecasting_regret_inr_est" in result["forecasting_benchmark"]
    assert result["optimizer"]["full_horizon_milp"]["solver_success"] is True
    for strategy in result["strategies"].values():
        assert strategy["metrics"]["total_cost_inr"] >= 0
        assert strategy["metrics"]["grid_import_kwh"] >= 0
        assert len(strategy["series"]) == 96


def test_soc_bounds_one_way_battery_and_energy_balance():
    cfg = sample_config(days=1, battery_capacity_kwh=300, battery_power_kw=100,
                        min_soc_pct=15, max_soc_pct=90, reserve_soc_pct=25)
    result = run_simulation(cfg)
    for key in ["rule_based", "forecast_mpc", "perfect_foresight", "global_optimizer"]:
        rows = result["strategies"][key]["series"]
        for row in rows:
            assert 24.9 <= row["battery_soc_pct"] <= 90.1
            assert not (row["battery_charge_kw"] > 1e-5 and row["battery_discharge_kw"] > 1e-5)
            assert math.isfinite(row["power_balance_error_kw"])
            assert abs(row["power_balance_error_kw"]) <= 0.15


def test_zero_battery_still_simulates_without_battery_dispatch():
    result = run_simulation(sample_config(battery_capacity_kwh=0, battery_power_kw=0, battery_inverter_kva=0))
    assert len(result["series"]) == 96
    for strategy in ["rule_based", "forecast_mpc", "perfect_foresight", "global_optimizer"]:
        m = result["strategies"][strategy]["metrics"]
        assert m["battery_discharge_kwh"] == 0
        assert m["battery_charge_kwh"] == 0


def test_month_to_date_demand_peak_is_carried_forward():
    cfg = sample_config(peak_kva_so_far_this_month=1800, contract_demand_kva=1200)
    result = run_simulation(cfg)
    metrics = result["strategies"]["forecast_mpc"]["metrics"]
    assert metrics["billing_peak_kva_month_to_date_est"] >= 1800
    assert metrics["demand_charge_increment_inr"] == 0
    assert metrics["projected_monthly_demand_charge_inr"] >= metrics["prior_month_demand_charge_basis_inr"]


def test_kva_and_pf_layer_are_present_and_sensible():
    result = run_simulation(sample_config(load_power_factor=0.82, battery_reactive_support_max_kvar=200))
    rows = result["strategies"]["forecast_mpc"]["series"]
    assert all(row["grid_import_kva"] + 1e-6 >= row["grid_import_kw"] for row in rows)
    assert all(0 <= row["grid_power_factor"] <= 1 for row in rows)
    assert all(row["battery_reactive_support_kvar"] >= 0 for row in rows)
    assert result["strategies"]["forecast_mpc"]["metrics"]["billed_average_power_factor_est"] <= 1


def test_outage_result_is_bounded_and_reports_start_soc_separately():
    result = run_simulation(sample_config())
    outage = run_outage_test(result, OutageRequest(start_hour=18, duration_hours=2, critical_load_pct=35))
    assert 0 <= outage["critical_energy_served_pct_est"] <= 100
    assert outage["unserved_critical_energy_kwh_est"] >= 0
    assert "Simulation only" in outage["result_label"]
    before = result["strategies"]["forecast_mpc"]["series"][int(18 * 4) - 1]["battery_soc_pct"]
    assert outage["battery_soc_at_outage_start_pct"] == pytest.approx(before, abs=0.02)


def test_outage_can_report_partial_horizon_at_end_of_day():
    result = run_simulation(sample_config(days=1))
    outage = run_outage_test(result, OutageRequest(start_hour=23.5, duration_hours=2))
    assert outage["duration_hours_simulated"] == pytest.approx(0.5)
    assert outage["duration_hours_requested"] == 2


def test_config_rejects_invalid_soc_relationships():
    with pytest.raises(ValidationError):
        sample_config(min_soc_pct=80, max_soc_pct=70)
    with pytest.raises(ValidationError):
        sample_config(initial_soc_pct=99, max_soc_pct=95)


def test_historical_tariff_is_labeled_as_historical_not_current():
    result = run_simulation(sample_config(tariff_preset="rajasthan_jvvnl_ht5_fy2024_25"))
    assert "historical" in result["tariff"]["selected_label"].lower()
    assert result["tariff"]["source_url"].startswith("https://")
    assert "historical" in " ".join(result["assumptions"]).lower()


def test_solver_exception_uses_rule_based_safe_fallback(monkeypatch):
    import app.simulator as simulator

    cfg = sample_config()
    profiles = generate_profiles(cfg)[:8]

    def fail_solver(*args, **kwargs):
        raise RuntimeError("simulated solver outage")

    monkeypatch.setattr(simulator, "milp", fail_solver)
    actions, info = simulator._solve_milp(profiles, cfg, time_limit_seconds=0.1)

    assert info["fallback_used"] is True
    assert "rule-based fallback used" in info["solver_status"]
    assert len(actions) == len(profiles)
    assert all(a["battery_charge_kw"] >= 0 and a["battery_discharge_kw"] >= 0 for a in actions)
