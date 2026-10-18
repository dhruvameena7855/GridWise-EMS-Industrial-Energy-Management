from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import math

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .models import SimulationConfig, OutageRequest


STRATEGY_LABELS = {
    "solar_only": "Solar + grid (no battery / DG)",
    "rule_based": "Rule-based safe controller",
    "forecast_mpc": "Forecast-driven rolling MPC with economic guard",
    "perfect_foresight": "Perfect-information MPC with economic guard",
    "global_optimizer": "Full-horizon MILP plan",
}

TARIFF_SOURCES = {
    "rajasthan_jvvnl_ht5_fy2024_25": {
        "label": "Rajasthan JVVNL HT-5 large industry (FY 2024–25 historical case study)",
        "source_name": "RERC order dated 26 July 2024, Petition Nos. RERC 2206/2024, 2207/2024 and 2208/2024",
        "source_url": "https://jankalyanfile.rajasthan.gov.in/Content/UploadFolder/OrderEntry/J_V_V_N_L_/2024/Order/O_140824_d1743273-3c6f-4632-8d0e-039ab77e1f91.pdf",
        "energy_rate_inr_kwh": 7.30,
        "demand_charge_inr_kva_month": 300.0,
        "note": "Historical rate reference for the named category/voltage shown in the order. Verify the latest applicable tariff and other bill components before using it for real decisions.",
    },
    "illustrative_tou": {
        "label": "Illustrative time-of-day tariff (not an official utility tariff)",
        "source_name": "User-configurable demonstration assumptions",
        "source_url": None,
        "energy_rate_inr_kwh": None,
        "demand_charge_inr_kva_month": None,
        "note": "Peak/off-peak rates are simulated inputs, not claimed to be an actual utility tariff.",
    },
}


def tariff_values(cfg: SimulationConfig) -> dict[str, Any]:
    if cfg.tariff_preset == "rajasthan_jvvnl_ht5_fy2024_25":
        return {
            "off_peak": 7.30, "normal": 7.30, "peak": 7.30,
            "export": 0.0, "demand_rate": 300.0,
            "label": TARIFF_SOURCES[cfg.tariff_preset]["label"],
            "source_url": TARIFF_SOURCES[cfg.tariff_preset]["source_url"],
        }
    return {
        "off_peak": cfg.off_peak_rate_inr_kwh, "normal": cfg.normal_rate_inr_kwh,
        "peak": cfg.peak_rate_inr_kwh, "export": cfg.export_rate_inr_kwh,
        "demand_rate": cfg.demand_charge_inr_kva_month,
        "label": TARIFF_SOURCES[cfg.tariff_preset]["label"], "source_url": None,
    }


def _rate_for_hour(hour: float, cfg: SimulationConfig) -> float:
    rates = tariff_values(cfg)
    if cfg.tariff_preset == "rajasthan_jvvnl_ht5_fy2024_25":
        return float(rates["normal"])
    if 0 <= hour < 6:
        return float(rates["off_peak"])
    if 17 <= hour < 21:
        return float(rates["peak"])
    return float(rates["normal"])


def _pf_tan(cfg: SimulationConfig) -> float:
    return math.tan(math.acos(float(np.clip(cfg.load_power_factor, 0.5, 1.0))))


def battery_inverter_efficiency_at_kw(power_kw: float, cfg: SimulationConfig) -> float:
    """A transparent 3-segment conversion-efficiency curve, interpolated by utilization."""
    if cfg.battery_inverter_kva <= 0 or power_kw <= 1e-9:
        return cfg.battery_inverter_efficiency_min
    utilization = float(np.clip(power_kw / cfg.battery_inverter_kva, 0.0, 1.0))
    x = np.array([0.0, 0.25, 0.75, 1.0])
    y = np.array([
        cfg.battery_inverter_efficiency_min,
        max(cfg.battery_inverter_efficiency_min, 0.94),
        min(cfg.battery_inverter_efficiency_rated, 0.96),
        cfg.battery_inverter_efficiency_rated,
    ])
    # Enforce monotonicity when a user changes assumptions.
    y = np.maximum.accumulate(y)
    return float(np.interp(utilization, x, y))


def _temperature_derate(temp_c: float, cfg: SimulationConfig) -> float:
    excess = max(0.0, temp_c - cfg.battery_temperature_derate_start_c)
    return float(np.clip(1.0 - excess * cfg.battery_temperature_derate_pct_per_c / 100.0, 0.35, 1.0))


def _effective_battery_kw(cfg: SimulationConfig, temp_c: float) -> float:
    if cfg.battery_capacity_kwh <= 0 or cfg.battery_inverter_kva <= 0:
        return 0.0
    return max(0.0, min(cfg.battery_power_kw, cfg.battery_capacity_kwh * cfg.battery_c_rate,
                        cfg.battery_inverter_kva) * _temperature_derate(temp_c, cfg))


def generate_profiles(cfg: SimulationConfig) -> list[dict[str, Any]]:
    """Generate deterministic 15-minute synthetic profiles for benchmarking."""
    rng = np.random.default_rng(cfg.random_seed)
    steps_per_day = 96
    count = cfg.days * steps_per_day
    start = datetime(2026, 1, 5, 0, 0)  # fixed Monday to keep results reproducible.
    solar_ac_rating = cfg.solar_capacity_dc_kw / max(cfg.solar_dc_ac_ratio, 1.0)
    profiles: list[dict[str, Any]] = []

    for i in range(count):
        ts = start + timedelta(minutes=15 * i)
        hour = ts.hour + ts.minute / 60.0
        weekday = ts.weekday() < 5
        ambient = 24.0 + 8.0 * max(0.0, math.sin(math.pi * (hour - 6.0) / 12.5)) + 1.8 * math.sin(i / 24.0)

        if cfg.industry_preset == "jio_datacenter":
            # Flat 24x7 critical base with a modest cooling peak.
            load_base = 720.0 + 38.0 * math.sin((hour - 13.0) * math.pi / 12.0) + max(0.0, ambient - 28.0) * 5.0
            load = load_base + rng.normal(0.0, 8.0)
            critical_share = 0.92
        elif cfg.industry_preset == "refinery_utilities":
            # Process utilities: high base load with pump/compressor cycle variation.
            pump_cycle = 95.0 if int(hour) in (7, 8, 14, 15, 20, 21) else 20.0
            load = 420.0 + pump_cycle + 24.0 * math.sin(hour * math.pi / 6.0) + rng.normal(0.0, 10.0)
            critical_share = 0.65
        else:
            base = 155.0 if weekday else 125.0
            morning = 115.0 * math.exp(-0.5 * ((hour - 9.5) / 2.0) ** 2)
            afternoon = 165.0 * math.exp(-0.5 * ((hour - 14.0) / 3.0) ** 2)
            evening = 75.0 * math.exp(-0.5 * ((hour - 19.0) / 2.2) ** 2)
            process_cycle = 18.0 * math.sin((i / steps_per_day) * 2 * math.pi + 0.6)
            load = base + morning + afternoon + evening + process_cycle + rng.normal(0, 7.5)
            critical_share = 0.35
        load = max(20.0, float(load) * cfg.factory_load_scale)

        solar_dc_raw = 0.0
        solar_ac_preclip = 0.0
        solar_ac = 0.0
        if 6.0 <= hour <= 18.5 and cfg.solar_capacity_dc_kw > 0:
            daylight = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.5))
            day_factor = 0.82 + 0.12 * math.sin((ts.timetuple().tm_yday + 3) * 0.63)
            cloud_factor = float(np.clip(day_factor + rng.normal(0.0, 0.07), 0.25, 1.0))
            solar_dc_raw = cfg.solar_capacity_dc_kw * daylight ** 1.35 * cloud_factor
            solar_ac_preclip = solar_dc_raw * cfg.solar_inverter_efficiency
            solar_ac = min(solar_ac_preclip, solar_ac_rating)
        clipped = max(0.0, solar_ac_preclip - solar_ac)

        profiles.append({
            "timestamp": ts.isoformat(timespec="minutes"),
            "hour": round(hour, 3),
            "load_kw": round(max(0.0, load), 3),
            "critical_load_share_pct": round(critical_share * 100.0, 1),
            "solar_dc_kw": round(solar_dc_raw, 3),
            "solar_kw": round(solar_ac, 3),
            "solar_clipped_kw": round(clipped, 3),
            "solar_ac_rating_kw": round(solar_ac_rating, 3),
            "ambient_temperature_c": round(ambient, 2),
            "import_rate_inr_kwh": round(_rate_for_hour(hour, cfg), 3),
        })
    return profiles


def _add_reason(row: dict[str, Any], p: dict[str, Any], cfg: SimulationConfig) -> str:
    if row.get("unserved_load_kw", 0.0) > 0.01:
        return "Safe-mode warning: grid/transformer limit caused unserved load; operator review required"
    if row.get("dg_on", 0.0) >= 0.5:
        return "DG committed to support load while respecting minimum run-time logic"
    if row.get("battery_charge_kw", 0.0) > 0.1:
        if p["solar_kw"] > p["load_kw"]:
            return "Charged from available solar surplus while battery headroom was available"
        if p["hour"] < 6:
            return "Charged during the configured off-peak window to rebuild energy reserve"
        return "Charged because the planned future value of stored energy exceeded current use"
    if row.get("battery_discharge_kw", 0.0) > 0.1:
        if p["hour"] >= 17 and p["hour"] < 21:
            return "Discharged during the configured peak-price window / demand peak"
        return "Discharged to reduce grid import while protecting the minimum reserve"
    if row.get("battery_reactive_support_kvar", 0.0) > 1.0:
        return "Provided inverter reactive support to improve power factor and reduce kVA demand"
    return "Held battery reserve; forecast savings did not justify additional cycling"


def _normalize_action(action: dict[str, Any]) -> dict[str, float]:
    return {key: float(action.get(key, 0.0) or 0.0) for key in (
        "battery_charge_kw", "battery_discharge_kw", "battery_reactive_support_kvar",
        "dg_power_kw", "dg_on", "grid_import_kw", "grid_export_kw", "curtailed_solar_kw",
        "unserved_load_kw", "solar_to_battery_kw",
    )}


def _replay_actions(
    profiles: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    cfg: SimulationConfig,
    *,
    initial_energy_kwh: float | None = None,
    initial_prev_battery_net_kw: float = 0.0,
    initial_dg_on: bool | None = None,
    initial_dg_min_up_remaining_steps: int = 0,
) -> list[dict[str, Any]]:
    """Replay planned controls on supplied actual data, enforcing hard operating limits."""
    dt = cfg.interval_minutes / 60.0
    cap = cfg.battery_capacity_kwh
    min_soc = max(cfg.min_soc_pct, cfg.reserve_soc_pct)
    min_e = cap * min_soc / 100.0
    max_e = cap * cfg.max_soc_pct / 100.0
    energy = cap * cfg.initial_soc_pct / 100.0 if initial_energy_kwh is None else float(initial_energy_kwh)
    energy = float(np.clip(energy, min_e if cap > 0 else 0.0, max_e))
    prev_net = float(initial_prev_battery_net_kw)
    dg_on_state = cfg.dg_initially_on if initial_dg_on is None else bool(initial_dg_on)
    dg_minup_remaining = max(0, int(initial_dg_min_up_remaining_steps))
    dg_min_up_steps = max(1, int(math.ceil(cfg.dg_min_up_minutes / cfg.interval_minutes)))
    rows: list[dict[str, Any]] = []
    rates = tariff_values(cfg)
    tan_phi = _pf_tan(cfg)

    for p, raw_action in zip(profiles, actions):
        action = _normalize_action(raw_action)
        temp = float(p.get("ambient_temperature_c", 25.0))
        derate = _temperature_derate(temp, cfg)
        pmax = _effective_battery_kw(cfg, temp)
        desired_net = action["battery_discharge_kw"] - action["battery_charge_kw"]
        ramp = cfg.battery_ramp_kw_per_minute * cfg.interval_minutes
        net = float(np.clip(desired_net, prev_net - ramp, prev_net + ramp))
        net = float(np.clip(net, -pmax, pmax))
        charge = max(0.0, -net)
        discharge = max(0.0, net)

        inv_eff = battery_inverter_efficiency_at_kw(abs(net), cfg)
        eta_c = cfg.charge_efficiency * inv_eff
        eta_d = cfg.discharge_efficiency * inv_eff
        if cap <= 0 or pmax <= 0:
            charge = discharge = 0.0
            net = 0.0
            inv_eff = cfg.battery_inverter_efficiency_min
            eta_c = cfg.charge_efficiency * inv_eff
            eta_d = cfg.discharge_efficiency * inv_eff
        if charge > 0 and cap > 0:
            charge = min(charge, max(0.0, (max_e - energy) / max(eta_c * dt, 1e-9)))
            energy += charge * eta_c * dt
            discharge = 0.0
        elif discharge > 0 and cap > 0:
            available = max(0.0, energy - min_e) * eta_d / dt
            discharge = min(discharge, available)
            energy -= discharge * dt / max(eta_d, 1e-9)
            charge = 0.0
        net = discharge - charge
        prev_net = net
        energy = float(np.clip(energy, min_e if cap > 0 else 0.0, max_e))

        q_load_full = max(0.0, p["load_kw"]) * tan_phi
        inv_kva_available = math.sqrt(max(0.0, cfg.battery_inverter_kva ** 2 - net ** 2)) if cfg.battery_inverter_kva > 0 else 0.0
        q_support = min(
            max(0.0, action["battery_reactive_support_kvar"]),
            cfg.battery_reactive_support_max_kvar,
            inv_kva_available,
            q_load_full,
        )

        # Honour generator minimum up-time when already committed.
        requested_dg_on = action["dg_on"] >= 0.5
        if not cfg.dg_enabled or cfg.dg_capacity_kw <= 0:
            dg_on = False
            dg_kw = 0.0
            dg_minup_remaining = 0
        else:
            if dg_on_state and dg_minup_remaining > 0:
                requested_dg_on = True
            dg_on = requested_dg_on
            if dg_on and not dg_on_state:
                dg_minup_remaining = max(0, dg_min_up_steps - 1)
            elif dg_on and dg_minup_remaining > 0:
                dg_minup_remaining -= 1
            elif not dg_on:
                dg_minup_remaining = 0
            dg_kw = float(np.clip(action["dg_power_kw"], 0.0, cfg.dg_capacity_kw)) if dg_on else 0.0
            if dg_on:
                min_dg_kw = cfg.dg_capacity_kw * cfg.dg_min_loading_pct / 100.0
                dg_kw = min(cfg.dg_capacity_kw, max(min_dg_kw, dg_kw))
        dg_on_state = dg_on

        # Solve active-power balance, including configured grid and transformer limits.
        net_need = p["load_kw"] - p["solar_kw"] + charge - discharge - dg_kw
        imp = exp = curtail = unserved = 0.0
        if net_need >= 0:
            # Iteratively estimate transformer-limited import while load shedding lowers Q demand.
            imp_guess = net_need
            for _ in range(5):
                served_guess = max(0.0, p["load_kw"] - max(0.0, net_need - imp_guess))
                q_grid_guess = max(0.0, served_guess * tan_phi - q_support)
                xfmr_limit = math.sqrt(max(0.0, cfg.transformer_rating_kva ** 2 - q_grid_guess ** 2)) if cfg.transformer_rating_kva is not None else float("inf")
                imp_limit = min(xfmr_limit, cfg.grid_import_limit_kw if cfg.grid_import_limit_kw is not None else float("inf"))
                imp_guess = min(net_need, imp_limit)
            imp = max(0.0, imp_guess)
            unserved = max(0.0, net_need - imp)
            served_load = max(0.0, p["load_kw"] - unserved)
            q_load_served = served_load * tan_phi
        else:
            surplus = -net_need
            if cfg.export_enabled:
                exp = min(surplus, cfg.max_grid_export_kw)
            curtail = max(0.0, surplus - exp)
            served_load = p["load_kw"]
            q_load_served = served_load * tan_phi

        q_grid = max(0.0, q_load_served - q_support)
        grid_kva = math.sqrt(imp ** 2 + q_grid ** 2) if imp > 1e-9 or q_grid > 1e-9 else 0.0
        pf = imp / grid_kva if grid_kva > 1e-9 else 1.0
        fuel_l = dg_kw * dt * cfg.dg_fuel_l_per_kwh
        row = {
            "grid_import_kw": imp,
            "grid_export_kw": exp,
            "grid_import_kvar": q_grid,
            "grid_import_kva": grid_kva,
            "grid_power_factor": float(np.clip(pf, 0.0, 1.0)),
            "battery_charge_kw": charge,
            "battery_discharge_kw": discharge,
            "battery_net_kw": net,
            "battery_energy_kwh": energy,
            "battery_soc_pct": energy / cap * 100.0 if cap > 0 else 0.0,
            "battery_reactive_support_kvar": q_support,
            "battery_inverter_efficiency_pct": inv_eff * 100.0,
            "temperature_derate_pct": derate * 100.0,
            "solar_to_battery_kw": min(charge, max(0.0, p["solar_kw"] - p["load_kw"])),
            "solar_used_kw": min(p["load_kw"], p["solar_kw"]) + min(charge, max(0.0, p["solar_kw"] - p["load_kw"])),
            "curtailed_solar_kw": curtail,
            "solar_clipped_kw": p.get("solar_clipped_kw", 0.0),
            "load_served_kw": max(0.0, p["load_kw"] - unserved),
            "unserved_load_kw": unserved,
            "dg_output_kw": dg_kw,
            "dg_on": 1.0 if dg_on else 0.0,
            "dg_fuel_l": fuel_l,
            "dg_min_up_remaining_steps": dg_minup_remaining,
            "power_balance_error_kw": (imp - exp - curtail - charge + discharge + dg_kw + unserved) - (p["load_kw"] - p["solar_kw"]),
        }
        row["decision_reason"] = _add_reason(row, p, cfg)
        rows.append(row)
    return rows


def _rule_actions(profiles: list[dict[str, Any]], cfg: SimulationConfig,
                  initial_energy_kwh: float | None = None, initial_dg_on: bool | None = None,
                  initial_dg_min_up_remaining_steps: int = 0,
                  initial_prev_battery_net_kw: float = 0.0) -> list[dict[str, float]]:
    dt = cfg.interval_minutes / 60.0
    cap = cfg.battery_capacity_kwh
    min_e = cap * max(cfg.min_soc_pct, cfg.reserve_soc_pct) / 100.0
    max_e = cap * cfg.max_soc_pct / 100.0
    energy = cap * cfg.initial_soc_pct / 100.0 if initial_energy_kwh is None else float(initial_energy_kwh)
    energy = max(min_e if cap > 0 else 0.0, min(max_e, energy))
    prev_net = float(initial_prev_battery_net_kw)
    dg_on = cfg.dg_initially_on if initial_dg_on is None else bool(initial_dg_on)
    dg_remaining = max(0, initial_dg_min_up_remaining_steps)
    min_up_steps = max(1, int(math.ceil(cfg.dg_min_up_minutes / cfg.interval_minutes)))
    actions: list[dict[str, float]] = []
    target_grid_kw = cfg.contract_demand_kva * cfg.load_power_factor

    for p in profiles:
        rate = p["import_rate_inr_kwh"]
        load, solar = p["load_kw"], p["solar_kw"]
        temp = p.get("ambient_temperature_c", 25.0)
        pmax = _effective_battery_kw(cfg, temp)
        charge = discharge = 0.0
        surplus = max(0.0, solar - load)
        deficit = max(0.0, load - solar)
        if cap > 0 and pmax > 0:
            if surplus > 1e-6 and energy < max_e:
                charge = min(surplus, pmax, (max_e - energy) / (cfg.charge_efficiency * cfg.battery_inverter_efficiency_min * dt))
            elif (cfg.tariff_preset == "illustrative_tou" and rate >= cfg.peak_rate_inr_kwh - 1e-6) or deficit > target_grid_kw:
                available = max(0.0, energy - min_e) * cfg.discharge_efficiency * cfg.battery_inverter_efficiency_min / dt
                target_excess = max(0.0, deficit - target_grid_kw * 0.88)
                peak_target = min(deficit, pmax) if (cfg.tariff_preset == "illustrative_tou" and rate >= cfg.peak_rate_inr_kwh - 1e-6) else target_excess
                discharge = min(pmax, available, max(target_excess, peak_target))
            elif p["hour"] < 6 and energy < max(min_e, max_e * 0.65) and rate <= cfg.normal_rate_inr_kwh:
                charge = min(pmax, (max_e * 0.65 - energy) / (cfg.charge_efficiency * cfg.battery_inverter_efficiency_min * dt))
        desired_net = discharge - charge
        ramp = cfg.battery_ramp_kw_per_minute * cfg.interval_minutes
        desired_net = float(np.clip(desired_net, prev_net - ramp, prev_net + ramp))
        desired_net = float(np.clip(desired_net, -pmax, pmax))
        charge, discharge = max(0.0, -desired_net), max(0.0, desired_net)
        inv_eff = battery_inverter_efficiency_at_kw(abs(desired_net), cfg)
        energy += charge * cfg.charge_efficiency * inv_eff * dt - (discharge * dt / max(cfg.discharge_efficiency * inv_eff, 1e-9))
        energy = float(np.clip(energy, min_e if cap > 0 else 0.0, max_e))
        prev_net = desired_net

        # Reactive capability shares the battery inverter's kVA circle with active power.
        qsupport = min(
            cfg.battery_reactive_support_max_kvar,
            load * _pf_tan(cfg),
            math.sqrt(max(0.0, cfg.battery_inverter_kva ** 2 - desired_net ** 2)) if cfg.battery_inverter_kva > 0 and cap > 0 else 0.0,
        )
        # Use DG when hard grid-import or transformer apparent-power limits would
        # otherwise be exceeded; an already committed DG honours its minimum up-time.
        projected_import = max(0.0, deficit - discharge + charge)
        q_grid_est = max(0.0, load * _pf_tan(cfg) - qsupport)
        transformer_active_limit = (
            math.sqrt(max(0.0, cfg.transformer_rating_kva ** 2 - q_grid_est ** 2))
            if cfg.transformer_rating_kva is not None else float("inf")
        )
        import_active_limit = cfg.grid_import_limit_kw if cfg.grid_import_limit_kw is not None else float("inf")
        allowed_grid_kw = min(transformer_active_limit, import_active_limit)
        wants_dg = bool(cfg.dg_enabled and cfg.dg_capacity_kw > 0 and projected_import > allowed_grid_kw + 1e-6)
        if dg_on and dg_remaining > 0:
            wants_dg = True
        if wants_dg and not dg_on:
            dg_remaining = min_up_steps - 1
        elif wants_dg and dg_remaining > 0:
            dg_remaining -= 1
        elif not wants_dg:
            dg_remaining = 0
        dg_on = wants_dg
        dg_power = min(cfg.dg_capacity_kw, max(cfg.dg_capacity_kw * cfg.dg_min_loading_pct / 100.0, projected_import - allowed_grid_kw)) if dg_on else 0.0
        actions.append({
            "battery_charge_kw": max(0.0, charge), "battery_discharge_kw": max(0.0, discharge),
            "battery_reactive_support_kvar": qsupport, "dg_power_kw": dg_power,
            "dg_on": 1.0 if dg_on else 0.0,
        })
    return actions


def _solve_milp(
    profiles: list[dict[str, Any]], cfg: SimulationConfig, *,
    initial_energy_kwh: float | None = None,
    previous_peak_kva: float | None = None,
    initial_dg_on: bool | None = None,
    initial_dg_min_up_remaining_steps: int = 0,
    initial_prev_battery_net_kw: float = 0.0,
    time_limit_seconds: float = 4.0,
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    """MILP dispatch with battery direction binaries, DG commitment, kVA billing and hard limits."""
    n = len(profiles)
    if n == 0:
        return [], {"solver_status": "empty horizon", "fallback_used": False}

    dt = cfg.interval_minutes / 60.0
    cap = cfg.battery_capacity_kwh
    p_batt_base = min(cfg.battery_power_kw, cap * cfg.battery_c_rate, cfg.battery_inverter_kva) if cap > 0 else 0.0
    rates = tariff_values(cfg)
    demand_rate = float(rates["demand_rate"])
    export_rate = float(rates["export"])
    prior_peak = cfg.peak_kva_so_far_this_month if previous_peak_kva is None else max(0.0, float(previous_peak_kva))
    initial_e = cap * cfg.initial_soc_pct / 100.0 if initial_energy_kwh is None else float(initial_energy_kwh)
    initial_e = float(np.clip(initial_e, cap * max(cfg.min_soc_pct, cfg.reserve_soc_pct) / 100.0 if cap > 0 else 0.0,
                               cap * cfg.max_soc_pct / 100.0))
    reserve_e = cap * max(cfg.min_soc_pct, cfg.reserve_soc_pct) / 100.0
    max_e = cap * cfg.max_soc_pct / 100.0
    min_up_steps = max(1, int(math.ceil(cfg.dg_min_up_minutes / cfg.interval_minutes)))
    dg_enabled = cfg.dg_enabled and cfg.dg_capacity_kw > 0
    dg_initial = cfg.dg_initially_on if initial_dg_on is None else bool(initial_dg_on)
    tan_phi = _pf_tan(cfg)

    # Variable blocks: import, export, curtail, battery charge/discharge, unserved,
    # stored energy, reactive support, DG power/on/start, battery charge-mode, billing peak, excess kVA.
    starts = {
        "imp": 0,
        "exp": n,
        "curt": 2*n,
        "ch": 3*n,
        "dis": 4*n,
        "uns": 5*n,
        "energy": 6*n,
        "q": 7*n + 1,
        "dg_p": 8*n + 1,
        "dg_on": 9*n + 1,
        "dg_start": 10*n + 1,
        "mode": 11*n + 1,
        "peak": 12*n + 1,
        "over": 12*n + 2,
    }
    nv = 12*n + 3

    def ix(name: str, t: int) -> int:
        return starts[name] + t

    c = np.zeros(nv, dtype=float)
    lb = np.zeros(nv, dtype=float)
    ub = np.full(nv, np.inf, dtype=float)
    integrality = np.zeros(nv, dtype=int)

    # Cost terms.
    for t, p in enumerate(profiles):
        c[ix("imp", t)] = p["import_rate_inr_kwh"] * dt
        c[ix("exp", t)] = -export_rate * dt
        c[ix("curt", t)] = 0.0001 * dt
        c[ix("ch", t)] = _expected_wear_rate_inr_per_kwh(cfg) * dt
        c[ix("dis", t)] = _expected_wear_rate_inr_per_kwh(cfg) * dt
        c[ix("uns", t)] = 100_000.0 * dt
        c[ix("dg_p", t)] = cfg.dg_fuel_l_per_kwh * cfg.diesel_price_inr_l * dt
        c[ix("dg_on", t)] = cfg.dg_running_cost_inr_per_hour * dt
        c[ix("dg_start", t)] = cfg.dg_start_cost_inr
        # Local linear value estimate for reactive support: reduce the configured PF
        # surcharge/incentive using dPF/dQ at the forecast operating point.
        p_est = max(1.0, p["load_kw"] - p["solar_kw"])
        q_est = max(0.0, p["load_kw"] * tan_phi)
        pf_derivative = (p_est * q_est / max((p_est * p_est + q_est * q_est) ** 1.5, 1e-9))
        pf_value_per_pf = max(cfg.pf_penalty_pct_per_0_01, cfg.pf_incentive_pct_per_0_01) / 100.0 / 0.01
        c[ix("q", t)] = -0.35 * p_est * p["import_rate_inr_kwh"] * dt * pf_derivative * pf_value_per_pf

        # Hard variable bounds, including thermal derating and solar clipping availability.
        lb[ix("imp", t)] = 0.0
        ub[ix("imp", t)] = cfg.grid_import_limit_kw if cfg.grid_import_limit_kw is not None else np.inf
        ub[ix("exp", t)] = cfg.max_grid_export_kw if cfg.export_enabled else 0.0
        ub[ix("curt", t)] = max(0.0, p["solar_kw"])
        pmax_t = p_batt_base * _temperature_derate(p.get("ambient_temperature_c", 25.0), cfg)
        ub[ix("ch", t)] = pmax_t
        ub[ix("dis", t)] = pmax_t
        ub[ix("uns", t)] = max(0.0, p["load_kw"])
        ub[ix("q", t)] = min(cfg.battery_reactive_support_max_kvar, cfg.battery_inverter_kva) if cap > 0 else 0.0
        ub[ix("dg_p", t)] = cfg.dg_capacity_kw if dg_enabled else 0.0
        ub[ix("dg_on", t)] = 1.0 if dg_enabled else 0.0
        ub[ix("dg_start", t)] = 1.0 if dg_enabled else 0.0
        ub[ix("mode", t)] = 1.0 if pmax_t > 0 else 0.0
        integrality[ix("dg_on", t)] = 1
        integrality[ix("dg_start", t)] = 1
        integrality[ix("mode", t)] = 1

    # Stored-energy bounds; reserve is a dispatch floor throughout the horizon.
    ub[starts["energy"]:starts["energy"] + n + 1] = max_e
    lb[starts["energy"]:starts["energy"] + n + 1] = reserve_e if cap > 0 else 0.0
    lb[ix("energy", 0)] = initial_e
    ub[ix("energy", 0)] = initial_e
    peak_floor = max(prior_peak, cfg.contract_demand_kva * cfg.minimum_billable_demand_fraction)
    lb[starts["peak"]] = peak_floor
    c[starts["peak"]] = demand_rate
    c[starts["over"]] = demand_rate * (cfg.excess_contract_demand_multiplier - 1.0)
    ub[starts["over"]] = np.inf

    # Initial DG minimum-up remaining commitment is honoured at the start of this horizon.
    if dg_initial and initial_dg_min_up_remaining_steps > 0:
        for t in range(min(n, int(initial_dg_min_up_remaining_steps))):
            lb[ix("dg_on", t)] = 1.0

    # Sparse constraints builder.
    rr: list[int] = []
    cc: list[int] = []
    vv: list[float] = []
    clb: list[float] = []
    cub: list[float] = []
    row_idx = 0

    def add_constraint(coefs: dict[int, float], lower: float = -np.inf, upper: float = np.inf):
        nonlocal row_idx
        for col, value in coefs.items():
            if abs(value) > 1e-12:
                rr.append(row_idx); cc.append(col); vv.append(float(value))
        clb.append(float(lower)); cub.append(float(upper)); row_idx += 1

    # Active-power balance and battery SOC dynamics.
    eta_c = cfg.charge_efficiency * cfg.battery_inverter_efficiency_min
    eta_d = cfg.discharge_efficiency * cfg.battery_inverter_efficiency_min
    for t, p in enumerate(profiles):
        add_constraint({ix("imp", t): 1, ix("exp", t): -1, ix("curt", t): -1,
                        ix("ch", t): -1, ix("dis", t): 1, ix("uns", t): 1,
                        ix("dg_p", t): 1}, p["load_kw"] - p["solar_kw"], p["load_kw"] - p["solar_kw"])
        add_constraint({ix("energy", t+1): 1, ix("energy", t): -1,
                        ix("ch", t): -eta_c * dt, ix("dis", t): dt / max(eta_d, 1e-9)}, 0, 0)

        # No simultaneous charge and discharge via one binary mode.
        add_constraint({ix("ch", t): 1, ix("mode", t): -max(0.0, ub[ix("ch", t)])}, upper=0)
        add_constraint({ix("dis", t): 1, ix("mode", t): max(0.0, ub[ix("dis", t)])}, upper=max(0.0, ub[ix("dis", t)]))

        # DG output must be on the committed minimum/maximum segment.
        if dg_enabled:
            dg_min = cfg.dg_capacity_kw * cfg.dg_min_loading_pct / 100.0
            add_constraint({ix("dg_p", t): 1, ix("dg_on", t): -cfg.dg_capacity_kw}, upper=0)
            add_constraint({ix("dg_p", t): -1, ix("dg_on", t): dg_min}, upper=0)
            prev_on = (1.0 if dg_initial else 0.0) if t == 0 else None
            if t == 0:
                add_constraint({ix("dg_on", t): 1, ix("dg_start", t): -1}, upper=prev_on)
            else:
                add_constraint({ix("dg_on", t): 1, ix("dg_on", t-1): -1, ix("dg_start", t): -1}, upper=0)
            if t + min_up_steps <= n:
                coefs = {ix("dg_start", t): float(min_up_steps)}
                for j in range(t, t + min_up_steps):
                    coefs[ix("dg_on", j)] = coefs.get(ix("dg_on", j), 0.0) - 1.0
                add_constraint(coefs, upper=0)
            else:
                # Don't start a generator unless its minimum run time fits within this horizon.
                ub[ix("dg_start", t)] = 0.0

    # Initial power and inter-interval ramping constraints on signed battery power.
    ramp_limit = cfg.battery_ramp_kw_per_minute * cfg.interval_minutes
    initial_net = float(initial_prev_battery_net_kw)
    for t in range(n):
        prev_coef = {} if t == 0 else {ix("dis", t-1): -1, ix("ch", t-1): 1}
        # net_t - net_(t-1) <= ramp
        coefs1 = {ix("dis", t): 1, ix("ch", t): -1, **prev_coef}
        rhs1 = ramp_limit if t > 0 else ramp_limit + initial_net
        add_constraint(coefs1, upper=rhs1)
        # -(net_t - net_(t-1)) <= ramp
        coefs2 = {ix("dis", t): -1, ix("ch", t): 1}
        if t > 0:
            coefs2[ix("dis", t-1)] = 1
            coefs2[ix("ch", t-1)] = -1
        rhs2 = ramp_limit if t > 0 else ramp_limit - initial_net
        add_constraint(coefs2, upper=rhs2)

    # Conservative polygonal approximation to the inverter apparent-power circle.
    facet_count = 12
    half_angle = (math.pi / 2) / (2 * facet_count)
    conservative_factor = math.cos(half_angle)
    for t in range(n):
        if cfg.battery_inverter_kva > 0:
            for k in range(facet_count + 1):
                theta = (math.pi / 2) * k / facet_count
                add_constraint({ix("ch", t): math.cos(theta), ix("dis", t): math.cos(theta),
                                ix("q", t): math.sin(theta)},
                               upper=cfg.battery_inverter_kva * conservative_factor)
        # Q support cannot exceed the reactive demand of served load.
        add_constraint({ix("q", t): 1, ix("uns", t): -tan_phi}, upper=profiles[t]["load_kw"] * tan_phi)

    # Transformer loading and month-to-date maximum billed kVA.
    # The facets conservatively approximate sqrt(P^2 + Q^2); Q is reduced by battery inverter support.
    for t, p in enumerate(profiles):
        q_load = p["load_kw"] * tan_phi
        for k in range(facet_count + 1):
            theta = (math.pi / 2) * k / facet_count
            cp, sp = math.cos(theta), math.sin(theta)
            # cp*P + sp*(Q_load - q_support - tan(phi)*unserved) <= kVA limit
            core = {ix("imp", t): cp, ix("q", t): -sp, ix("uns", t): -sp * tan_phi}
            if cfg.transformer_rating_kva is not None:
                add_constraint(core, upper=cfg.transformer_rating_kva * conservative_factor - sp * q_load)
            # Billed peak variable carries the month-to-date peak as a lower bound.
            peak_coefs = dict(core)
            peak_coefs[ix("peak", 0)] = -1.0
            add_constraint(peak_coefs, upper=-sp * q_load)

    # Excess demand above contract demand is billed at a configurable multiplier.
    add_constraint({ix("peak", 0): 1, ix("over", 0): -1}, upper=cfg.contract_demand_kva)

    # Finish at least at the protected reserve; the MILP bounds enforce reserve at every step.
    matrix = coo_matrix((vv, (rr, cc)), shape=(row_idx, nv)).tocsr()
    constraint = LinearConstraint(matrix, np.asarray(clb), np.asarray(cub))
    try:
        result = milp(c, integrality=integrality, bounds=Bounds(lb, ub), constraints=constraint,
                      options={"time_limit": time_limit_seconds, "mip_rel_gap": 0.03, "presolve": True})
    except Exception as exc:  # Any solver/availability error moves to the safe controller.
        fallback_actions = _rule_actions(profiles, cfg, initial_e, dg_initial, initial_dg_min_up_remaining_steps, initial_prev_battery_net_kw)
        return fallback_actions, {"solver_status": f"MILP exception; rule-based fallback used: {exc}", "fallback_used": True}

    if result.x is None:
        fallback_actions = _rule_actions(profiles, cfg, initial_e, dg_initial, initial_dg_min_up_remaining_steps, initial_prev_battery_net_kw)
        return fallback_actions, {"solver_status": f"MILP failed; rule-based fallback used: {result.message}", "fallback_used": True}

    x = result.x
    actions: list[dict[str, float]] = []
    for t in range(n):
        actions.append({
            "battery_charge_kw": max(0.0, float(x[ix("ch", t)])),
            "battery_discharge_kw": max(0.0, float(x[ix("dis", t)])),
            "battery_reactive_support_kvar": max(0.0, float(x[ix("q", t)])),
            "dg_power_kw": max(0.0, float(x[ix("dg_p", t)])),
            "dg_on": 1.0 if x[ix("dg_on", t)] >= 0.5 else 0.0,
        })
    info = {
        "solver_status": str(result.message),
        "solver_success": bool(result.success),
        "solver_objective_inr": round(float(result.fun), 3) if result.fun is not None else None,
        "fallback_used": False,
        "binary_commitment_model": True,
        "battery_simultaneous_charge_discharge_forbidden": True,
        "billing_peak_carry_forward_kva": round(prior_peak, 3),
        "forecast_horizon_steps": n,
    }
    return actions, info


def _forecast_horizon(actual: list[dict[str, Any]], cfg: SimulationConfig, start_idx: int) -> list[dict[str, Any]]:
    """Generate synthetic forecast error for testing robustness, not a real weather service."""
    rng = np.random.default_rng(cfg.random_seed + 7001 + start_idx)
    forecast: list[dict[str, Any]] = []
    load_sd = cfg.forecast_load_error_pct / 100.0
    solar_sd = cfg.forecast_solar_error_pct / 100.0
    for i, p in enumerate(actual):
        row = dict(p)
        # Perturb only forecasted load and solar; all other operating assumptions remain fixed.
        load_bias = 0.015 * math.sin((start_idx + i) / 15.0)
        solar_bias = 0.04 * math.sin((start_idx + i) / 10.0)
        row["load_kw"] = max(0.0, p["load_kw"] * (1.0 + load_bias + rng.normal(0.0, load_sd)))
        row["solar_kw"] = max(0.0, p["solar_kw"] * (1.0 + solar_bias + rng.normal(0.0, solar_sd)))
        row["solar_kw"] = min(row["solar_kw"], p.get("solar_ac_rating_kw", row["solar_kw"]))
        row["import_rate_inr_kwh"] = p["import_rate_inr_kwh"]
        forecast.append(row)
    return forecast


def _simulate_forecast_mpc(profiles: list[dict[str, Any]], cfg: SimulationConfig, *, perfect_information: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-plan a 24-hour horizon hourly; apply one hour, with either noisy or true look-ahead."""
    dt_minutes = cfg.interval_minutes
    steps_per_hour = max(1, int(60 / dt_minutes))
    horizon_steps = int(24 * 60 / dt_minutes)
    total_actions: list[dict[str, Any]] = []
    energy = cfg.battery_capacity_kwh * cfg.initial_soc_pct / 100.0
    dg_on = cfg.dg_initially_on
    dg_remaining = 0
    prev_net = 0.0
    peak_so_far = cfg.peak_kva_so_far_this_month
    solve_infos: list[dict[str, Any]] = []
    actual_rows: list[dict[str, Any]] = []
    baseline_guard_windows = 0

    for start in range(0, len(profiles), steps_per_hour):
        end = min(len(profiles), start + steps_per_hour)
        horizon = profiles[start:min(len(profiles), start + horizon_steps)]
        forecast = [dict(p) for p in horizon] if perfect_information else _forecast_horizon(horizon, cfg, start)
        plan, info = _solve_milp(
            forecast, cfg, initial_energy_kwh=energy, previous_peak_kva=peak_so_far,
            initial_dg_on=dg_on, initial_dg_min_up_remaining_steps=dg_remaining,
            initial_prev_battery_net_kw=prev_net, time_limit_seconds=1.5,
        )
        # Economic guard: score the MILP and a transparent rule-based candidate on the
        # same predicted horizon and current physical/billing state. If the MILP's
        # own predicted realized-cost metric is worse, fall back to the rule-based
        # candidate for this horizon rather than assuming optimization always wins.
        rule_candidate = _rule_actions(
            forecast, cfg, initial_energy_kwh=energy, initial_dg_on=dg_on,
            initial_dg_min_up_remaining_steps=dg_remaining, initial_prev_battery_net_kw=prev_net,
        )
        scoring_cfg = cfg.model_copy(update={"peak_kva_so_far_this_month": peak_so_far, "dg_initially_on": dg_on})
        if not info.get("fallback_used"):
            planned_rows = _replay_actions(
                forecast, plan, scoring_cfg, initial_energy_kwh=energy,
                initial_prev_battery_net_kw=prev_net, initial_dg_on=dg_on,
                initial_dg_min_up_remaining_steps=dg_remaining,
            )
            rule_rows = _replay_actions(
                forecast, rule_candidate, scoring_cfg, initial_energy_kwh=energy,
                initial_prev_battery_net_kw=prev_net, initial_dg_on=dg_on,
                initial_dg_min_up_remaining_steps=dg_remaining,
            )
            plan_cost = calculate_metrics(forecast, planned_rows, scoring_cfg)["total_cost_inr"]
            rule_cost = calculate_metrics(forecast, rule_rows, scoring_cfg)["total_cost_inr"]
            info["predicted_horizon_cost_inr_est"] = round(float(plan_cost), 2)
            info["rule_candidate_horizon_cost_inr_est"] = round(float(rule_cost), 2)
            if rule_cost + 0.01 < plan_cost:
                plan = rule_candidate
                info["baseline_guard_selected"] = True
                baseline_guard_windows += 1
            else:
                info["baseline_guard_selected"] = False
        else:
            plan = rule_candidate
            info["baseline_guard_selected"] = True
            baseline_guard_windows += 1
        solve_infos.append(info)
        block_plan = plan[:end-start]
        block_actual = profiles[start:end]
        block_rows = _replay_actions(
            block_actual, block_plan, cfg, initial_energy_kwh=energy,
            initial_prev_battery_net_kw=prev_net, initial_dg_on=dg_on,
            initial_dg_min_up_remaining_steps=dg_remaining,
        )
        actual_rows.extend(block_rows)
        if block_rows:
            last = block_rows[-1]
            energy = float(last["battery_energy_kwh"])
            prev_net = float(last["battery_net_kw"])
            dg_on = bool(last["dg_on"] >= 0.5)
            dg_remaining = int(last["dg_min_up_remaining_steps"])
            peak_so_far = max(peak_so_far, max(float(r["grid_import_kva"]) for r in block_rows))
        total_actions.extend(block_plan)

    fallback_count = sum(1 for info in solve_infos if info.get("fallback_used"))
    return actual_rows, {
        "controller": "perfect-information rolling-horizon control" if perfect_information else "forecast-driven rolling-horizon model predictive control",
        "forecast_horizon_hours": 24,
        "replan_every_minutes": 60,
        "uses_true_future_within_horizon": perfect_information,
        "forecast_load_error_assumption_pct": 0.0 if perfect_information else cfg.forecast_load_error_pct,
        "forecast_solar_error_assumption_pct": 0.0 if perfect_information else cfg.forecast_solar_error_pct,
        "solver_windows": len(solve_infos),
        "fallback_windows": fallback_count,
        "fallback_used": fallback_count > 0,
        "baseline_guard_windows": baseline_guard_windows,
        "solver_status": "; ".join(sorted(set(str(x.get("solver_status", "unknown")) for x in solve_infos)))[:1000],
    }


def _expected_wear_rate_inr_per_kwh(cfg: SimulationConfig) -> float:
    """Expected cost per kWh of total battery throughput using a DoD/cycle-life curve."""
    dod = max(0.10, cfg.expected_cycle_dod_pct / 100.0)
    reference_dod = 0.80
    cycle_life = cfg.cycle_life_at_80dod * (reference_dod / dod) ** cfg.dod_life_curve_exponent
    return cfg.battery_replacement_cost_inr_per_kwh / max(2.0 * cycle_life * dod, 1.0)


def _depth_sensitive_battery_aging(rows: list[dict[str, Any]], profiles: list[dict[str, Any]], cfg: SimulationConfig) -> dict[str, float]:
    cap = cfg.battery_capacity_kwh
    if cap <= 0 or not rows:
        return {"battery_depth_adjusted_wear_cost_inr_est": 0.0, "battery_estimated_soh_end_pct": 100.0,
                "battery_estimated_efc": 0.0, "battery_observed_daily_dod_pct_est": 0.0}
    daily: dict[str, dict[str, Any]] = {}
    for p, row in zip(profiles, rows):
        day = p["timestamp"][:10]
        if day not in daily:
            daily[day] = {"soc": [], "throughput": 0.0, "discharge": 0.0}
        daily[day]["soc"].append(float(row["battery_soc_pct"]))
        daily[day]["throughput"] += (float(row["battery_charge_kw"]) + float(row["battery_discharge_kw"])) * cfg.interval_minutes / 60.0
        daily[day]["discharge"] += float(row["battery_discharge_kw"]) * cfg.interval_minutes / 60.0
    total_wear = 0.0
    soh_loss = 0.0
    weighted_dod = 0.0
    weighted_discharge = 0.0
    for stats in daily.values():
        if not stats["soc"] or stats["throughput"] <= 1e-9:
            continue
        dod = float(np.clip((max(stats["soc"]) - min(stats["soc"])) / 100.0, 0.10, 1.0))
        cycle_life = cfg.cycle_life_at_80dod * (0.80 / dod) ** cfg.dod_life_curve_exponent
        wear_rate = cfg.battery_replacement_cost_inr_per_kwh / max(2.0 * cycle_life * dod, 1.0)
        total_wear += stats["throughput"] * wear_rate
        life_efc = cycle_life * dod
        efc = stats["discharge"] / cap
        soh_loss += 20.0 * efc / max(life_efc, 1.0)
        weighted_dod += dod * stats["discharge"]
        weighted_discharge += stats["discharge"]
    efc_total = sum(float(r["battery_discharge_kw"]) * cfg.interval_minutes / 60.0 for r in rows) / cap
    avg_dod = weighted_dod / weighted_discharge * 100.0 if weighted_discharge > 1e-9 else 0.0
    return {
        "battery_depth_adjusted_wear_cost_inr_est": round(total_wear, 2),
        "battery_estimated_soh_end_pct": round(float(np.clip(100.0 - soh_loss, 0.0, 100.0)), 5),
        "battery_estimated_efc": round(efc_total, 4),
        "battery_observed_daily_dod_pct_est": round(avg_dod, 2),
    }


def calculate_metrics(profiles: list[dict[str, Any]], rows: list[dict[str, Any]], cfg: SimulationConfig) -> dict[str, Any]:
    dt = cfg.interval_minutes / 60.0
    rates = tariff_values(cfg)
    load_kwh = sum(p["load_kw"] * dt for p in profiles)
    solar_kwh = sum(p["solar_kw"] * dt for p in profiles)
    imported_kwh = sum(r["grid_import_kw"] * dt for r in rows)
    exported_kwh = sum(r["grid_export_kw"] * dt for r in rows)
    charged_kwh = sum(r["battery_charge_kw"] * dt for r in rows)
    discharged_kwh = sum(r["battery_discharge_kw"] * dt for r in rows)
    dg_kwh = sum(r["dg_output_kw"] * dt for r in rows)
    dg_fuel_l = sum(r["dg_fuel_l"] for r in rows)
    curtailed_kwh = sum(r["curtailed_solar_kw"] * dt for r in rows)
    clipped_kwh = sum(r.get("solar_clipped_kw", 0.0) * dt for r in profiles)
    unserved_kwh = sum(r.get("unserved_load_kw", 0.0) * dt for r in rows)
    energy_cost = sum(r["grid_import_kw"] * p["import_rate_inr_kwh"] * dt for p, r in zip(profiles, rows))
    export_credit = exported_kwh * float(rates["export"])
    pf_kvah = sum(r["grid_import_kva"] * dt for r in rows)
    avg_billed_pf = imported_kwh / pf_kvah if pf_kvah > 1e-9 else 1.0
    pf_adjust_pct = 0.0
    if avg_billed_pf < cfg.pf_reference:
        pf_adjust_pct = min(50.0, (cfg.pf_reference - avg_billed_pf) / 0.01 * cfg.pf_penalty_pct_per_0_01)
    elif avg_billed_pf > cfg.pf_incentive_threshold:
        pf_adjust_pct = -min(20.0, (avg_billed_pf - cfg.pf_incentive_threshold) / 0.01 * cfg.pf_incentive_pct_per_0_01)
    pf_adjustment = energy_cost * pf_adjust_pct / 100.0

    peak_kw = max((r["grid_import_kw"] for r in rows), default=0.0)
    peak_kva = max((r["grid_import_kva"] for r in rows), default=0.0)
    billing_peak_kva = max(cfg.peak_kva_so_far_this_month, peak_kva)
    billable_peak_kva = max(billing_peak_kva, cfg.contract_demand_kva * cfg.minimum_billable_demand_fraction)
    prior_billable_peak_kva = max(cfg.peak_kva_so_far_this_month, cfg.contract_demand_kva * cfg.minimum_billable_demand_fraction)
    projected_monthly_demand_charge = billable_peak_kva * float(rates["demand_rate"])
    projected_monthly_demand_charge += max(0.0, billable_peak_kva - cfg.contract_demand_kva) * float(rates["demand_rate"]) * (cfg.excess_contract_demand_multiplier - 1.0)
    prior_monthly_demand_charge = prior_billable_peak_kva * float(rates["demand_rate"])
    prior_monthly_demand_charge += max(0.0, prior_billable_peak_kva - cfg.contract_demand_kva) * float(rates["demand_rate"]) * (cfg.excess_contract_demand_multiplier - 1.0)
    incremental_demand_charge = max(0.0, projected_monthly_demand_charge - prior_monthly_demand_charge)

    aging = _depth_sensitive_battery_aging(rows, profiles, cfg)
    battery_wear = float(aging["battery_depth_adjusted_wear_cost_inr_est"])
    dg_fuel_cost = dg_fuel_l * cfg.diesel_price_inr_l
    dg_running_cost = sum(1.0 for r in rows if r["dg_on"] >= 0.5) * dt * cfg.dg_running_cost_inr_per_hour
    dg_start_count = sum(1 for i, r in enumerate(rows) if r["dg_on"] >= 0.5 and ((i == 0 and not cfg.dg_initially_on) or (i > 0 and rows[i-1]["dg_on"] < 0.5)))
    dg_start_cost = dg_start_count * cfg.dg_start_cost_inr
    unserved_penalty = unserved_kwh * 100_000.0
    total_cost = energy_cost - export_credit + incremental_demand_charge + pf_adjustment + battery_wear + dg_fuel_cost + dg_running_cost + dg_start_cost + unserved_penalty

    direct_solar_to_load = sum(min(p["solar_kw"], p["load_kw"]) * dt for p in profiles)
    solar_to_battery_kwh = sum(r.get("solar_to_battery_kw", 0.0) * dt for r in rows)
    solar_self_consumed = min(solar_kwh, direct_solar_to_load + solar_to_battery_kwh)
    solar_self_consumption = 100.0 * solar_self_consumed / solar_kwh if solar_kwh > 1e-9 else 0.0
    self_sufficiency = 100.0 * max(0.0, 1.0 - imported_kwh / load_kwh) if load_kwh > 1e-9 else 0.0
    emissions_grid = imported_kwh * cfg.grid_emissions_kg_kwh
    emissions_dg = dg_fuel_l * cfg.diesel_emissions_kg_l
    emissions_total = emissions_grid + emissions_dg
    energy_balance_max_error = max((abs(float(r["power_balance_error_kw"])) for r in rows), default=0.0)
    kva_limit_violations = sum(1 for r in rows if cfg.transformer_rating_kva is not None and r["grid_import_kva"] > cfg.transformer_rating_kva + 0.1)

    return {
        "total_cost_inr": round(float(total_cost), 2),
        "energy_cost_inr": round(float(energy_cost), 2),
        "demand_charge_increment_inr": round(float(incremental_demand_charge), 2),
        "projected_monthly_demand_charge_inr": round(float(projected_monthly_demand_charge), 2),
        "prior_month_demand_charge_basis_inr": round(float(prior_monthly_demand_charge), 2),
        "power_factor_adjustment_inr_est": round(float(pf_adjustment), 2),
        "billed_average_power_factor_est": round(float(avg_billed_pf), 4),
        "export_credit_inr": round(float(export_credit), 2),
        "battery_wear_cost_inr_est": round(float(battery_wear), 2),
        "dg_fuel_cost_inr": round(float(dg_fuel_cost), 2),
        "dg_running_cost_inr": round(float(dg_running_cost), 2),
        "dg_start_cost_inr": round(float(dg_start_cost), 2),
        "dg_fuel_l": round(float(dg_fuel_l), 2),
        "dg_generation_kwh": round(float(dg_kwh), 2),
        "grid_import_kwh": round(float(imported_kwh), 2),
        "grid_export_kwh": round(float(exported_kwh), 2),
        "solar_generation_kwh": round(float(solar_kwh), 2),
        "solar_self_consumed_kwh_est": round(float(solar_self_consumed), 2),
        "solar_self_consumption_pct_est": round(float(solar_self_consumption), 2),
        "load_energy_kwh": round(float(load_kwh), 2),
        "unserved_load_kwh_est": round(float(unserved_kwh), 2),
        "unserved_load_penalty_inr_est": round(float(unserved_penalty), 2),
        "solar_to_load_direct_kwh": round(float(direct_solar_to_load), 2),
        "solar_to_battery_kwh_est": round(float(solar_to_battery_kwh), 2),
        "peak_grid_import_kw": round(float(peak_kw), 2),
        "peak_grid_import_kva": round(float(peak_kva), 2),
        "billing_peak_kva_month_to_date_est": round(float(billing_peak_kva), 2),
        "contract_demand_kva": round(float(cfg.contract_demand_kva), 2),
        "exceeds_contract_demand": bool(billing_peak_kva > cfg.contract_demand_kva + 0.1),
        "battery_charge_kwh": round(float(charged_kwh), 2),
        "battery_discharge_kwh": round(float(discharged_kwh), 2),
        "battery_equivalent_full_cycles_est": aging["battery_estimated_efc"],
        "battery_observed_daily_dod_pct_est": aging["battery_observed_daily_dod_pct_est"],
        "battery_estimated_soh_end_pct": aging["battery_estimated_soh_end_pct"],
        "curtailed_solar_kwh": round(float(curtailed_kwh), 2),
        "solar_inverter_clipping_kwh_est": round(float(clipped_kwh), 2),
        "grid_emissions_kg_est": round(float(emissions_grid), 2),
        "diesel_emissions_kg_est": round(float(emissions_dg), 2),
        "total_emissions_kg_est": round(float(emissions_total), 2),
        "grid_self_sufficiency_pct_est": round(float(self_sufficiency), 2),
        "energy_balance_max_abs_error_kw": round(float(energy_balance_max_error), 6),
        "transformer_kva_limit_violations": int(kva_limit_violations),
        "max_grid_import_kva": round(float(cfg.transformer_rating_kva), 2) if cfg.transformer_rating_kva is not None else None,
    }


def _simulate_no_storage(profiles: list[dict[str, Any]], cfg: SimulationConfig) -> list[dict[str, Any]]:
    actions = [{"battery_charge_kw": 0.0, "battery_discharge_kw": 0.0,
                "battery_reactive_support_kvar": 0.0, "dg_power_kw": 0.0, "dg_on": 0.0} for _ in profiles]
    # Force zero battery energy for this strategy: its controls are zero, so its SOC stays at the initial display value only.
    rows = _replay_actions(profiles, actions, cfg, initial_energy_kwh=cfg.battery_capacity_kwh * cfg.initial_soc_pct / 100.0)
    for r in rows:
        r["battery_charge_kw"] = r["battery_discharge_kw"] = r["battery_net_kw"] = 0.0
        r["battery_reactive_support_kvar"] = 0.0
        r["battery_soc_pct"] = cfg.initial_soc_pct if cfg.battery_capacity_kwh > 0 else 0.0
        r["battery_energy_kwh"] = cfg.battery_capacity_kwh * cfg.initial_soc_pct / 100.0
        r["dg_on"] = r["dg_output_kw"] = r["dg_fuel_l"] = 0.0
        r["decision_reason"] = "Baseline: solar supplies the load first; no battery or DG dispatch"
    return rows


def run_simulation(cfg: SimulationConfig) -> dict[str, Any]:
    profiles = generate_profiles(cfg)
    no_storage_rows = _simulate_no_storage(profiles, cfg)

    rule_actions = _rule_actions(profiles, cfg)
    rule_rows = _replay_actions(profiles, rule_actions, cfg)

    global_actions, global_info = _solve_milp(profiles, cfg, time_limit_seconds=8.0)
    global_rows = _replay_actions(profiles, global_actions, cfg)

    mpc_rows, mpc_info = _simulate_forecast_mpc(profiles, cfg, perfect_information=False)
    oracle_rows, oracle_info = _simulate_forecast_mpc(profiles, cfg, perfect_information=True)

    strategy_rows = {
        "solar_only": no_storage_rows,
        "rule_based": rule_rows,
        "forecast_mpc": mpc_rows,
        "perfect_foresight": oracle_rows,
        "global_optimizer": global_rows,
    }
    strategies: dict[str, Any] = {}
    for key, rows in strategy_rows.items():
        metrics = calculate_metrics(profiles, rows, cfg)
        strategies[key] = {"label": STRATEGY_LABELS[key], "metrics": metrics, "series": rows}

    baseline_cost = strategies["solar_only"]["metrics"]["total_cost_inr"]
    forecast_cost = strategies["forecast_mpc"]["metrics"]["total_cost_inr"]
    perfect_cost = strategies["perfect_foresight"]["metrics"]["total_cost_inr"]
    possible_savings = baseline_cost - perfect_cost
    forecast_savings = baseline_cost - forecast_cost
    captured = 100.0 * forecast_savings / possible_savings if possible_savings > 1e-6 else 0.0
    forecasting_regret = forecast_cost - perfect_cost

    chart_series: list[dict[str, Any]] = []
    for p, op, mpc, global_row, rule_row in zip(profiles, oracle_rows, mpc_rows, global_rows, rule_rows):
        chart_series.append({**p, **{
            "optimized_grid_import_kw": round(op["grid_import_kw"], 3),
            "optimized_grid_import_kva": round(op["grid_import_kva"], 3),
            "optimized_grid_export_kw": round(op["grid_export_kw"], 3),
            "optimized_battery_charge_kw": round(op["battery_charge_kw"], 3),
            "optimized_battery_discharge_kw": round(op["battery_discharge_kw"], 3),
            "optimized_battery_soc_pct": round(op["battery_soc_pct"], 3),
            "forecast_mpc_grid_import_kw": round(mpc["grid_import_kw"], 3),
            "forecast_mpc_battery_soc_pct": round(mpc["battery_soc_pct"], 3),
            "global_optimizer_grid_import_kw": round(global_row["grid_import_kw"], 3),
            "rule_grid_import_kw": round(rule_row["grid_import_kw"], 3),
        }})

    return {
        "config": cfg.model_dump(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interval_count": len(profiles),
        "interval_minutes": cfg.interval_minutes,
        "series": chart_series,
        "strategies": strategies,
        "optimizer": {"full_horizon_milp": global_info, "perfect_foresight": oracle_info, "forecast_mpc": mpc_info},
        "forecasting_benchmark": {
            "no_storage_cost_inr": round(float(baseline_cost), 2),
            "forecast_mpc_cost_inr": round(float(forecast_cost), 2),
            "perfect_foresight_cost_inr": round(float(perfect_cost), 2),
            "forecast_controller_savings_vs_no_storage_inr": round(float(forecast_savings), 2),
            "perfect_foresight_savings_headroom_inr": round(float(possible_savings), 2),
            "forecasting_regret_inr_est": round(float(forecasting_regret), 2),
            "share_of_perfect_foresight_savings_captured_pct_est": round(float(captured), 2),
            "interpretation": "The perfect-information benchmark sees the true generated load/solar profile for its next 24-hour horizon and re-plans hourly. Forecast MPC uses the same controller cadence with synthetic forecast errors. The separate full-horizon MILP plan is reported as an additional diagnostic; perfect-information results are an idealized benchmark, not an implementable controller.",
        },
        "tariff": {**TARIFF_SOURCES[cfg.tariff_preset], **{"selected_label": tariff_values(cfg)["label"]}},
        "assumptions": [
            "Factory demand and solar output are synthetic demonstration profiles, not measured Reliance or customer telemetry.",
            "The Rajasthan HT-5 tariff option is a historical FY 2024–25 case study from the RERC order dated 26 July 2024; verify the latest category-, voltage- and DISCOM-specific order before real use.",
            "Illustrative time-of-day prices are demonstration settings, not a claimed current Indian utility tariff.",
            "Demand charge modelling carries forward the month-to-date kVA peak and computes the incremental impact of a new peak; the full projected monthly basis is separately reported.",
            "Power factor, inverter reactive support, transformer loading and kVA demand are simplified engineering models; actual metering and tariff billing rules may differ.",
            "Battery state-of-health and depth-of-discharge wear are simplified cycle-life estimates, not an electrochemical warranty or validated rainflow model.",
            "Forecast uncertainty is synthetically injected around the generated profiles to benchmark control decisions; this is not a trained real-world forecast service.",
            "This project is a simulation and decision-support prototype only. It is not connected to or authorized to control real electrical equipment.",
        ],
    }


def run_outage_test(result: dict[str, Any], req: OutageRequest) -> dict[str, Any]:
    cfg = SimulationConfig.model_validate(result["config"])
    profiles = result["series"]
    key = req.strategy
    chosen = result["strategies"][key]["series"]
    dt = cfg.interval_minutes / 60.0
    start_idx = int(req.start_hour / dt)
    duration_steps = max(1, int(math.ceil(req.duration_hours / dt)))
    cap = cfg.battery_capacity_kwh
    if chosen and start_idx > 0:
        stored_energy = float(chosen[min(start_idx - 1, len(chosen)-1)].get("battery_energy_kwh", cap * cfg.initial_soc_pct / 100.0))
    else:
        stored_energy = cap * cfg.initial_soc_pct / 100.0
    start_energy_kwh = stored_energy
    min_energy = cap * cfg.min_soc_pct / 100.0
    dg_available = bool(req.dg_available_during_outage and cfg.dg_enabled and cfg.dg_capacity_kw > 0)
    dg_on = False
    dg_remaining = 0
    min_up_steps = max(1, int(math.ceil(cfg.dg_min_up_minutes / cfg.interval_minutes)))
    rows: list[dict[str, Any]] = []
    total_critical = total_served = total_unserved = total_dg_fuel = 0.0

    for j in range(duration_steps):
        idx = start_idx + j
        if idx >= len(profiles):
            break
        p = profiles[idx]
        critical_load = p["load_kw"] * req.critical_load_pct / 100.0
        solar = min(p["solar_kw"], critical_load) if req.solar_available_during_outage else 0.0
        remaining = max(0.0, critical_load - solar)

        dg_kw = 0.0
        if dg_available and (remaining > 1e-6 or (dg_on and dg_remaining > 0)):
            if not dg_on:
                dg_on = True
                dg_remaining = min_up_steps - 1
            elif dg_remaining > 0:
                dg_remaining -= 1
            dg_kw = min(cfg.dg_capacity_kw, max(cfg.dg_capacity_kw * cfg.dg_min_loading_pct / 100.0, remaining))
        else:
            dg_on = False
            dg_remaining = 0
        dg_to_load = min(remaining, dg_kw)
        remaining -= dg_to_load
        pmax = _effective_battery_kw(cfg, p.get("ambient_temperature_c", 25.0))
        eta = battery_inverter_efficiency_at_kw(min(pmax, remaining), cfg)
        eta_d = cfg.discharge_efficiency * eta
        available_discharge = max(0.0, stored_energy - min_energy) * eta_d / dt if cap > 0 else 0.0
        battery_to_load = min(remaining, pmax, available_discharge)
        stored_energy -= battery_to_load * dt / max(eta_d, 1e-9) if cap > 0 else 0.0
        served = solar + dg_to_load + battery_to_load
        unserved = max(0.0, critical_load - served)
        fuel_l = dg_kw * dt * cfg.dg_fuel_l_per_kwh
        total_critical += critical_load * dt
        total_served += (critical_load - unserved) * dt
        total_unserved += unserved * dt
        total_dg_fuel += fuel_l
        rows.append({
            "timestamp": p["timestamp"], "critical_load_kw": round(critical_load, 3),
            "solar_to_critical_kw": round(solar, 3), "dg_to_critical_kw": round(dg_to_load, 3),
            "battery_to_critical_kw": round(battery_to_load, 3), "unserved_critical_kw": round(unserved, 3),
            "battery_soc_pct": round(stored_energy / cap * 100.0, 2) if cap > 0 else 0.0,
            "dg_fuel_l": round(fuel_l, 3),
        })
    served_pct = 100.0 * total_served / total_critical if total_critical > 1e-9 else 100.0
    return {
        "strategy": key,
        "start_hour": req.start_hour,
        "duration_hours_requested": req.duration_hours,
        "duration_hours_simulated": round(len(rows) * dt, 2),
        "critical_load_pct": req.critical_load_pct,
        "solar_available_during_outage": req.solar_available_during_outage,
        "dg_available_during_outage": dg_available,
        "battery_soc_at_outage_start_pct": round(start_energy_kwh / cap * 100.0, 2) if cap > 0 else 0.0,
        "critical_energy_required_kwh": round(total_critical, 2),
        "critical_energy_served_kwh_est": round(total_served, 2),
        "unserved_critical_energy_kwh_est": round(total_unserved, 2),
        "critical_energy_served_pct_est": round(float(np.clip(served_pct, 0.0, 100.0)), 2),
        "dg_fuel_l_est": round(total_dg_fuel, 2),
        "result_label": "Simulation only; not a validated backup-power guarantee",
        "series": rows,
    }


def run_sizing_sweep(cfg: SimulationConfig, capacity_options_kwh: list[float] | None = None) -> dict[str, Any]:
    """Simple battery capacity sizing/NPV sweep using the perfect-foresight model."""
    options = capacity_options_kwh or [0.0, 200.0, 400.0, 600.0, 800.0]
    rows: list[dict[str, Any]] = []
    base_result = run_simulation(cfg)
    base_cost = base_result["strategies"]["solar_only"]["metrics"]["total_cost_inr"]
    days = cfg.days
    capex_per_kwh = cfg.battery_replacement_cost_inr_per_kwh
    project_years, discount, escalation, om_pct = 10, 0.10, 0.04, 0.015
    for capacity in options:
        capacity = max(0.0, min(float(capacity), 10_000.0))
        trial = cfg.model_copy(update={"battery_capacity_kwh": capacity,
                                       "battery_power_kw": min(cfg.battery_power_kw if cfg.battery_capacity_kwh > 0 else capacity * 0.5,
                                                                capacity * cfg.battery_c_rate),
                                       "initial_soc_pct": cfg.initial_soc_pct,
                                       "battery_inverter_kva": max(cfg.battery_inverter_kva, min(capacity * cfg.battery_c_rate, cfg.battery_power_kw)) if capacity > 0 else 0.0,
                                       "battery_reactive_support_max_kvar": cfg.battery_reactive_support_max_kvar if capacity > 0 else 0.0})
        sim = run_simulation(trial)
        optimized_cost = sim["strategies"]["perfect_foresight"]["metrics"]["total_cost_inr"]
        daily_savings = (base_cost - optimized_cost) / max(days, 1)
        annual_savings = daily_savings * 365.0
        capex = capacity * capex_per_kwh
        annual_om = capex * om_pct
        npv = -capex
        for year in range(1, project_years + 1):
            benefit = annual_savings * (1.0 + escalation) ** (year - 1) - annual_om
            npv += benefit / (1.0 + discount) ** year
        rows.append({
            "battery_capacity_kwh": round(capacity, 2),
            "battery_power_kw": round(float(trial.battery_power_kw), 2),
            "modelled_daily_cost_inr_est": round(float(optimized_cost / max(days, 1)), 2),
            "modelled_daily_savings_inr_est": round(float(daily_savings), 2),
            "annualized_savings_inr_est": round(float(annual_savings), 2),
            "capex_inr_assumption": round(float(capex), 2),
            "simple_payback_years_est": round(capex / annual_savings, 2) if annual_savings > 1e-6 else None,
            "npv_10y_inr_est": round(float(npv), 2),
            "perfect_foresight_solver_fallback": bool(sim["optimizer"]["perfect_foresight"].get("fallback_used")),
        })
    return {
        "items": rows,
        "assumptions": {
            "project_life_years": project_years, "discount_rate_pct": discount * 100,
            "annual_energy_savings_escalation_pct": escalation * 100, "annual_om_pct_of_capex": om_pct * 100,
            "battery_capex_inr_per_kwh_assumption": capex_per_kwh,
            "annualization_note": f"Daily savings from {days} synthetic day(s) are annualized to 365 days; do not treat as a bankable forecast.",
            "replacement_note": "No separate mid-life replacement cash flow is included; add one before an investment-grade analysis.",
        },
    }
