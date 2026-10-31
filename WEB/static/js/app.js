/**
 * BESS Interactive Web Dashboard - Core Controller
 * Handles Grid BESS and Solarnex BESS state, API dispatch, generated chart images and hourly tables.
 */

// Global Dashboard State
const Dashboard = {
  currentScreen: 'grid_bess', // 'grid_bess' | 'solarnex'
  activeView: 'static_plots',  // 'static_plots' | 'table'
  datasets: [],
  datasetDates: {},
  gridBessData: null,
  solarnexData: null,
};

// Formatting utilities
function formatINR(val) {
  if (val === undefined || val === null || isNaN(val)) return '₹0.00';
  return '₹' + Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatNum(val, dec = 2) {
  if (val === undefined || val === null || isNaN(val)) return '0.00';
  return Number(val).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function showToast(msg) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3500);
}

// Initialize Application on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
  initNavigation();
  initFormSync();
  initSubTabs();
  loadDatasets();
});

// Screen Switching Navigation (Grid BESS vs Solarnex BESS)
function initNavigation() {
  const tabGrid = document.getElementById('nav-grid-bess');
  const tabSolarnex = document.getElementById('nav-solarnex');
  const panelGrid = document.getElementById('panel-grid-bess');
  const panelSolarnex = document.getElementById('panel-solarnex');

  tabGrid.addEventListener('click', () => {
    Dashboard.currentScreen = 'grid_bess';
    tabGrid.classList.add('active');
    tabSolarnex.classList.remove('active');
    panelGrid.style.display = 'block';
    panelSolarnex.style.display = 'none';
    renderCurrentWorkspace();
  });

  tabSolarnex.addEventListener('click', () => {
    Dashboard.currentScreen = 'solarnex';
    tabSolarnex.classList.add('active');
    tabGrid.classList.remove('active');
    panelGrid.style.display = 'none';
    panelSolarnex.style.display = 'block';
    renderCurrentWorkspace();
  });
}

// Keep the selected results view and its visible tab in sync.
function setActiveView(view) {
  Dashboard.activeView = view === 'table' ? 'table' : 'static_plots';
  renderCurrentWorkspace();
}

function initSubTabs() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => setActiveView(btn.dataset.view));
  });
  renderCurrentWorkspace();
}

// Synchronize Sliders with Numeric Inputs
function initFormSync() {
  // Grid BESS inputs
  syncPair('grid-capacity-slider', 'grid-capacity-num', 'grid-capacity-badge', ' MWh');
  syncPair('grid-power-slider', 'grid-power-num', 'grid-power-badge', ' MW');
  syncPair('grid-eff-slider', 'grid-eff-num', 'grid-eff-badge', '', (v) => `${(v * 100).toFixed(0)}%`);

  // Solarnex inputs
  syncPair('sn-capacity-slider', 'sn-capacity-num', 'sn-capacity-badge', ' MWh');
  syncPair('sn-power-slider', 'sn-power-num', 'sn-power-badge', ' MW');
  syncPair('sn-rte-slider', 'sn-rte-num', 'sn-rte-badge', '', (v) => `${(v * 100).toFixed(0)}%`);

  // Solarnex Date Mode radio change
  document.querySelectorAll('input[name="sn-date-mode"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      const mode = e.target.value;
      const singleBox = document.getElementById('sn-single-date-box');
      const rangeBox = document.getElementById('sn-range-date-box');
      if (mode === 'single') {
        singleBox.style.display = 'block';
        rangeBox.style.display = 'none';
      } else if (mode === 'range') {
        singleBox.style.display = 'none';
        rangeBox.style.display = 'block';
      } else {
        singleBox.style.display = 'none';
        rangeBox.style.display = 'none';
      }
    });
  });

  // Solarnex Animation toggle
  const animCheckbox = document.getElementById('sn-animate');
  const animHoursBox = document.getElementById('sn-anim-hours-box');
  if (animCheckbox && animHoursBox) {
    animCheckbox.addEventListener('change', (e) => {
      animHoursBox.style.display = e.target.checked ? 'block' : 'none';
    });
  }

  // Action buttons
  document.getElementById('btn-run-grid').addEventListener('click', runGridBESSOptimization);
  document.getElementById('btn-run-solarnex').addEventListener('click', runSolarnexSimulation);
}

function syncPair(sliderId, numId, badgeId, unit, formatter) {
  const slider = document.getElementById(sliderId);
  const num = document.getElementById(numId);
  const badge = document.getElementById(badgeId);
  if (!slider || !num) return;

  const update = (val) => {
    slider.value = val;
    num.value = val;
    if (badge) {
      badge.textContent = formatter ? formatter(val) : `${val}${unit}`;
    }
  };

  slider.addEventListener('input', (e) => update(e.target.value));
  num.addEventListener('input', (e) => update(e.target.value));
}

// Fetch available datasets from REST API
async function loadDatasets() {
  try {
    const res = await fetch('/api/datasets');
    const json = await res.json();
    if (json.status !== 'success') return;

    Dashboard.datasets = json.datasets;
    const gridSelect = document.getElementById('grid-dataset');
    const snSelect = document.getElementById('sn-dataset');

    gridSelect.innerHTML = '';
    snSelect.innerHTML = '';

    json.datasets.forEach(d => {
      const opt1 = new Option(`${d.name} (${d.size_kb} KB)`, d.id);
      const opt2 = new Option(`${d.name} (${d.size_kb} KB)`, d.id);
      gridSelect.add(opt1);
      snSelect.add(opt2);
    });

    // Default to september 2023 if available
    const defaultDs = json.datasets.find(d => d.filename.includes('september_2023')) || json.datasets[0];
    if (defaultDs) {
      gridSelect.value = defaultDs.id;
      snSelect.value = defaultDs.id;
    }

    gridSelect.addEventListener('change', () => onDatasetChange('grid'));
    snSelect.addEventListener('change', () => onDatasetChange('solarnex'));

    await onDatasetChange('grid');
    await onDatasetChange('solarnex');
  } catch (err) {
    console.error('Failed to load datasets:', err);
  }
}

// Populate available dates for selected dataset
async function onDatasetChange(target) {
  const selectId = target === 'grid' ? 'grid-dataset' : 'sn-dataset';
  const dsPath = document.getElementById(selectId).value;
  if (!dsPath) return;

  try {
    const res = await fetch(`/api/dataset-dates?dataset=${encodeURIComponent(dsPath)}`);
    const json = await res.json();
    if (json.status !== 'success') return;

    const data = json.data;
    Dashboard.datasetDates[dsPath] = data;

    if (target === 'grid') {
      const dateInput = document.getElementById('grid-date');
      dateInput.min = data.min_date;
      dateInput.max = data.max_date;
      // Default to 15th if valid or min_date
      const prefDate = data.available_dates.find(d => d.endsWith('-15')) || data.min_date;
      dateInput.value = prefDate;
    } else {
      const snSingle = document.getElementById('sn-single-date');
      const snStart = document.getElementById('sn-start-date');
      const snEnd = document.getElementById('sn-end-date');

      snSingle.min = data.min_date;
      snSingle.max = data.max_date;
      snSingle.value = data.available_dates.find(d => d.endsWith('-15')) || data.min_date;

      snStart.min = data.min_date;
      snStart.max = data.max_date;
      snStart.value = data.available_dates.find(d => d.endsWith('-15')) || data.min_date;

      snEnd.min = data.min_date;
      snEnd.max = data.max_date;
      snEnd.value = data.available_dates.find(d => d.endsWith('-22')) || data.max_date;
    }
  } catch (err) {
    console.error('Failed to load dates for dataset:', err);
  }
}

// Run Grid BESS Optimization
async function runGridBESSOptimization() {
  const btn = document.getElementById('btn-run-grid');
  const spinner = document.getElementById('grid-spinner');
  btn.disabled = true;
  spinner.style.display = 'inline-block';

  const payload = {
    dataset: document.getElementById('grid-dataset').value,
    date: document.getElementById('grid-date').value,
    capacity_mwh: parseFloat(document.getElementById('grid-capacity-num').value),
    max_power_mw: parseFloat(document.getElementById('grid-power-num').value),
    efficiency: parseFloat(document.getElementById('grid-eff-num').value),
  };

  try {
    const res = await fetch('/api/grid-bess/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const json = await res.json();
    if (json.status !== 'success') {
      alert(`Optimization failed: ${json.message || 'Unknown error'}`);
      return;
    }

    Dashboard.gridBessData = json;
    if (Dashboard.currentScreen === 'grid_bess') setActiveView('static_plots');
    showToast('Grid BESS optimization completed successfully!');
  } catch (err) {
    alert(`Server request failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

// Run Solarnex BESS Simulation
async function runSolarnexSimulation() {
  const btn = document.getElementById('btn-run-solarnex');
  const spinner = document.getElementById('sn-spinner');
  btn.disabled = true;
  spinner.style.display = 'inline-block';

  const dateMode = document.querySelector('input[name="sn-date-mode"]:checked').value;
  const payload = {
    dataset: document.getElementById('sn-dataset').value,
    date_mode: dateMode,
    date: document.getElementById('sn-single-date').value,
    start_date: document.getElementById('sn-start-date').value,
    end_date: document.getElementById('sn-end-date').value,
    capacity_mwh: parseFloat(document.getElementById('sn-capacity-num').value),
    max_power_mw: parseFloat(document.getElementById('sn-power-num').value),
    rte: parseFloat(document.getElementById('sn-rte-num').value),
    horizon_hours: parseInt(document.getElementById('sn-horizon-hours').value),
    step_hours: parseInt(document.getElementById('sn-step-hours').value),
    spike_filter: document.getElementById('sn-spike-filter').checked,
    animate: document.getElementById('sn-animate').checked,
    animation_hours: parseInt(document.getElementById('sn-anim-hours').value) || null,
  };

  try {
    const res = await fetch('/api/solarnex/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const json = await res.json();
    if (json.status !== 'success') {
      alert(`Solarnex simulation failed: ${json.message || 'Unknown error'}`);
      return;
    }

    Dashboard.solarnexData = json;
    if (Dashboard.currentScreen === 'solarnex') setActiveView('static_plots');
    showToast('Solarnex rolling simulation completed successfully!');
  } catch (err) {
    alert(`Server request failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

// Render Results Router
const initialKpis = document.getElementById('kpi-container').innerHTML;
const initialViewport = document.getElementById('chart-viewport').innerHTML;

function renderCurrentWorkspace() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    const selected = btn.dataset.view === Dashboard.activeView;
    btn.classList.toggle('active', selected);
    btn.setAttribute('aria-pressed', String(selected));
  });
  const data = Dashboard.currentScreen === 'grid_bess' ? Dashboard.gridBessData : Dashboard.solarnexData;
  if (!data) {
    document.getElementById('kpi-container').innerHTML = initialKpis;
    document.getElementById('chart-viewport').innerHTML = initialViewport;
    return;
  }
  if (Dashboard.currentScreen === 'grid_bess') renderGridBESSResults(data);
  else renderSolarnexResults(data);
}

// Both engines use the same figure view so tab changes restore every image.
function renderStaticFigures(data, container, dispatchTitle, priceTitle) {
  const viewer = document.createElement('div');
  viewer.className = 'image-viewer';
  const figures = [
    {filename: 'bess_dispatch_chart.png', title: dispatchTitle},
    {filename: 'market_prices_chart.png', title: priceTitle},
  ];
  if (data.has_animation) {
    figures.push({filename: 'dispatch_animation.gif', title: 'Rolling Horizon Animated Dispatch'});
  }

  figures.forEach(({filename, title}) => {
    const url = `/api/files/view?${new URLSearchParams({session_id: data.session_id, file: filename})}`;
    const figure = document.createElement('figure');
    figure.className = 'dispatch-figure';
    const caption = document.createElement('figcaption');
    caption.textContent = title;
    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.title = `Open ${title} at full resolution`;
    const image = document.createElement('img');
    image.className = 'rendered-plot';
    image.alt = title;
    image.addEventListener('error', () => {
      const message = document.createElement('p');
      message.className = 'chart-error';
      message.textContent = `${title} is unavailable. Run the simulation again to regenerate it.`;
      link.replaceWith(message);
    }, {once: true});
    image.src = url;
    link.append(image);
    const scrollRegion = document.createElement('div');
    scrollRegion.className = 'figure-scroll';
    scrollRegion.tabIndex = 0;
    scrollRegion.setAttribute('role', 'region');
    scrollRegion.setAttribute('aria-label', `${title}. Scroll horizontally to inspect the figure.`);
    scrollRegion.append(link);
    figure.append(caption, scrollRegion);
    viewer.append(figure);
  });
  container.replaceChildren(viewer);
}

// ==========================================================================
// Grid BESS Renderers
// ==========================================================================
function renderGridBESSResults(data) {
  const sum = data.summary;

  // 1. Render KPI Ribbon
  const kpiContainer = document.getElementById('kpi-container');
  kpiContainer.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Gross Arbitrage</div>
      <div class="kpi-value" style="color: var(--c-primary-dark);">${formatINR(sum.gross_arbitrage_inr)}</div>
      <div class="kpi-subtext">Status: <b>${sum.status}</b> (24h)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Energy Traded</div>
      <div class="kpi-value">${formatNum(sum.discharged_mwh, 2)} <span style="font-size:0.9rem">MWh</span></div>
      <div class="kpi-subtext">Discharged (${formatNum(sum.charged_mwh, 2)} MWh In)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Discharge Revenue</div>
      <div class="kpi-value">${formatINR(sum.discharge_revenue_inr)}</div>
      <div class="kpi-subtext">Peak energy sales</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Charging Cost</div>
      <div class="kpi-value">${formatINR(sum.charge_cost_inr)}</div>
      <div class="kpi-subtext">Off-peak grid purchases</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Full Cycles</div>
      <div class="kpi-value">${formatNum(sum.equivalent_full_discharge_cycles, 2)}</div>
      <div class="kpi-subtext">Eq. full discharge cycles</div>
    </div>
  `;

  const viewport = document.getElementById('chart-viewport');
  if (Dashboard.activeView === 'table') {
    renderGridTable(data.hourly, viewport);
  } else {
    renderStaticFigures(data, viewport,
      'Optimal Arbitrage Dispatch & Market Prices',
      'Day-Ahead Market Price Curve (IST)');
  }
}

// ==========================================================================
// Solarnex BESS Renderers
// ==========================================================================
function renderSolarnexResults(data) {
  const sum = data.summary;

  // 1. Render KPI Ribbon
  const kpiContainer = document.getElementById('kpi-container');
  kpiContainer.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Gross Arbitrage Savings</div>
      <div class="kpi-value" style="color: var(--c-primary-dark);">${formatINR(sum.gross_arbitrage_savings_inr)}</div>
      <div class="kpi-subtext"><b>${sum.hours}</b> hours evaluated</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost Reduction</div>
      <div class="kpi-value">${formatNum(sum.percentage_cost_reduction, 4)}%</div>
      <div class="kpi-subtext">Overall grid expenditure reduction</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Filtered Hours</div>
      <div class="kpi-value">${sum.filtered_committed_hours}</div>
      <div class="kpi-subtext">Spike filter mitigations</div>
    </div>
  `;

  const viewport = document.getElementById('chart-viewport');
  if (Dashboard.activeView === 'table') {
    renderSolarnexTable(data.records, viewport);
  } else {
    renderStaticFigures(data, viewport,
      'Solarnex Rolling Dispatch & Market Prices',
      'Market Prices & Optimization Filter');
  }
}

// ==========================================================================
// Data Table Renderers
// ==========================================================================
function renderGridTable(hourly, container) {
  let rowsHtml = hourly.map(h => `
    <tr>
      <td style="font-weight:700">H${h.hour} (${h.timestamp.slice(11, 16)})</td>
      <td>₹${formatNum(h.price_mwh, 2)}</td>
      <td>₹${formatNum(h.price_kwh, 3)}</td>
      <td class="${h.charge_mw > 0 ? 'badge-charge' : ''}">${formatNum(h.charge_mw, 4)}</td>
      <td class="${h.discharge_mw > 0 ? 'badge-discharge' : ''}">${formatNum(h.discharge_mw, 4)}</td>
      <td style="color:var(--c-soc); font-weight:600">${formatNum(h.soc_mwh, 4)}</td>
      <td>${formatINR(h.charge_cost_inr)}</td>
      <td>${formatINR(h.discharge_revenue_inr)}</td>
      <td style="font-weight:700; color:${h.gross_arbitrage_inr >= 0 ? 'var(--c-discharge)' : 'var(--c-charge)'}">
        ${formatINR(h.gross_arbitrage_inr)}
      </td>
      <td style="font-weight:700">${formatINR(h.cumulative_arbitrage_inr)}</td>
    </tr>
  `).join('');

  container.innerHTML = `
    <div class="table-wrapper" tabindex="0" role="region" aria-label="Grid BESS hourly results, scroll to view all columns">
      <table class="data-table">
        <thead>
          <tr>
            <th>Interval (IST)</th>
            <th>Price (₹/MWh)</th>
            <th>Price (₹/kWh)</th>
            <th>Charge (MW)</th>
            <th>Discharge (MW)</th>
            <th>SoC (MWh)</th>
            <th>Charge Cost</th>
            <th>Discharge Rev</th>
            <th>Gross Arbitrage</th>
            <th>Cumulative</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
}

function renderSolarnexTable(records, container) {
  // Show first 100 records with notice
  const displayRecords = records.slice(0, 100);
  let rowsHtml = displayRecords.map(r => `
    <tr>
      <td style="font-weight:700">${r.time_label}</td>
      <td>₹${formatNum(r.price, 2)}</td>
      <td>${formatNum(r.load_mw, 1)}</td>
      <td>${formatNum(r.net_grid_mw, 1)}</td>
      <td class="${r.charge_mw > 0 ? 'badge-charge' : ''}">${formatNum(r.charge_mw, 3)}</td>
      <td class="${r.discharge_mw > 0 ? 'badge-discharge' : ''}">${formatNum(r.discharge_mw, 3)}</td>
      <td style="color:var(--c-soc); font-weight:600">${formatNum(r.soc_mwh, 3)}</td>
      <td style="font-weight:700; color:${r.savings_inr >= 0 ? 'var(--c-discharge)' : 'var(--c-charge)'}">
        ${formatINR(r.savings_inr)}
      </td>
      <td style="font-weight:700">${formatINR(r.cumulative_savings_inr)}</td>
    </tr>
  `).join('');

  container.innerHTML = `
    <div style="font-size:0.8rem; color:var(--c-text-muted); margin-bottom:0.4rem;">
      Displaying ${displayRecords.length} intervals of ${records.length} total hours.
    </div>
    <div class="table-wrapper" tabindex="0" role="region" aria-label="Solarnex hourly results, scroll to view all columns">
      <table class="data-table">
        <thead>
          <tr>
            <th>Timestamp (IST)</th>
            <th>Price (₹/MWh)</th>
            <th>Base Load (MW)</th>
            <th>Net Grid (MW)</th>
            <th>Charge (MW)</th>
            <th>Discharge (MW)</th>
            <th>SoC (MWh)</th>
            <th>Hourly Savings</th>
            <th>Cum. Savings</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
}

