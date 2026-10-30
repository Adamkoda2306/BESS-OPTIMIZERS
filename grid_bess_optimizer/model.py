"""Choose a day's battery dispatch and save the results and charts."""

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import pulp

if __package__:
    from . import config
else:
    import config


def _cbc_solver():
    solver_path = os.environ.get('CBC_PATH') or shutil.which('cbc')
    if not solver_path:
        # Homebrew CBC may be installed without being linked onto PATH.
        for prefix in ('/opt/homebrew', '/usr/local'):
            candidate = Path(prefix) / 'opt/cbc/bin/cbc'
            if candidate.is_file():
                solver_path = str(candidate)
                break
    solver = pulp.COIN_CMD(path=solver_path, msg=False) if solver_path else pulp.PULP_CBC_CMD(msg=False)
    if not solver.available():
        raise RuntimeError('CBC is unavailable; install PuLP with its bundled CBC solver')
    return solver


def optimize_bess_arbitrage(
    prices, capacity_mwh=config.CAPACITY_MWH,
    max_power_mw=config.MAX_POWER_MW, efficiency=config.EFFICIENCY,
):
    """Return optimal dispatch and status; SoC is measured at each hour's end.

    Efficiency is round-trip efficiency. The battery starts and ends half full.
    Gross revenue excludes degradation and market fees.
    """
    prices = np.asarray(prices, dtype=float)
    if prices.ndim != 1 or prices.size == 0 or not np.isfinite(prices).all():
        raise ValueError('prices must be a nonempty, finite one-dimensional sequence')
    for name, value in (('capacity_mwh', capacity_mwh), ('max_power_mw', max_power_mw)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    if not math.isfinite(efficiency) or not 0 < efficiency <= 1:
        raise ValueError('efficiency must be in (0, 1]')

    timesteps = range(len(prices))
    eta = math.sqrt(efficiency)
    model = pulp.LpProblem('BESS_Arbitrage_Optimization', pulp.LpMaximize)
    p_charge = pulp.LpVariable.dicts('Charge', timesteps, lowBound=0, upBound=max_power_mw)
    p_discharge = pulp.LpVariable.dicts('Discharge', timesteps, lowBound=0, upBound=max_power_mw)
    soc = pulp.LpVariable.dicts('SoC', range(len(prices) + 1), lowBound=0, upBound=capacity_mwh)
    is_charging = pulp.LpVariable.dicts('IsCharging', timesteps, cat='Binary')

    # Apply losses in the energy balance, so revenue uses grid-side power.
    model += pulp.lpSum(prices[t] * (p_discharge[t] - p_charge[t]) for t in timesteps)
    model += soc[0] == capacity_mwh * 0.5
    for t in timesteps:
        model += soc[t + 1] == soc[t] + p_charge[t] * eta - p_discharge[t] / eta
        model += p_charge[t] <= max_power_mw * is_charging[t]
        model += p_discharge[t] <= max_power_mw * (1 - is_charging[t])
    # Leave the battery half full so the run cannot profit by emptying it.
    model += soc[len(prices)] == capacity_mwh * 0.5

    solver = _cbc_solver()
    try:
        model.solve(solver)
    except (OSError, pulp.PulpSolverError) as exc:
        raise RuntimeError('Cannot run CBC; set CBC_PATH to a compatible CBC executable') from exc
    status = pulp.LpStatus[model.status]
    if status != 'Optimal':
        raise RuntimeError(f'BESS optimization failed: {status}')
    return pd.DataFrame({
        'Hour': list(timesteps),
        'Price (INR/MWh)': prices,
        'Charge (MW)': [p_charge[t].varValue for t in timesteps],
        'Discharge (MW)': [p_discharge[t].varValue for t in timesteps],
        'SoC (MWh)': [soc[t + 1].varValue for t in timesteps],
    }), status


def validate_dataset(frame):
    """Check hourly timestamps and prices before selecting a day."""
    raw_times = frame['timestamp_local_IN'].astype(str)
    if not raw_times.str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$', regex=True).all():
        raise ValueError('Hourly timestamps must include an explicit UTC offset')
    timestamps = pd.to_datetime(raw_times, utc=True, errors='raise').dt.tz_convert(config.TIMEZONE)
    prices = pd.to_numeric(frame['price_inr_mwh'], errors='raise').to_numpy(dtype=float)
    if frame.empty or timestamps.isna().any() or not np.isfinite(prices).all():
        raise ValueError('Dataset must contain valid timestamps and finite INR/MWh prices')
    if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise ValueError('Dataset timestamps must be unique and increasing')
    if not timestamps.diff().iloc[1:].eq(pd.Timedelta(hours=1)).all():
        raise ValueError('Dataset must have consecutive hourly records')
    if not timestamps.eq(timestamps.dt.floor('h')).all():
        raise ValueError('Timestamps must mark the beginning of each hour')
    return timestamps, prices


def select_day(frame, selected_date=None):
    """Select a complete IST day, defaulting to the first date in the file."""
    timestamps, _ = validate_dataset(frame)
    selected_date = selected_date or timestamps.iloc[0].date()
    frame = frame.loc[timestamps.dt.date == selected_date].copy()
    if len(frame) != 24:
        raise ValueError(f'Date {selected_date} must contain exactly 24 IST hourly records; use --date YYYY-MM-DD')
    timestamps, prices = validate_dataset(frame)
    return frame, timestamps, prices, selected_date


def verify_dispatch(results, capacity, power, efficiency):
    """Check the returned dispatch against the battery limits and energy balance."""
    charge = results['Charge (MW)'].to_numpy()
    discharge = results['Discharge (MW)'].to_numpy()
    soc = results['SoC (MWh)'].to_numpy()
    eta = np.sqrt(efficiency)
    previous = np.r_[capacity / 2, soc[:-1]]
    if not np.allclose(soc, previous + charge * eta - discharge / eta, atol=2e-6, rtol=0):
        raise ValueError('Dispatch violates hourly energy balance')
    if (np.minimum(charge, discharge) > 1e-6).any():
        raise ValueError('Simultaneous charging and discharging detected')
    if min(charge.min(), discharge.min(), soc.min()) < -1e-6:
        raise ValueError('Negative power or stored energy detected')
    if max(charge.max(), discharge.max()) > power + 1e-6 or soc.max() > capacity + 1e-6:
        raise ValueError('Dispatch exceeds power or storage bounds')
    if not np.isclose(soc[-1], capacity / 2, atol=1e-6):
        raise ValueError('Battery must finish half full')


def export_results(results, frame, timestamps, prices, selected_date, status, args):
    """Save hourly dispatch, daily totals, and the run settings."""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results.insert(0, 'timestamp_local_IN', timestamps.to_numpy())
    results['Price (INR/kWh)'] = prices / 1000
    # Each row covers one hour, so MW also gives the energy traded in MWh.
    results['Charge cost (INR)'] = results['Charge (MW)'] * prices
    results['Discharge revenue (INR)'] = results['Discharge (MW)'] * prices
    results['Gross arbitrage (INR)'] = results['Discharge revenue (INR)'] - results['Charge cost (INR)']
    results['Cumulative gross arbitrage (INR)'] = results['Gross arbitrage (INR)'].cumsum()
    results.to_csv(args.output_dir / 'bess_results.csv', index=False)

    daily = results.copy()
    daily['date_IST'] = timestamps.dt.date.to_numpy()
    daily = daily.groupby('date_IST').agg(
        charged_mwh=('Charge (MW)', 'sum'),
        discharged_mwh=('Discharge (MW)', 'sum'),
        charge_cost_inr=('Charge cost (INR)', 'sum'),
        discharge_revenue_inr=('Discharge revenue (INR)', 'sum'),
        gross_arbitrage_inr=('Gross arbitrage (INR)', 'sum'))
    daily.to_csv(args.output_dir / 'daily_summary.csv')
    source = ['unspecified_custom_dataset']
    if 'price_source' in frame:
        source = frame['price_source'].unique().tolist()
    summary = {
        'status': status,
        'currency': 'INR',
        'timezone': config.TIMEZONE,
        'start': str(timestamps.iloc[0]),
        'last_hour_start': str(timestamps.iloc[-1]),
        'hours': len(results),
        'selected_date_ist': str(selected_date),
        'capacity_mwh': args.capacity_mwh,
        'dataset': str(args.dataset.resolve()),
        'max_power_mw': args.max_power_mw,
        'round_trip_efficiency': args.efficiency,
        'initial_soc_mwh': args.capacity_mwh / 2,
        'final_soc_mwh': float(results['SoC (MWh)'].iloc[-1]),
        'charged_mwh': float(results['Charge (MW)'].sum()),
        'discharged_mwh': float(results['Discharge (MW)'].sum()),
        'charge_cost_inr': float(results['Charge cost (INR)'].sum()),
        'discharge_revenue_inr': float(results['Discharge revenue (INR)'].sum()),
        'gross_arbitrage_inr': float(results['Gross arbitrage (INR)'].sum()),
        'equivalent_full_discharge_cycles': float(
            results['Discharge (MW)'].sum() / np.sqrt(args.efficiency) / args.capacity_mwh
        ),
        'price_source': source,
        'model': 'Original hourly price-taking arbitrage MILP with perfect foresight over selected horizon',
        'excluded_costs': [
            'battery degradation', 'capital cost', 'exchange and trading fees',
            'transmission and wheeling charges', 'taxes and duties',
        ],
    }
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=config.DATASET)
    parser.add_argument(
        '--date', type=date.fromisoformat,
        help='Select one IST day YYYY-MM-DD; default: first date in dataset',
    )
    parser.add_argument('--output-dir', type=Path, default=config.OUTPUT_DIR)
    parser.add_argument('--capacity-mwh', type=float, default=config.CAPACITY_MWH)
    parser.add_argument('--max-power-mw', type=float, default=config.MAX_POWER_MW)
    parser.add_argument('--efficiency', type=float, default=config.EFFICIENCY)
    parser.add_argument('--no-show', action='store_true', help='Save plots without opening windows')
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        frame = pd.read_csv(args.dataset)
        frame, timestamps, prices, selected_date = select_day(frame, args.date)
        print(f'Selected IST day: {selected_date} (24 hourly intervals)', flush=True)
        results, status = optimize_bess_arbitrage(
            prices, args.capacity_mwh, args.max_power_mw, args.efficiency)
        verify_dispatch(results, args.capacity_mwh, args.max_power_mw, args.efficiency)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))

    summary = export_results(
        results, frame, timestamps, prices, selected_date, status, args)

    # Keep Matplotlib's cache in this project folder.
    os.environ.setdefault('MPLCONFIGDIR', str(config.BASE_DIR / '.matplotlib'))
    if args.no_show:
        import matplotlib
        matplotlib.use('Agg')
    if __package__:
        from .plots import plot_prices_curve, plot_dispatch
    else:
        from plots import plot_prices_curve, plot_dispatch
    plot_prices_curve(results['Hour'], prices, output_dir=args.output_dir, show=not args.no_show)
    plot_dispatch(
        results['Hour'], results['Discharge (MW)'], results['Charge (MW)'], prices,
        results['SoC (MWh)'], capacity_mwh=args.capacity_mwh,
        output_dir=args.output_dir, show=not args.no_show)
    print(f'Optimization: {status}; {len(results)} hourly intervals (IST)')
    print(f'Gross arbitrage revenue: INR {summary["gross_arbitrage_inr"]:,.2f}')
    print(f'Results, daily totals, summary and charts: {args.output_dir}')


if __name__ == '__main__':
    main()
