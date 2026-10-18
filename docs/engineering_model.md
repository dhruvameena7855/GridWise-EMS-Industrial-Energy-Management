# Engineering model and assumptions

This note explains the model used in the demo. It is a transparent educational approximation, not a validated plant model or a protection / control design.

## Time grid and units

- Time interval: 15 minutes (`Δt = 0.25 h`).
- Active power: kW; reactive power: kVAr; apparent power: kVA; stored / transferred energy: kWh; tariff: ₹/kWh or ₹/kVA/month as configured.
- Power-flow and battery calculations use interval-average values.

## 1. PV production and clipping

A synthetic irradiance/day profile is multiplied by configured DC PV nameplate power. An illustrative inverter efficiency is applied, and AC power is limited by the DC/AC ratio-derived inverter capacity. Export can be bounded by the configured grid-export limit; otherwise excess solar energy is curtailed. Weather and solar data are synthetic rather than sourced site measurements.

Conceptually:

`P_pv_ac = min(P_pv_dc × η_pv_inverter, P_pv_ac_rated)`

The generation profile is designed to exercise realistic morning / midday / evening shape, not to reproduce a site-specific annual PV yield.

## 2. Active-power balance

With imports positive and exports negative in the net-grid convention:

`P_grid = P_load + P_charge − P_solar − P_discharge − P_DG`

Surplus can be exported, up to configured limits, or curtailed. The replay stores interval power-balance error and the test suite checks it is near zero within numeric tolerance. The exact implementation also records unserved energy where constraints make all demand impossible to serve.

## 3. Battery energy, SOC and operating limits

Stored energy is updated as:

`E[t+1] = E[t] + η_charge × P_charge[t] × Δt − P_discharge[t] × Δt / η_discharge`

SOC is `100 × E / capacity`. The model constrains the dispatch to the SOC bounds and the configured reserve. Power is limited by nameplate power, C-rate × energy capacity, inverter rating, and temperature derating. Inter-interval signed-power ramps are bounded by the configured kW/minute rate times the interval length. Charge and discharge are mutually exclusive within an interval.

The inverter conversion-efficiency model changes with the operating point between a configured minimum and rated efficiency. Reactive support and active power share the inverter's apparent-power capability; the optimizer uses a conservative polygonal approximation to the kVA envelope.

## 4. Power factor, kVA and reactive support

For a simplified fixed load PF:

- `Q_load ≈ P_load × tan(arccos(PF_load))`
- `S_grid ≈ sqrt(P_grid² + Q_grid²)`
- `PF_grid ≈ P_grid / S_grid` when active import is positive and the simplified meter convention applies.

The battery inverter can provide configurable reactive support to reduce the modelled grid reactive demand, subject to its kVA capability. A transformer kVA envelope, grid-import power limit and contract-demand parameters affect dispatch. Real plants may have varying motor PF, capacitors, nonlinear loads, voltage effects, multiple meters, and utility-specific metering rules; this single-equivalent-load model does not capture all of them.

## 5. Demand billing and month-to-date peak

The input `peak_kva_so_far_this_month` carries in a previously recorded peak. The modeled billing peak for a new interval cannot be less than this starting value. A higher simulated kVA reading can raise the peak and the demand-charge estimate. Contract demand and a configurable excess multiplier represent a simplified penalty above the contracted level. The demand billing basis can also have a minimum-billable fraction.

This is the project's key temporal billing constraint: peak demand is not reset when the controller re-plans a new 24-hour horizon. The reported demand charge is a modeled increment / projection, not a full replica of a particular utility's monthly bill rules.

## 6. Diesel generator

The DG model includes rated kW, minimum loading while on, fuel litres per kWh, diesel price, start cost, running cost, and minimum-up time. MILP commitment uses on/start binaries. The rule-based controller is a fallback policy, including configured reserve and capacity guards. This is not a generator transient, fuel-curve, emissions-certification, synchronizing, or real switching model.

## 7. Battery degradation approximation

A simple cycle-life curve approximates a deeper cycle as more expensive per discharged kWh than a shallower cycle. The reported life-consumption / state-of-health metric is only an estimate derived from configured replacement cost, cycle life and cycle depth. It is not a validated electrochemical model, equivalent to the OEM warranty, or a full rainflow-cycle counting algorithm. It does not model all state-of-charge, C-rate, temperature, calendar-ageing, and chemistry interactions.

## 8. Optimizer and comparison

The dispatch MILP includes grid import / export, solar curtailment, charge and discharge, stored-energy state, reactive support, DG on/start/output, mutually exclusive battery direction, a billing-peak variable, and constraint approximations for kVA. The SciPy `milp` interface uses the HiGHS solver. A full-horizon solve is included as a diagnostic.

The rolling controller uses a 24-hour horizon and commits only the next interval before planning again. Forecast MPC injects controlled synthetic errors around the generated future demand and solar values. Perfect-information MPC sees the actual simulated profile within its horizon; it provides an idealized comparison and must not be confused with a practical forecast. Both use an economic guard that compares against a rule-based candidate using the modeled horizon objective.

After scheduling, controller actions are replayed through the more detailed common model. Some replay metrics (for example detailed efficiency and degradation estimates) are nonlinear approximations that the MILP linear objective does not match exactly. Therefore:

- do not claim the full-horizon MILP's final replay cost is guaranteed to beat all rolling controllers;
- do not promise a positive value of forecasting for every scenario;
- compare every controller on the same profile and state the chosen tariff / configuration;
- use the benchmark values as measured outputs of this prototype, not universal performance claims.

## 9. Outage and sizing analysis

Outage mode supplies a configured fraction of the load using remaining solar (if permitted), a DG (if available), and the battery subject to stored energy / power limits. It estimates critical load served over the simulated portion of the requested outage. It does not simulate real islanding, grid-forming controls, protection coordination, black start, motor starting, frequency / voltage dynamics, or switching transients.

The battery capacity sweep compares configured capacity options using scenario dispatch and calculates indicative simple payback / discounted NPV with stated capex, escalation, operating-cost and discount assumptions. These outputs are highly assumption-sensitive and are not a project finance model.
