from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


IndustryPreset = Literal["factory_shift", "jio_datacenter", "refinery_utilities"]
TariffPreset = Literal["illustrative_tou", "rajasthan_jvvnl_ht5_fy2024_25"]


class SimulationConfig(BaseModel):
    """User-adjustable assumptions for a simulation-only industrial EMS."""

    site_name: str = Field(default="Demo Industrial Plant", min_length=2, max_length=80)
    industry_preset: IndustryPreset = "factory_shift"
    days: int = Field(default=1, ge=1, le=3)
    interval_minutes: Literal[15] = 15
    random_seed: int = Field(default=17, ge=0, le=1_000_000)

    # Solar PV DC sizing and inverter layer.
    solar_capacity_dc_kw: float = Field(default=500.0, ge=0, le=10_000)
    solar_dc_ac_ratio: float = Field(default=1.20, ge=1.0, le=2.0)
    solar_inverter_efficiency: float = Field(default=0.97, gt=0.8, le=1.0)
    max_grid_export_kw: float = Field(default=1000.0, ge=0, le=20_000)
    export_enabled: bool = True

    # Battery energy storage system.
    battery_capacity_kwh: float = Field(default=600.0, ge=0, le=20_000)
    battery_power_kw: float = Field(default=200.0, ge=0, le=10_000)
    battery_c_rate: float = Field(default=0.50, gt=0, le=4.0)
    battery_inverter_kva: float = Field(default=250.0, ge=0, le=20_000)
    battery_reactive_support_max_kvar: float = Field(default=150.0, ge=0, le=20_000)
    initial_soc_pct: float = Field(default=65.0, ge=0, le=100)
    min_soc_pct: float = Field(default=10.0, ge=0, le=100)
    max_soc_pct: float = Field(default=95.0, ge=0, le=100)
    reserve_soc_pct: float = Field(default=25.0, ge=0, le=100)
    charge_efficiency: float = Field(default=0.97, gt=0.5, le=1.0)
    discharge_efficiency: float = Field(default=0.97, gt=0.5, le=1.0)
    battery_inverter_efficiency_min: float = Field(default=0.92, gt=0.7, le=1.0)
    battery_inverter_efficiency_rated: float = Field(default=0.97, gt=0.8, le=1.0)
    battery_ramp_kw_per_minute: float = Field(default=4.0, gt=0, le=1000)
    battery_temperature_derate_start_c: float = Field(default=40.0, ge=15, le=80)
    battery_temperature_derate_pct_per_c: float = Field(default=2.0, ge=0, le=10)

    # Depth-of-discharge-dependent cycle-life approximation.
    battery_replacement_cost_inr_per_kwh: float = Field(default=12_000.0, ge=0, le=1_000_000)
    cycle_life_at_80dod: int = Field(default=6000, ge=100, le=50_000)
    dod_life_curve_exponent: float = Field(default=1.6, ge=0.1, le=4.0)
    expected_cycle_dod_pct: float = Field(default=60.0, ge=10, le=100)

    # Grid / electrical loading layer.
    load_power_factor: float = Field(default=0.88, gt=0.5, le=1.0)
    transformer_rating_kva: float | None = Field(default=650.0, gt=1, le=50_000)
    grid_import_limit_kw: float | None = Field(default=None, gt=0, le=20_000)
    contract_demand_kva: float = Field(default=500.0, gt=1, le=50_000)
    peak_kva_so_far_this_month: float = Field(default=300.0, ge=0, le=50_000)
    demand_charge_inr_kva_month: float = Field(default=300.0, ge=0, le=100_000)
    minimum_billable_demand_fraction: float = Field(default=0.75, ge=0, le=1.0)
    excess_contract_demand_multiplier: float = Field(default=1.5, ge=1.0, le=5.0)
    pf_reference: float = Field(default=0.90, gt=0.5, le=1.0)
    pf_penalty_pct_per_0_01: float = Field(default=1.0, ge=0, le=20)
    pf_incentive_threshold: float = Field(default=0.95, gt=0.5, le=1.0)
    pf_incentive_pct_per_0_01: float = Field(default=0.5, ge=0, le=20)

    # Tariff choices: one explicitly historical RERC case study and one configurable ToD demo.
    tariff_preset: TariffPreset = "illustrative_tou"
    off_peak_rate_inr_kwh: float = Field(default=5.5, ge=0, le=1000)
    normal_rate_inr_kwh: float = Field(default=8.5, ge=0, le=1000)
    peak_rate_inr_kwh: float = Field(default=13.5, ge=0, le=1000)
    export_rate_inr_kwh: float = Field(default=2.0, ge=0, le=1000)

    # Diesel generator.
    dg_enabled: bool = True
    dg_capacity_kw: float = Field(default=200.0, ge=0, le=10_000)
    dg_min_loading_pct: float = Field(default=30.0, ge=0, le=100)
    dg_fuel_l_per_kwh: float = Field(default=0.27, ge=0, le=5)
    diesel_price_inr_l: float = Field(default=95.0, ge=0, le=1000)
    dg_min_up_minutes: int = Field(default=60, ge=15, le=720)
    dg_start_cost_inr: float = Field(default=250.0, ge=0, le=1_000_000)
    dg_running_cost_inr_per_hour: float = Field(default=50.0, ge=0, le=100_000)
    dg_initially_on: bool = False

    # Carbon assumptions. Replace with a chosen CEA factor when preparing a formal study.
    grid_emissions_kg_kwh: float = Field(default=0.716, ge=0, le=10)
    diesel_emissions_kg_l: float = Field(default=2.68, ge=0, le=20)
    factory_load_scale: float = Field(default=1.0, ge=0.2, le=3.0)
    forecast_load_error_pct: float = Field(default=5.0, ge=0, le=50)
    forecast_solar_error_pct: float = Field(default=15.0, ge=0, le=80)

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.min_soc_pct >= self.max_soc_pct:
            raise ValueError("min_soc_pct must be lower than max_soc_pct")
        if not (self.min_soc_pct <= self.initial_soc_pct <= self.max_soc_pct):
            raise ValueError("initial_soc_pct must be between min_soc_pct and max_soc_pct")
        if self.reserve_soc_pct > self.max_soc_pct:
            raise ValueError("reserve_soc_pct cannot exceed max_soc_pct")
        if self.export_rate_inr_kwh > min(self.off_peak_rate_inr_kwh, self.normal_rate_inr_kwh, self.peak_rate_inr_kwh):
            raise ValueError("Export rate should not exceed the lowest configured import rate")
        if self.battery_inverter_efficiency_min > self.battery_inverter_efficiency_rated:
            raise ValueError("Minimum inverter efficiency cannot exceed rated efficiency")
        if self.dg_enabled and self.dg_capacity_kw > 0 and self.dg_min_loading_pct > 0 and self.dg_min_loading_pct > 100:
            raise ValueError("DG minimum loading must be at most 100 percent")
        return self


class OutageRequest(BaseModel):
    start_hour: float = Field(default=18.0, ge=0, lt=24)
    duration_hours: float = Field(default=2.0, gt=0, le=24)
    critical_load_pct: float = Field(default=35.0, gt=0, le=100)
    strategy: Literal["rule_based", "forecast_mpc", "perfect_foresight", "solar_only"] = "forecast_mpc"
    solar_available_during_outage: bool = True
    dg_available_during_outage: bool = True


class ScenarioCompareRequest(BaseModel):
    base: SimulationConfig = Field(default_factory=SimulationConfig)
    alternatives: list[SimulationConfig] = Field(default_factory=list, max_length=3)
