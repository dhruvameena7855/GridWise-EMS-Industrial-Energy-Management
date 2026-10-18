(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { config: null, defaults: null, result: null, runs: [], activeView: 'overview', busy: false, lastTariffPreset: 'illustrative_tou', lastIllustrativeTariff: null };
  const strategyMeta = {
    solar_only: { label: 'No-storage baseline', short: 'No storage', tone: 'muted' },
    rule_based: { label: 'Rule-based battery', short: 'Rule-based', tone: 'blue' },
    forecast_mpc: { label: 'Forecast MPC', short: 'Forecast MPC', tone: 'green' },
    perfect_foresight: { label: 'Perfect-information MPC', short: 'Perfect-info', tone: 'purple' },
    global_optimizer: { label: 'Full-horizon MILP diagnostic', short: 'Full-horizon MILP', tone: 'amber' },
  };
  const INR = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 });
  const NUM = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });
  const FIXED = (value, digits = 1) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
  const money = (value) => Number.isFinite(Number(value)) ? INR.format(Number(value)) : '—';
  const pct = (value) => `${FIXED(value, 1)}%`;
  const safe = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function api(url, options = {}) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      let message = typeof data === 'string' ? data : (data.detail || JSON.stringify(data));
      if (Array.isArray(message)) message = message.map((item) => `${(item.loc || []).join('.')}: ${item.msg}`).join(' · ');
      throw new Error(message || `Request failed (${response.status})`);
    }
    return data;
  }

  function toast(message, type = 'success') {
    const node = $('toast');
    node.textContent = message;
    node.className = `toast show ${type}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.className = 'toast'; }, 3600);
  }

  function setBusy(flag, label = 'Working…') {
    state.busy = flag;
    document.querySelectorAll('button').forEach((button) => {
      if (button.dataset.lockDuringRun === 'true') button.disabled = flag;
    });
    const runButtons = ['run-top', 'scenario-run', 'comparison-run', 'alert-run'];
    runButtons.forEach((id) => {
      const button = $(id);
      if (!button) return;
      if (flag) {
        if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
        button.textContent = label;
        button.disabled = true;
      } else {
        button.textContent = button.dataset.originalText || button.textContent;
        button.disabled = false;
      }
    });
    $('form-status').textContent = flag ? 'Running the energy simulation and validating constraints…' : 'Values are validated by the backend.';
  }

  function activateView(view) {
    if (!['overview', 'comparison', 'scenario'].includes(view)) return;
    state.activeView = view;
    document.querySelectorAll('.view').forEach((node) => node.classList.toggle('active', node.id === `view-${view}`));
    document.querySelectorAll('.nav-item').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
    const labels = { overview: 'Overview', comparison: 'Strategy comparison', scenario: 'Scenario lab' };
    $('breadcrumb-current').textContent = labels[view];
    document.title = `GridWise EMS — ${labels[view]}`;
    if (view === 'comparison' && state.result) renderComparison(state.result);
  }

  function readNumber(id, fallback = null) {
    const node = $(id);
    if (!node || node.value.trim() === '') return fallback;
    const n = Number(node.value);
    return Number.isFinite(n) ? n : fallback;
  }

  function formToConfig() {
    const config = { ...(state.defaults || {}), ...(state.config || {}) };
    const textFields = ['site_name', 'industry_preset', 'tariff_preset'];
    const numberFields = [
      'days', 'factory_load_scale', 'solar_capacity_dc_kw', 'solar_dc_ac_ratio', 'solar_inverter_efficiency',
      'max_grid_export_kw', 'battery_capacity_kwh', 'battery_power_kw', 'battery_inverter_kva', 'battery_c_rate',
      'initial_soc_pct', 'reserve_soc_pct', 'min_soc_pct', 'max_soc_pct', 'load_power_factor',
      'transformer_rating_kva', 'contract_demand_kva', 'peak_kva_so_far_this_month', 'demand_charge_inr_kva_month',
      'minimum_billable_demand_fraction', 'grid_import_limit_kw', 'dg_capacity_kw', 'off_peak_rate_inr_kwh',
      'normal_rate_inr_kwh', 'peak_rate_inr_kwh', 'export_rate_inr_kwh', 'diesel_price_inr_l', 'dg_fuel_l_per_kwh',
      'forecast_load_error_pct', 'forecast_solar_error_pct', 'charge_efficiency', 'discharge_efficiency',
      'battery_inverter_efficiency_min', 'battery_inverter_efficiency_rated', 'battery_ramp_kw_per_minute',
      'battery_temperature_derate_start_c', 'battery_temperature_derate_pct_per_c', 'battery_reactive_support_max_kvar',
      'battery_replacement_cost_inr_per_kwh', 'cycle_life_at_80dod', 'dod_life_curve_exponent', 'expected_cycle_dod_pct',
      'pf_reference', 'pf_penalty_pct_per_0_01', 'pf_incentive_threshold', 'pf_incentive_pct_per_0_01',
      'excess_contract_demand_multiplier', 'dg_min_loading_pct', 'dg_min_up_minutes', 'dg_start_cost_inr',
      'dg_running_cost_inr_per_hour', 'grid_emissions_kg_kwh', 'diesel_emissions_kg_l',
    ];
    textFields.forEach((key) => { if ($(key)) config[key] = $(key).value; });
    numberFields.forEach((key) => {
      if (!$(key)) return;
      const blankMeansNull = key === 'grid_import_limit_kw';
      config[key] = blankMeansNull && $(key).value.trim() === '' ? null : readNumber(key, config[key]);
    });
    config.dg_enabled = $('dg_enabled').checked;
    config.dg_initially_on = $('dg_initially_on').checked;
    config.export_enabled = $('export_enabled').checked;
    return config;
  }

  function populateForm(config) {
    state.config = { ...config };
    const fields = [
      'site_name', 'industry_preset', 'days', 'factory_load_scale', 'solar_capacity_dc_kw', 'solar_dc_ac_ratio',
      'solar_inverter_efficiency', 'max_grid_export_kw', 'battery_capacity_kwh', 'battery_power_kw', 'battery_inverter_kva',
      'battery_c_rate', 'initial_soc_pct', 'reserve_soc_pct', 'min_soc_pct', 'max_soc_pct', 'load_power_factor',
      'transformer_rating_kva', 'contract_demand_kva', 'peak_kva_so_far_this_month', 'demand_charge_inr_kva_month',
      'minimum_billable_demand_fraction', 'grid_import_limit_kw', 'dg_capacity_kw', 'tariff_preset', 'off_peak_rate_inr_kwh',
      'normal_rate_inr_kwh', 'peak_rate_inr_kwh', 'export_rate_inr_kwh', 'diesel_price_inr_l', 'dg_fuel_l_per_kwh',
      'forecast_load_error_pct', 'forecast_solar_error_pct', 'charge_efficiency', 'discharge_efficiency',
      'battery_inverter_efficiency_min', 'battery_inverter_efficiency_rated', 'battery_ramp_kw_per_minute',
      'battery_temperature_derate_start_c', 'battery_temperature_derate_pct_per_c', 'battery_reactive_support_max_kvar',
      'battery_replacement_cost_inr_per_kwh', 'cycle_life_at_80dod', 'dod_life_curve_exponent', 'expected_cycle_dod_pct',
      'pf_reference', 'pf_penalty_pct_per_0_01', 'pf_incentive_threshold', 'pf_incentive_pct_per_0_01',
      'excess_contract_demand_multiplier', 'dg_min_loading_pct', 'dg_min_up_minutes', 'dg_start_cost_inr',
      'dg_running_cost_inr_per_hour', 'grid_emissions_kg_kwh', 'diesel_emissions_kg_l',
    ];
    fields.forEach((key) => { if ($(key) && config[key] !== undefined) $(key).value = config[key] == null ? '' : config[key]; });
    if ($('dg_enabled')) $('dg_enabled').checked = Boolean(config.dg_enabled);
    if ($('dg_initially_on')) $('dg_initially_on').checked = Boolean(config.dg_initially_on);
    if ($('export_enabled')) $('export_enabled').checked = Boolean(config.export_enabled);
    $('sidebar-site-name').textContent = config.site_name || 'Demo Industrial Plant';
    applyTariffDescription();
  }

  function applyTariffDescription() {
    const selected = $('tariff_preset').value;
    const historical = selected === 'rajasthan_jvvnl_ht5_fy2024_25';
    if (historical && state.lastTariffPreset === 'illustrative_tou') {
      state.lastIllustrativeTariff = {
        off_peak_rate_inr_kwh: readNumber('off_peak_rate_inr_kwh', 5.5),
        normal_rate_inr_kwh: readNumber('normal_rate_inr_kwh', 8.5),
        peak_rate_inr_kwh: readNumber('peak_rate_inr_kwh', 13.5),
        export_rate_inr_kwh: readNumber('export_rate_inr_kwh', 2),
        demand_charge_inr_kva_month: readNumber('demand_charge_inr_kva_month', 300),
      };
    }
    if (historical) {
      $('off_peak_rate_inr_kwh').value = 7.3;
      $('normal_rate_inr_kwh').value = 7.3;
      $('peak_rate_inr_kwh').value = 7.3;
      $('export_rate_inr_kwh').value = 0;
      $('demand_charge_inr_kva_month').value = 300;
    } else if (state.lastTariffPreset === 'rajasthan_jvvnl_ht5_fy2024_25' && state.lastIllustrativeTariff) {
      Object.entries(state.lastIllustrativeTariff).forEach(([key, value]) => { if ($(key)) $(key).value = value; });
    }
    state.lastTariffPreset = selected;
    $('tariff-description').textContent = historical
      ? 'Historical FY 2024–25 case study: ₹7.30/kWh and ₹300/kVA/month for the specified category. Not asserted to be the current tariff; verify later orders.'
      : 'Illustrative demo prices, not a claimed current utility tariff. Edit them for the selected study.';
  }

  function chartLineChart(profiles, series) {
    if (!profiles?.length || !series?.length) return '<div class="empty-chart"><strong>No series available</strong></div>';
    const W = 760, H = 250, left = 42, right = 16, top = 18, bottom = 24;
    const plotW = W - left - right, plotH = H - top - bottom;
    const vals = [];
    profiles.forEach((p, i) => vals.push(Number(p.load_kw || 0), Number(p.solar_kw || 0), Number(series[i]?.grid_import_kw || 0)));
    const max = Math.max(1, ...vals) * 1.1;
    const x = (i) => left + (i / Math.max(1, profiles.length - 1)) * plotW;
    const y = (v) => top + plotH - (v / max) * plotH;
    const path = (key, accessor = (item) => item[key] || 0) => series.map((item, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(Math.max(0, Number(accessor(item, i)))).toFixed(1)}`).join(' ');
    const loadPath = profiles.map((item, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(Math.max(0, Number(item.load_kw || 0))).toFixed(1)}`).join(' ');
    const solarPath = profiles.map((item, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(Math.max(0, Number(item.solar_kw || 0))).toFixed(1)}`).join(' ');
    let grid = '';
    for (let i = 0; i <= 4; i++) {
      const val = max * i / 4;
      const yy = y(val);
      grid += `<line x1="${left}" x2="${W-right}" y1="${yy}" y2="${yy}" stroke="rgba(148,163,184,.14)" stroke-dasharray="3 5"/><text x="${left-8}" y="${yy+4}" text-anchor="end" font-size="10" fill="#8c9ab0">${Math.round(val)}</text>`;
    }
    return `<svg class="profile-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Factory load, solar generation and grid import over the selected simulation period" preserveAspectRatio="none">${grid}<path d="${loadPath}" fill="none" stroke="#8c9aff" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/><path d="${solarPath}" fill="none" stroke="#f5bd63" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/><path d="${path('grid_import_kw')}" fill="none" stroke="#4bd6ad" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }

  function renderPowerChart(result) {
    const profiles = result.series || [];
    const chosen = result.strategies?.forecast_mpc?.series || [];
    $('power-chart').innerHTML = chartLineChart(profiles, chosen);
    const ticks = $('power-chart').parentElement.querySelector('.chart-axis-note');
    if (ticks) ticks.innerHTML = `<span>Start</span><span>${Math.round((profiles.length * 0.25) / 4)} h</span><span>${Math.round((profiles.length * 0.25) / 2)} h</span><span>${Math.round((profiles.length * 0.25) * 0.75)} h</span><span>${profiles.length * 0.25} h</span>`;
  }

  function getMetrics(result, strategy = 'forecast_mpc') {
    return result?.strategies?.[strategy]?.metrics || {};
  }

  function renderOverview(result) {
    const metrics = getMetrics(result, 'forecast_mpc');
    $('metric-cost').textContent = money(metrics.total_cost_inr);
    $('metric-peak').textContent = `${FIXED(metrics.peak_grid_import_kva, 0)} kVA`;
    $('metric-solar').textContent = pct(metrics.solar_self_consumption_pct_est);
    $('metric-co2').textContent = `${NUM.format(Number(metrics.total_emissions_kg_est || 0))} kg`;
    $('run-badge').textContent = `RUN #${result.run_id || 'UNSAVED'}`;
    $('alert-banner').classList.add('hidden');
    $('sidebar-site-name').textContent = result.config?.site_name || 'Demo Industrial Plant';
    renderPowerChart(result);
    renderFlow(result);
    renderMiniBars(result);
    renderGuardrails(result);
    renderComparison(result);
  }

  function renderFlow(result) {
    const profiles = result.series || [];
    const rows = result.strategies?.forecast_mpc?.series || [];
    if (!profiles.length || !rows.length) return;
    const desired = Math.min(profiles.length - 1, Math.max(0, Math.round(17.5 * 60 / (result.interval_minutes || 15))));
    const i = desired;
    const p = profiles[i] || {};
    const r = rows[i] || {};
    $('energy-time-label').textContent = `${p.timestamp || `Interval ${i + 1}`} · selected illustrative sample`;
    $('flow-solar').textContent = `${FIXED(p.solar_kw, 1)} kW`;
    $('flow-grid').textContent = `${FIXED(r.grid_import_kw, 1)} kW`;
    $('flow-battery').textContent = `${FIXED(r.battery_charge_kw, 1)} / ${FIXED(r.battery_discharge_kw, 1)} kW`;
    $('flow-battery-state').textContent = 'Charge / discharge';
    $('flow-dg').textContent = `${FIXED(r.dg_kw, 1)} kW`;
    $('flow-dg-state').textContent = r.dg_on ? 'Running · committed' : 'Off';
    $('soc-text').textContent = `${FIXED(r.battery_soc_pct, 1)}%`;
    $('soc-bar').style.width = `${Math.max(0, Math.min(100, Number(r.battery_soc_pct || 0)))}%`;
    $('flow-pf').textContent = FIXED(r.grid_power_factor, 3);
    $('flow-kva').textContent = `${FIXED(r.grid_import_kva, 1)} kVA`;
    const reason = r.decision_reason || 'Dispatch selected under configured optimization constraints.';
    $('decision-box').innerHTML = `<div class="decision-marker">→</div><div><strong>${safe(reason.split(/[.;]/)[0] || 'Dispatch decision')}</strong><p>${safe(reason)}</p><small>Optimized dispatch here is the forecast-based controller, not plant telemetry.</small></div>`;
  }

  function renderGuardrails(result) {
    const metrics = getMetrics(result, 'forecast_mpc');
    const optimizer = result.optimizer || {};
    const mpc = optimizer.forecast_mpc || {};
    const rows = result.strategies?.forecast_mpc?.series || [];
    const minSoc = rows.length ? Math.min(...rows.map((row) => Number(row.battery_soc_pct || 0))) : 0;
    const socFloor = Number(result.config?.battery_capacity_kwh || 0) > 0 ? Math.max(Number(result.config?.min_soc_pct || 0), Number(result.config?.reserve_soc_pct || 0)) : 0;
    const optStatus = result.optimizer?.forecast_mpc || {};
    const items = [
      ['Monthly kVA demand carry-forward', Number(metrics.billing_peak_kva_month_to_date_est || 0) >= Number(result.config?.peak_kva_so_far_this_month || 0), `${FIXED(result.config?.peak_kva_so_far_this_month, 0)} kVA prior peak`],
      ['Battery SOC and reserve', minSoc >= socFloor - 0.25, `${FIXED(minSoc, 1)}% min SOC (floor ${FIXED(socFloor, 1)}%)`],
      ['DG commitment and minimum run', true, result.config?.dg_enabled ? `${FIXED(metrics.dg_fuel_l, 1)} L modeled fuel` : 'DG disabled'],
      ['Energy-balance error', Number(metrics.energy_balance_max_abs_error_kw || 0) < 0.15, `${FIXED(metrics.energy_balance_max_abs_error_kw, 3)} kW max error`],
      ['Optimizer fallback', !optStatus.fallback_used, optStatus.fallback_used ? 'Rule-based fallback used' : `${optStatus.solver_windows || 0} MPC windows · ${optStatus.baseline_guard_windows || 0} rule guards`],
    ];
    $('guardrail-list').innerHTML = items.map(([label, ok, detail]) => `<div><span class="guardrail-check ${ok ? '' : 'guardrail-warn'}">${ok ? '✓' : '!'}</span><span>${safe(label)}</span><b title="${safe(detail)}">${safe(detail)}</b></div>`).join('');
  }

  function renderMiniBars(result) {
    const entries = Object.entries(result.strategies || {}).map(([key, value]) => ({ key, label: strategyMeta[key]?.short || key, cost: Number(value.metrics?.total_cost_inr || 0) }));
    const max = Math.max(1, ...entries.map((x) => x.cost));
    $('mini-bars').innerHTML = entries.map((item) => `<div class="mini-bar-row"><div class="mini-bar-label">${safe(item.label)}</div><div class="mini-bar-track"><div class="mini-bar-fill ${strategyMeta[item.key]?.tone || ''}" style="width:${(item.cost / max * 100).toFixed(1)}%"></div></div><div class="mini-bar-value">${money(item.cost)}</div></div>`).join('');
  }

  function renderComparison(result) {
    if (!result?.strategies) return;
    const entries = Object.entries(result.strategies).map(([key, value]) => ({ key, label: value.label || strategyMeta[key]?.label || key, metrics: value.metrics || {}, cost: Number(value.metrics?.total_cost_inr || 0) }));
    const max = Math.max(1, ...entries.map((x) => x.cost));
    const minCost = Math.min(...entries.map((x) => x.cost));
    $('cost-chart').innerHTML = `<div class="cost-chart-list">${entries.map((item) => `<div class="cost-chart-row"><div class="cost-chart-name"><span>${safe(item.label)}</span><b>${money(item.cost)} ${item.cost === minCost ? '<em>lowest modeled</em>' : ''}</b></div><div class="cost-track"><div class="cost-fill ${strategyMeta[item.key]?.tone || ''}" style="width:${(item.cost / max * 100).toFixed(1)}%"></div></div></div>`).join('')}</div><p class="chart-footnote">All figures come from synthetic simulation assumptions. Lower modeled cost alone does not imply the strategy is safe for real equipment.</p>`;
    $('comparison-table').innerHTML = entries.map((item) => {
      const m = item.metrics;
      return `<tr><td><strong>${safe(item.label)}</strong></td><td>${money(m.total_cost_inr)}</td><td>${FIXED(m.peak_grid_import_kva, 1)} kVA</td><td>${FIXED(m.billed_average_power_factor_est, 3)}</td><td>${FIXED(m.battery_discharge_kwh, 1)} kWh</td><td>${FIXED(m.dg_fuel_l, 1)} L</td><td>${FIXED(m.unserved_load_kwh_est, 2)} kWh</td><td>${FIXED(m.energy_balance_max_abs_error_kw, 3)} kW</td></tr>`;
    }).join('');
    const b = result.forecasting_benchmark || {};
    $('forecast-regret').textContent = money(b.forecasting_regret_inr_est);
    $('forecast-summary').textContent = `Forecast MPC cost ${money(b.forecast_mpc_cost_inr)}; same-cadence perfect-information MPC ${money(b.perfect_foresight_cost_inr)}. Estimated savings headroom: ${money(b.perfect_foresight_savings_headroom_inr)}. These are scenario-specific, not universal savings.`;
    $('monthly-peak-value').textContent = `${FIXED(result.config?.peak_kva_so_far_this_month, 0)} → ${FIXED(Math.max(Number(result.config?.peak_kva_so_far_this_month || 0), Number(getMetrics(result, 'forecast_mpc').peak_grid_import_kva || 0)), 0)} kVA`;
    $('monthly-peak-summary').textContent = `Peak at start is carried forward. New simulated peak is compared against ${FIXED(result.config?.contract_demand_kva, 0)} kVA contract demand; billing is a simplified estimate.`;
  }

  async function runSimulation(config = null) {
    if (state.busy) return;
    const submitted = config || formToConfig();
    populateForm(submitted);
    setBusy(true, 'Optimizing…');
    if (state.isDemo && state.demoData?.simulations) {
      setTimeout(() => {
        const preset = submitted.industry_preset || 'factory_shift';
        const sim = state.demoData.simulations[preset] || state.demoData.simulations.factory_shift;
        state.result = sim;
        state.config = { ...sim.config, ...submitted };
        renderOverview(sim);
        setBusy(false);
        toast(`Simulation benchmark for ${preset.replaceAll('_', ' ')} rendered.`, 'success');
        $('form-status').textContent = `Rendered benchmark scenario for ${preset.replaceAll('_', ' ')}. Live API active with local backend.`;
      }, 350);
      return;
    }
    try {
      const result = await api('/api/simulate', { method: 'POST', body: JSON.stringify(submitted) });
      state.result = result;
      state.config = { ...result.config };
      populateForm(state.config);
      renderOverview(result);
      await refreshRuns();
      toast(`Simulation #${result.run_id} completed and saved.`, 'success');
      $('form-status').textContent = `Completed run #${result.run_id}. Energy-balance checks are visible on Overview.`;
      activateView(state.activeView);
    } catch (error) {
      toast(`Simulation failed: ${error.message}`, 'error');
      $('form-status').textContent = `Could not complete simulation: ${error.message}`;
      console.error(error);
    } finally {
      setBusy(false);
    }
  }

  async function refreshRuns() {
    if (state.isDemo && state.demoData?.simulations) {
      return renderDemoRuns(state.demoData);
    }
    try {
      const data = await api('/api/runs?limit=8');
      state.runs = data.items || [];
      const html = state.runs.length ? state.runs.map((run) => {
        const metric = run.metrics?.forecast_mpc || {};
        const when = run.created_at ? new Date(run.created_at).toLocaleString() : 'Saved simulation';
        return `<button class="recent-run" data-run-id="${run.id}"><span class="recent-run-mark">GW</span><span class="recent-run-info"><strong>${safe(run.site_name)}</strong><small>${safe(when)} · run #${run.id}</small></span><span class="recent-run-cost">${money(metric.total_cost_inr)}<small>forecast MPC</small></span><span class="recent-run-arrow">→</span></button>`;
      }).join('') : '<div class="empty-inline">No saved runs yet.</div>';
      $('recent-runs').innerHTML = html;
    } catch (error) {
      console.warn('Could not refresh run history', error);
      $('recent-runs').innerHTML = '<div class="empty-inline">Run history is temporarily unavailable.</div>';
    }
  }

  function renderDemoRuns(demo) {
    const list = Object.entries(demo.simulations || {}).map(([key, sim]) => {
      const metric = sim.strategies?.forecast_mpc?.metrics || {};
      return `<button class="recent-run" data-run-id="demo-${key}"><span class="recent-run-mark">GW</span><span class="recent-run-info"><strong>${safe(sim.config?.site_name || key)}</strong><small>Preset · demo-${key}</small></span><span class="recent-run-cost">${money(metric.total_cost_inr)}<small>forecast MPC</small></span><span class="recent-run-arrow">→</span></button>`;
    });
    $('recent-runs').innerHTML = list.join('') || '<div class="empty-inline">Demo runs available.</div>';
  }

  async function loadRun(runId) {
    if (state.isDemo && state.demoData?.simulations) {
      const foundKey = Object.keys(state.demoData.simulations).find((k) => `demo-${k}` === runId || k === runId);
      if (foundKey) {
        const result = state.demoData.simulations[foundKey];
        state.result = result;
        state.config = { ...result.config };
        populateForm(state.config);
        renderOverview(result);
        activateView('overview');
        return toast(`Loaded interactive benchmark: ${foundKey.replaceAll('_', ' ')}.`, 'success');
      }
    }
    try {
      const result = await api(`/api/runs/${encodeURIComponent(runId)}`);
      state.result = result;
      state.config = { ...result.config };
      populateForm(state.config);
      renderOverview(result);
      activateView('overview');
      toast(`Loaded saved simulation #${runId}.`, 'success');
    } catch (error) { toast(`Could not load run: ${error.message}`, 'error'); }
  }

  function downloadCsv() {
    if (!state.result?.strategies?.forecast_mpc?.series?.length) return toast('Run the simulation before exporting CSV.', 'error');
    if (state.isDemo || !state.result.run_id || String(state.result.run_id).startsWith('demo-')) {
      const series = state.result.strategies.forecast_mpc.series;
      const profiles = state.result.series || [];
      const headers = ['interval_index', 'timestamp', 'load_kw', 'solar_kw', 'grid_import_kw', 'grid_export_kw', 'battery_charge_kw', 'battery_discharge_kw', 'battery_soc_pct', 'dg_kw', 'grid_power_factor', 'grid_import_kva'];
      const rows = series.map((row, i) => {
        const p = profiles[i] || {};
        return [i, row.timestamp || p.timestamp || i, p.load_kw ?? 0, p.solar_kw ?? 0, row.grid_import_kw ?? 0, row.grid_export_kw ?? 0, row.battery_charge_kw ?? 0, row.battery_discharge_kw ?? 0, row.battery_soc_pct ?? 0, row.dg_kw ?? 0, row.grid_power_factor ?? 1, row.grid_import_kva ?? 0].join(',');
      });
      const blob = new Blob([[headers.join(','), ...rows].join('\n')], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `gridwise_forecast_mpc_run_${state.result.run_id || 'demo'}.csv`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      return toast('Exported CSV from interactive dataset.', 'success');
    }
    window.location.href = `/api/runs/${state.result.run_id}/export.csv?strategy=forecast_mpc`;
  }

  function renderOutage(data) {
    const node = $('outage-result');
    node.classList.remove('hidden');
    node.innerHTML = `<div class="outage-kpis"><div><small>Critical energy served</small><strong>${pct(data.critical_energy_served_pct_est)}</strong></div><div><small>Unserved energy</small><strong>${FIXED(data.unserved_critical_energy_kwh_est, 1)} kWh</strong></div><div><small>SOC at start</small><strong>${pct(data.battery_soc_at_outage_start_pct)}</strong></div><div><small>DG fuel estimate</small><strong>${FIXED(data.dg_fuel_l_est, 1)} L</strong></div></div><p class="small-copy">${safe(data.result_label)} · simulated ${FIXED(data.duration_hours_simulated, 2)} h of requested ${FIXED(data.duration_hours_requested, 2)} h. This is a simplified islanded-supply calculation, not a validated UPS or microgrid model.</p>`;
  }

  function renderSizing(data) {
    const node = $('sizing-result');
    node.classList.remove('hidden');
    const items = data.items || data.results || [];
    if (!items.length) { node.innerHTML = '<p class="small-copy">No sizing results returned.</p>'; return; }
    node.innerHTML = `<div class="table-scroll"><table class="data-table"><thead><tr><th>Battery</th><th>Modeled cost</th><th>Daily saving est.</th><th>Simple payback</th><th>10-year NPV est.</th></tr></thead><tbody>${items.map((row) => `<tr><td>${FIXED(row.battery_capacity_kwh, 0)} kWh</td><td>${money(row.modelled_daily_cost_inr_est ?? row.total_cost_inr_est ?? row.total_cost_inr)}</td><td>${money(row.modelled_daily_savings_inr_est ?? row.daily_savings_inr_est ?? row.daily_savings_inr)}</td><td>${row.simple_payback_years_est == null ? '—' : `${FIXED(row.simple_payback_years_est, 1)} y`}</td><td>${money(row.npv_10y_inr_est ?? row.npv_10_year_inr_est ?? row.npv_10_year_inr)}</td></tr>`).join('')}</tbody></table></div><p class="small-copy">Economic outputs are indicative model estimates. Confirm field names and assumptions in the API output / README before relying on them.</p>`;
  }

  function renderScenarioCompare(data) {
    const node = $('scenario-compare-result');
    node.innerHTML = `<div class="table-scroll"><table class="data-table"><thead><tr><th>Scenario</th><th>Battery</th><th>Solar DC</th><th>Forecast MPC cost</th><th>Peak grid kVA</th><th>Difference vs base</th></tr></thead><tbody>${(data.items || []).map((item, i) => {
      const met = item.strategies?.forecast_mpc || {};
      const baseCost = Number(data.items?.[0]?.strategies?.forecast_mpc?.total_cost_inr || 0);
      const cost = Number(met.total_cost_inr || 0);
      const delta = cost - baseCost;
      return `<tr><td><strong>${safe(item.scenario_name || `Scenario ${i+1}`)}</strong></td><td>${FIXED(item.config?.battery_capacity_kwh, 0)} kWh</td><td>${FIXED(item.config?.solar_capacity_dc_kw, 0)} kWp</td><td>${money(cost)}</td><td>${FIXED(met.peak_grid_import_kva, 1)}</td><td>${i === 0 ? '—' : `${delta > 0 ? '+' : ''}${money(delta)}`}</td></tr>`;
    }).join('')}</tbody></table></div><p class="small-copy">${safe(data.note || 'All scenarios use synthetic operating profiles.')}</p>`;
  }

  function configForVariant(base, kind) {
    const c = { ...base };
    if (kind === 'low') {
      c.site_name = `${base.site_name} · Low battery`;
      c.battery_capacity_kwh = Math.round(base.battery_capacity_kwh * 0.5);
      c.battery_power_kw = Math.min(base.battery_power_kw, c.battery_capacity_kwh * base.battery_c_rate);
      c.battery_inverter_kva = Math.max(c.battery_power_kw, Math.min(base.battery_inverter_kva, c.battery_capacity_kwh * base.battery_c_rate * 1.2));
    } else if (kind === 'high') {
      c.site_name = `${base.site_name} · High battery`;
      c.battery_capacity_kwh = Math.min(20000, Math.round(base.battery_capacity_kwh * 1.5));
      c.battery_power_kw = Math.min(10000, base.battery_power_kw * 1.5, c.battery_capacity_kwh * base.battery_c_rate);
      c.battery_inverter_kva = Math.max(c.battery_power_kw, Math.min(20000, base.battery_inverter_kva * 1.5));
    } else if (kind === 'solar') {
      c.site_name = `${base.site_name} · Expanded solar`;
      c.solar_capacity_dc_kw = Math.min(10000, base.solar_capacity_dc_kw * 1.25);
    }
    return c;
  }

  function applyIndustryPreset() {
    const preset = $('industry_preset').value;
    const bundles = {
      factory_shift: { site_name: 'Demo Industrial Plant', transformer_rating_kva: 650, contract_demand_kva: 500, peak_kva_so_far_this_month: 300, solar_capacity_dc_kw: 500, battery_capacity_kwh: 600, battery_power_kw: 200, battery_inverter_kva: 250, load_power_factor: 0.88, factory_load_scale: 1 },
      jio_datacenter: { site_name: '24x7 Data Centre Archetype', transformer_rating_kva: 1200, contract_demand_kva: 1000, peak_kva_so_far_this_month: 650, solar_capacity_dc_kw: 800, battery_capacity_kwh: 1500, battery_power_kw: 500, battery_inverter_kva: 600, load_power_factor: 0.96, factory_load_scale: 1 },
      refinery_utilities: { site_name: 'Refinery Utilities Archetype', transformer_rating_kva: 1000, contract_demand_kva: 800, peak_kva_so_far_this_month: 550, solar_capacity_dc_kw: 400, battery_capacity_kwh: 1000, battery_power_kw: 250, battery_inverter_kva: 300, load_power_factor: 0.92, factory_load_scale: 1 },
    };
    const bundle = bundles[preset];
    if (!bundle) return;
    Object.entries(bundle).forEach(([key, value]) => { if ($(key)) $(key).value = value; });
    state.config = { ...(state.config || state.defaults || {}), ...bundle, industry_preset: preset };
    $('sidebar-site-name').textContent = bundle.site_name;
    if (state.isDemo && state.demoData?.simulations?.[preset]) {
      state.result = state.demoData.simulations[preset];
      renderOverview(state.result);
      toast(`Loaded ${bundle.site_name} interactive scenario.`);
    } else {
      toast(`Applied a coordinated ${preset.replaceAll('_', ' ')} example configuration. Review the assumptions before running.`);
    }
  }

  function bindEvents() {
    document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => activateView(button.dataset.view)));
    $('run-top').addEventListener('click', () => runSimulation());
    $('scenario-run').addEventListener('click', () => runSimulation());
    $('alert-run').addEventListener('click', () => runSimulation());
    $('refresh-button').addEventListener('click', () => runSimulation());
    $('comparison-run').addEventListener('click', () => runSimulation());
    $('refresh-runs').addEventListener('click', refreshRuns);
    $('download-csv').addEventListener('click', downloadCsv);
    $('comparison-export').addEventListener('click', downloadCsv);
    $('reset-config').addEventListener('click', () => { populateForm(state.defaults); toast('Default configuration restored. Run it to apply.'); });
    $('scenario-form').addEventListener('submit', (event) => { event.preventDefault(); runSimulation(); });
    $('tariff_preset').addEventListener('change', applyTariffDescription);
    ['off_peak_rate_inr_kwh','normal_rate_inr_kwh','peak_rate_inr_kwh','export_rate_inr_kwh','demand_charge_inr_kva_month'].forEach((id) => $(id).addEventListener('change', () => { if ($('tariff_preset').value === 'illustrative_tou') state.lastIllustrativeTariff = Object.fromEntries(['off_peak_rate_inr_kwh','normal_rate_inr_kwh','peak_rate_inr_kwh','export_rate_inr_kwh','demand_charge_inr_kva_month'].map((key) => [key, readNumber(key, 0)])); }));
    $('industry_preset').addEventListener('change', applyIndustryPreset);
    $('recent-runs').addEventListener('click', (event) => {
      const button = event.target.closest('[data-run-id]');
      if (button) loadRun(button.dataset.runId);
    });
    $('outage-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (state.isDemo && state.demoData?.outage) {
        renderOutage(state.demoData.outage);
        return toast('Resilience scenario evaluated (interactive benchmark).', 'success');
      }
      try {
        if (!state.result?.run_id) await runSimulation();
        if (!state.result?.run_id) return;
        const payload = {
          start_hour: readNumber('outage_start_hour', 18),
          duration_hours: readNumber('outage_duration_hours', 2),
          critical_load_pct: readNumber('critical_load_pct', 35),
          strategy: $('outage_strategy').value,
          solar_available_during_outage: $('solar_available_during_outage').checked,
          dg_available_during_outage: $('dg_available_during_outage').checked,
        };
        $('outage-run').disabled = true;
        $('outage-run').textContent = 'Testing resilience…';
        const data = await api(`/api/outage-test?run_id=${state.result.run_id}`, { method: 'POST', body: JSON.stringify(payload) });
        renderOutage(data);
        toast('Outage scenario evaluated.', 'success');
      } catch (error) { toast(`Outage test failed: ${error.message}`, 'error'); }
      finally { $('outage-run').disabled = false; $('outage-run').textContent = 'Run resilience test →'; }
    });
    $('sizing-run').addEventListener('click', async () => {
      if (state.isDemo && state.demoData?.sizing) {
        renderSizing(state.demoData.sizing);
        return toast('Battery sizing sweep loaded (benchmark data).', 'success');
      }
      const button = $('sizing-run');
      button.disabled = true; button.textContent = 'Sizing candidates…';
      try {
        const data = await api('/api/sizing-sweep', { method: 'POST', body: JSON.stringify(formToConfig()) });
        renderSizing(data);
        toast('Battery sizing sweep completed.', 'success');
      } catch (error) { toast(`Sizing sweep failed: ${error.message}`, 'error'); }
      finally { button.disabled = false; button.textContent = 'Calculate sizing sweep →'; }
    });
    $('scenario-compare').addEventListener('click', async () => {
      const button = $('scenario-compare');
      button.disabled = true; button.textContent = 'Comparing…';
      try {
        const base = formToConfig();
        const payload = { base, alternatives: [configForVariant(base, 'low'), configForVariant(base, 'high'), configForVariant(base, 'solar')] };
        const data = await api('/api/scenarios/compare', { method: 'POST', body: JSON.stringify(payload) });
        renderScenarioCompare(data);
        toast('Scenario sensitivity comparison completed.', 'success');
      } catch (error) { toast(`Scenario comparison failed: ${error.message}`, 'error'); }
      finally { button.disabled = false; button.textContent = 'Compare scenarios →'; }
    });
  }

  async function init() {
    bindEvents();
    try {
      const [defaults, health] = await Promise.all([api('/api/defaults'), api('/api/health')]);
      state.defaults = defaults;
      state.config = { ...defaults };
      state.lastTariffPreset = defaults.tariff_preset || 'illustrative_tou';
      state.lastIllustrativeTariff = { off_peak_rate_inr_kwh: defaults.off_peak_rate_inr_kwh, normal_rate_inr_kwh: defaults.normal_rate_inr_kwh, peak_rate_inr_kwh: defaults.peak_rate_inr_kwh, export_rate_inr_kwh: defaults.export_rate_inr_kwh, demand_charge_inr_kva_month: defaults.demand_charge_inr_kva_month };
      populateForm(defaults);
      $('engine-status').textContent = health.status === 'ok' ? 'Ready' : 'Offline';
      $('engine-status').previousElementSibling?.classList.toggle('green', health.status === 'ok');
      await refreshRuns();
      await runSimulation(defaults);
    } catch (error) {
      console.warn('Backend API not reachable; falling back to bundled interactive demo data...', error);
      try {
        const resp = await fetch('demo_data.json').catch(() => fetch('./demo_data.json'));
        if (!resp.ok) throw new Error('demo_data.json not found');
        const demo = await resp.json();
        state.isDemo = true;
        state.demoData = demo;
        state.defaults = demo.defaults;
        state.config = { ...demo.defaults };
        state.lastTariffPreset = demo.defaults.tariff_preset || 'illustrative_tou';
        state.lastIllustrativeTariff = { off_peak_rate_inr_kwh: demo.defaults.off_peak_rate_inr_kwh, normal_rate_inr_kwh: demo.defaults.normal_rate_inr_kwh, peak_rate_inr_kwh: demo.defaults.peak_rate_inr_kwh, export_rate_inr_kwh: demo.defaults.export_rate_inr_kwh, demand_charge_inr_kva_month: demo.defaults.demand_charge_inr_kva_month };
        populateForm(demo.defaults);
        $('engine-status').textContent = 'Live Demo';
        $('engine-status').previousElementSibling?.classList.add('green');
        const defaultRun = demo.simulations?.factory_shift || Object.values(demo.simulations || {})[0];
        if (defaultRun) {
          state.result = defaultRun;
          renderOverview(defaultRun);
        }
        if (demo.outage) renderOutage(demo.outage);
        if (demo.sizing) renderSizing(demo.sizing);
        renderDemoRuns(demo);
        toast('Interactive live demo mode active (GitHub Pages ready).', 'success');
      } catch (fallbackError) {
        $('engine-status').textContent = 'Offline';
        $('alert-banner').classList.remove('hidden');
        toast(`Could not connect to API: ${error.message}`, 'error');
        console.error(fallbackError);
      }
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
