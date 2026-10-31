"""Bridge services connecting the Web Dashboard to Grid BESS and Solarnex BESS engines.

Imports and coordinates the optimization modules without altering any core project code.
"""

from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import sys

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Configure Matplotlib for headless server execution
os.environ.setdefault('MPLCONFIGDIR', '/tmp/.matplotlib')
import matplotlib
matplotlib.use('Agg')

import numpy as np
import pandas as pd

# Import from existing modules directly without modifying them
from grid_bess_optimizer.model import (
    select_day,
    optimize_bess_arbitrage,
    verify_dispatch as verify_grid_dispatch,
)
from grid_bess_optimizer.plots import (
    plot_prices_curve as grid_plot_prices,
    plot_dispatch as grid_plot_dispatch,
)

from solarnex_bess_engine.model import (
    load_market_data,
    SolarnexBESSOptimizer,
    SolarnexRollingSimulator,
    verify_results as verify_solarnex_results,
    export_simulation_to_csv,
)
from solarnex_bess_engine.plots import (
    plot_prices_curve as solarnex_plot_prices,
    plot_dispatch as solarnex_plot_dispatch,
    animate_dispatch,
)

logger = logging.getLogger(__name__)


def get_available_datasets():
    """Scan and return all pre-converted hourly datasets."""
    converted_dir = REPO_ROOT / 'datasets' / 'converted'
    datasets = []

    if converted_dir.is_dir():
        for csv_file in sorted(converted_dir.glob('*_hourly.csv')):
            try:
                size_kb = round(csv_file.stat().st_size / 1024, 1)
                meta_file = csv_file.with_name(csv_file.name.replace('.csv', '.metadata.json'))
                has_meta = meta_file.is_file()
                display_name = csv_file.name.replace('_hourly.csv', '').replace('_', ' ').title()

                datasets.append({
                    'id': str(csv_file.relative_to(REPO_ROOT)),
                    'path': str(csv_file.resolve()),
                    'filename': csv_file.name,
                    'name': display_name,
                    'size_kb': size_kb,
                    'has_metadata': has_meta,
                })
            except Exception as exc:
                logger.warning("Could not read dataset %s: %s", csv_file, exc)

    return datasets


def get_dataset_date_range(dataset_rel_or_abs_path):
    """Inspect dataset and return min date, max date, and total hourly records."""
    path = Path(dataset_rel_or_abs_path)
    if not path.is_absolute():
        path = REPO_ROOT / path

    if not path.is_file():
        raise FileNotFoundError(f"Dataset file not found: {path}")

    # Read timestamps with efficient parsing
    df = pd.read_csv(path, usecols=['timestamp_local_IN'])
    raw_times = df['timestamp_local_IN'].astype(str)
    timestamps = pd.to_datetime(raw_times, utc=True).dt.tz_convert('Asia/Kolkata')

    dates = timestamps.dt.date
    min_date = str(dates.min())
    max_date = str(dates.max())
    total_hours = len(df)
    unique_days = sorted(list(set(str(d) for d in dates)))

    return {
        'min_date': min_date,
        'max_date': max_date,
        'total_hours': total_hours,
        'available_dates': unique_days,
    }


def run_grid_bess_service(
    dataset_path,
    selected_date_str,
    capacity_mwh,
    max_power_mw,
    efficiency,
    output_dir,
):
    """Execute single-day Grid BESS arbitrage optimization and export artifacts."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    file_path = Path(dataset_path)
    if not file_path.is_absolute():
        file_path = REPO_ROOT / file_path

    if not file_path.is_file():
        raise FileNotFoundError(f"Dataset not found: {file_path}")

    target_date = date.fromisoformat(selected_date_str)
    frame = pd.read_csv(file_path)
    frame, timestamps, prices, selected_date = select_day(frame, target_date)

    # Solve MILP
    results, status = optimize_bess_arbitrage(
        prices=prices,
        capacity_mwh=capacity_mwh,
        max_power_mw=max_power_mw,
        efficiency=efficiency,
    )
    verify_grid_dispatch(results, capacity_mwh, max_power_mw, efficiency)

    # Attach computed values
    timestamps_arr = timestamps.to_numpy()
    results.insert(0, 'timestamp_local_IN', timestamps_arr)
    results['Price (INR/kWh)'] = prices / 1000.0
    results['Charge cost (INR)'] = results['Charge (MW)'] * prices
    results['Discharge revenue (INR)'] = results['Discharge (MW)'] * prices
    results['Gross arbitrage (INR)'] = results['Discharge revenue (INR)'] - results['Charge cost (INR)']
    results['Cumulative gross arbitrage (INR)'] = results['Gross arbitrage (INR)'].cumsum()

    # Export CSVs to temporary session directory
    results.to_csv(output_path / 'bess_results.csv', index=False)

    daily = results.copy()
    daily['date_IST'] = timestamps.dt.date.to_numpy()
    daily_grouped = daily.groupby('date_IST').agg(
        charged_mwh=('Charge (MW)', 'sum'),
        discharged_mwh=('Discharge (MW)', 'sum'),
        charge_cost_inr=('Charge cost (INR)', 'sum'),
        discharge_revenue_inr=('Discharge revenue (INR)', 'sum'),
        gross_arbitrage_inr=('Gross arbitrage (INR)', 'sum'),
    )
    daily_grouped.to_csv(output_path / 'daily_summary.csv')

    # Summary metrics
    price_source = frame['price_source'].unique().tolist() if 'price_source' in frame else ['unspecified']
    eta = np.sqrt(efficiency)
    summary = {
        'status': status,
        'currency': 'INR',
        'timezone': 'Asia/Kolkata',
        'start': str(timestamps.iloc[0]),
        'last_hour_start': str(timestamps.iloc[-1]),
        'hours': len(results),
        'selected_date_ist': str(selected_date),
        'capacity_mwh': float(capacity_mwh),
        'max_power_mw': float(max_power_mw),
        'round_trip_efficiency': float(efficiency),
        'initial_soc_mwh': float(capacity_mwh / 2.0),
        'final_soc_mwh': float(results['SoC (MWh)'].iloc[-1]),
        'charged_mwh': float(results['Charge (MW)'].sum()),
        'discharged_mwh': float(results['Discharge (MW)'].sum()),
        'charge_cost_inr': float(results['Charge cost (INR)'].sum()),
        'discharge_revenue_inr': float(results['Discharge revenue (INR)'].sum()),
        'gross_arbitrage_inr': float(results['Gross arbitrage (INR)'].sum()),
        'equivalent_full_discharge_cycles': float(
            results['Discharge (MW)'].sum() / eta / capacity_mwh
        ),
        'price_source': price_source,
        'model': 'Grid BESS Single-Day Perfect Foresight Arbitrage MILP',
    }
    (output_path / 'summary.json').write_text(json.dumps(summary, indent=2))

    # Generate static chart figures in the temporary folder
    grid_plot_prices(results['Hour'], prices, output_dir=output_path, show=False)
    grid_plot_dispatch(
        results['Hour'],
        results['Discharge (MW)'],
        results['Charge (MW)'],
        prices,
        results['SoC (MWh)'],
        capacity_mwh=capacity_mwh,
        output_dir=output_path,
        show=False,
    )

    # Format hourly data for interactive chart serialization
    hourly_records = []
    for _, row in results.iterrows():
        hourly_records.append({
            'hour': int(row['Hour']),
            'timestamp': str(row['timestamp_local_IN']),
            'price_mwh': round(float(row['Price (INR/MWh)']), 2),
            'price_kwh': round(float(row['Price (INR/kWh)']), 3),
            'charge_mw': round(float(row['Charge (MW)']), 4),
            'discharge_mw': round(float(row['Discharge (MW)']), 4),
            'soc_mwh': round(float(row['SoC (MWh)']), 4),
            'charge_cost_inr': round(float(row['Charge cost (INR)']), 2),
            'discharge_revenue_inr': round(float(row['Discharge revenue (INR)']), 2),
            'gross_arbitrage_inr': round(float(row['Gross arbitrage (INR)']), 2),
            'cumulative_arbitrage_inr': round(float(row['Cumulative gross arbitrage (INR)']), 2),
        })

    return {
        'status': 'success',
        'module': 'grid_bess',
        'summary': summary,
        'hourly': hourly_records,
        'files': [
            {'name': 'bess_results.csv', 'type': 'csv'},
            {'name': 'daily_summary.csv', 'type': 'csv'},
            {'name': 'summary.json', 'type': 'json'},
            {'name': 'market_prices_chart.png', 'type': 'image/png'},
            {'name': 'bess_dispatch_chart.png', 'type': 'image/png'},
        ],
    }


def run_solarnex_bess_service(
    dataset_path,
    date_mode,
    selected_date_str,
    start_date_str,
    end_date_str,
    capacity_mwh,
    max_power_mw,
    rte,
    horizon_hours,
    step_hours,
    spike_filter,
    animate,
    animation_hours,
    gif_duration,
    output_dir,
):
    """Execute Solarnex rolling-horizon dispatch simulation and export artifacts."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    file_path = Path(dataset_path)
    if not file_path.is_absolute():
        file_path = REPO_ROOT / file_path

    if not file_path.is_file():
        raise FileNotFoundError(f"Dataset not found: {file_path}")

    # Resolve date filter arguments
    single_date = None
    start_date = None
    end_date = None

    if date_mode == 'single' and selected_date_str:
        single_date = date.fromisoformat(selected_date_str)
    elif date_mode == 'range' and start_date_str and end_date_str:
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)

    data = load_market_data(
        file_path,
        selected_date=single_date,
        start_date=start_date,
        end_date=end_date,
    )

    optimizer = SolarnexBESSOptimizer(capacity_mwh, max_power_mw, rte)
    simulator = SolarnexRollingSimulator(
        data,
        optimizer,
        horizon_hours=horizon_hours,
        step_hours=step_hours,
        spike_filter=spike_filter,
    )

    results = simulator.run_simulation()
    results.attrs['price_source'] = data.attrs.get('price_source', ['unspecified'])
    verify_solarnex_results(results, optimizer)

    # Export main CSV and daily summary to session directory
    csv_file = output_path / 'solarnex_bess_simulation_output.csv'
    export_simulation_to_csv(results, csv_file)

    daily = results.groupby(results.index.date).agg(
        charged_mwh=('p_charge', 'sum'),
        discharged_mwh=('p_discharge', 'sum'),
        baseline_cost_inr=('baseline_cost_inr', 'sum'),
        optimized_cost_inr=('optimized_cost_inr', 'sum'),
        gross_savings_inr=('savings_inr', 'sum'),
    )
    daily.index.name = 'date_IST'
    daily.to_csv(output_path / 'daily_summary.csv')

    filtered_hours = int((np.abs(results['price'] - results['optimization_price']) > 1e-8).sum())
    savings_sum = float(results['savings_inr'].sum())
    baseline_cost_sum = float(results['baseline_cost_inr'].sum())
    optimized_cost_sum = float(results['optimized_cost_inr'].sum())
    pct_savings = (savings_sum / baseline_cost_sum * 100.0) if baseline_cost_sum > 0 else 0.0

    summary = {
        'status': results.attrs.get('status', 'Optimal'),
        'currency': 'INR',
        'timezone': 'Asia/Kolkata',
        'start': str(results.index[0]),
        'last_hour_start': str(results.index[-1]),
        'hours': len(results),
        'start_date': str(start_date) if start_date else (str(single_date) if single_date else None),
        'end_date': str(end_date) if end_date else (str(single_date) if single_date else None),
        'capacity_mwh': float(capacity_mwh),
        'max_power_mw': float(max_power_mw),
        'round_trip_efficiency': float(rte),
        'horizon_hours': int(horizon_hours),
        'step_hours': int(step_hours),
        'spike_filter_enabled': bool(spike_filter),
        'filtered_committed_hours': filtered_hours,
        'initial_soc_mwh': float(capacity_mwh * 0.5),
        'final_soc_mwh': float(results['soc'].iloc[-1]),
        'charged_mwh': float(results['p_charge'].sum()),
        'discharged_mwh': float(results['p_discharge'].sum()),
        'battery_charge_cost_inr': float((results['p_charge'] * results['price']).sum()),
        'battery_discharge_revenue_inr': float((results['p_discharge'] * results['price']).sum()),
        'gross_arbitrage_savings_inr': savings_sum,
        'baseline_cost_inr': baseline_cost_sum,
        'optimized_cost_inr': optimized_cost_sum,
        'percentage_cost_reduction': round(pct_savings, 4),
        'model': 'Solarnex BESS Rolling-Horizon Dispatch Simulation',
    }
    (output_path / 'summary.json').write_text(json.dumps(summary, indent=2))

    # Generate plot images
    solarnex_plot_prices(results, output_dir=output_path, show=False)
    solarnex_plot_dispatch(results, capacity_mwh=capacity_mwh, output_dir=output_path, show=False)

    gif_created = False
    if animate:
        anim_h = animation_hours
        if not anim_h:
            anim_h = min(len(results), 168 if len(results) > 48 else len(results))
        animate_dispatch(
            results,
            capacity_mwh=capacity_mwh,
            output_dir=output_path,
            hours=anim_h,
            duration_seconds=gif_duration or 40.0,
        )
        gif_created = (output_path / 'dispatch_animation.gif').is_file()

    # Prepare time-series for interactive plotting (with smart decimation if > 500 rows)
    time_series = []
    cumulative_savings = 0.0
    for idx, (ts, row) in enumerate(results.iterrows()):
        savings_row = float(row['savings_inr'])
        cumulative_savings += savings_row
        time_series.append({
            'index': idx,
            'timestamp': str(ts),
            'time_label': ts.strftime('%m-%d %H:%M'),
            'price': round(float(row['price']), 2),
            'opt_price': round(float(row['optimization_price']), 2),
            'load_mw': round(float(row['load']), 3),
            'net_grid_mw': round(float(row['net_grid_mw']), 3),
            'charge_mw': round(float(row['p_charge']), 4),
            'discharge_mw': round(float(row['p_discharge']), 4),
            'soc_mwh': round(float(row['soc']), 4),
            'baseline_cost_inr': round(float(row['baseline_cost_inr']), 2),
            'optimized_cost_inr': round(float(row['optimized_cost_inr']), 2),
            'savings_inr': round(savings_row, 2),
            'cumulative_savings_inr': round(cumulative_savings, 2),
        })

    files = [
        {'name': 'solarnex_bess_simulation_output.csv', 'type': 'csv'},
        {'name': 'daily_summary.csv', 'type': 'csv'},
        {'name': 'summary.json', 'type': 'json'},
        {'name': 'market_prices_chart.png', 'type': 'image/png'},
        {'name': 'bess_dispatch_chart.png', 'type': 'image/png'},
    ]
    if gif_created:
        files.append({'name': 'dispatch_animation.gif', 'type': 'image/gif'})

    return {
        'status': 'success',
        'module': 'solarnex_bess',
        'summary': summary,
        'records': time_series,
        'has_animation': gif_created,
        'files': files,
    }

