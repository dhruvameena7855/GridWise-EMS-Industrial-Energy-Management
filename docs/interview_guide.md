# Interview walkthrough — GridWise EMS

Use this as a guide to explain your own implementation. Be ready to open the code and show the calculations, not just the dashboard.

## 60–90 second project pitch

“I built GridWise EMS, a full-stack, simulation-first industrial energy management system. It models 15-minute factory load and solar generation, battery SOC and inverter constraints, power factor / kVA billing, contract demand, a diesel generator and configurable tariffs. It compares a transparent rule-based controller with forecast-driven rolling mixed-integer optimization and a perfect-information diagnostic. The web app supports scenario comparison, outage analysis, battery sizing and CSV export. I emphasized validation: the test suite checks energy balance, SOC limits, controller fallback, month-to-date demand peaks and API persistence. The current data is synthetic and the results are scenario-specific, so I report measured metrics rather than claiming unvalidated savings.”

## Explain why kVA matters

Industrial demand can be metered in kVA, not only kW. At the same active load, poor power factor raises apparent power. A model that bills only kW can understate a facility's demand peak. GridWise represents a fixed equivalent load PF, approximates `S = √(P² + Q²)`, allows battery inverter reactive support inside its kVA envelope, and carries the highest billed kVA from earlier in the billing month into each new optimization horizon. Explain clearly that real tariff rules and plant PF vary and need actual data.

## Explain the three kinds of controller result

- **Rule-based:** easier to explain and useful as a fallback; it reacts to predefined thresholds but doesn't optimize a whole future schedule.
- **Forecast MPC:** uses a forecast for the next 24 hours, solves, executes one interval, then replans. Forecast error can lead to different outcomes.
- **Perfect-information MPC:** uses actual future synthetic inputs inside each horizon. This is an idealized diagnostic for the value of better forecasts, not a possible real controller.
- **Full-horizon MILP:** useful as an additional optimization diagnostic, but its linearized objective is not perfectly identical to all post-processing and battery-wear estimates, so be careful about calling it a strict lower bound.

## Common technical questions

**Why use a 15-minute interval?** It is a useful compromise for an educational energy-management schedule and aligns with many interval-metering applications. Real tariff intervals and telemetry sampling must be checked for the selected site.

**Why is the demand peak a state?** A monthly demand charge depends on the highest demand across the billing period, not just the new 24-hour window. The carried-in peak ensures a controller doesn't pretend the previous billing peak vanished at midnight.

**What does a battery reserve mean?** It protects a minimum modeled stored-energy fraction for resilience. A reserve setting must be selected based on critical load, outage-duration assumptions and operational requirements; it cannot guarantee backup by itself.

**What is the role of the DG?** It is a dispatchable source with minimum output, fuel cost, start / running cost and minimum-up-time constraints. A real DG also needs transient, ramping, synchronization, fuel-curve, reliability, emission and protection modeling.

**Why model battery degradation?** A dispatch that saves on energy might consume battery life. The prototype assigns a higher modeled wear cost to deeper cycling, but its degradation curve is explicitly an approximation—not a validated chemistry model.

**Why might optimization not win?** It may see imperfect forecasts, a simplified tariff, model mismatch, or a different linearized objective from the detailed replay bill. Results are scenario-dependent. The economic guard can prefer the rule-based plan when the forecast-window modeled objective is worse.

**How would you move toward real use?** Obtain interval load, PV and meter data; verify the exact tariff and metering rules; validate inverter, transformer, DG and battery parameters; establish forecast accuracy; perform out-of-sample back-tests; quantify uncertainty; add operator-in-the-loop approvals; then undertake formal electrical, protection, cybersecurity and functional-safety reviews. Start with read-only recommendations, not automatic equipment control.

## Demonstration sequence

1. Start with the default shift-based factory and run a simulation.
2. Explain a midday solar surplus and evening load peak using the timeline.
3. Open strategy comparison and compare cost, kVA peak, PF, battery throughput and DG usage.
4. Increase the carried-in monthly kVA peak to show why a new local peak may not change the billing peak.
5. Change battery capacity or reserve and rerun.
6. Run an outage scenario and explain that the result is simulated energy served, not a backup guarantee.
7. Run the capacity sweep, showing how the assumptions affect payback / NPV.
8. Export CSV and point to the automated tests.

## Resume metric discipline

Don't copy savings values into a resume without rerunning the same configuration and recording its exact tariff, load / solar seed, dates, battery size, billing-peak input, and cost definition. Clearly label synthetic benchmark results. Avoid saying that the model was deployed in or validated against a Reliance facility unless that is actually true.
