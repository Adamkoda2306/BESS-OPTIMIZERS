"""Run battery dispatch over a rolling horizon of hourly prices and load."""

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


def load_market_data(path=config.DATASET_PATH, selected_date=None, start_date=None, end_date=None):
    """Return price (INR/MWh) and grid load (MW), indexed by local timestamps.

    Each source row represents one hourly settlement interval. Require explicit
    timezone offsets and consecutive IST hours; never interpolate market prices.
    """
    frame = pd.read_csv(path)
    raw_times = frame['timestamp_local_IN'].astype(str)
    if not raw_times.str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$', regex=True).all():
        raise ValueError('Indian dataset timestamps must include an explicit UTC offset')
    timestamps = pd.to_datetime(raw_times, utc=True, errors='raise').dt.tz_convert(config.TIMEZONE)
    if timestamps.isna().any() or timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise ValueError('Indian IEX timestamps must be valid and ordered')
    frame.index = pd.DatetimeIndex(timestamps, name='Timestamp')

    if selected_date is not None:
        if start_date is not None or end_date is not None:
            raise ValueError('Cannot specify both selected_date and start_date/end_date')
        start_date = selected_date
        end_date = selected_date

    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError(f'Start date {start_date} cannot be after end date {end_date}')

    if start_date is not None:
        if not (frame.index.date == start_date).any():
            raise ValueError(f'Start date {start_date} not found in dataset')
        frame = frame.loc[frame.index.date >= start_date]

    if end_date is not None:
        if not (frame.index.date == end_date).any():
            raise ValueError(f'End date {end_date} not found in dataset')
        frame = frame.loc[frame.index.date <= end_date]

    result = pd.DataFrame({
        'price': pd.to_numeric(frame['price_inr_mwh'], errors='raise'),
        'load': pd.to_numeric(frame['consumption_mw'], errors='raise'),
    }, index=frame.index)
    if result.empty or not np.isfinite(result.to_numpy()).all():
        raise ValueError('Selected dataset must contain finite price and load records')
    if (result['load'] < 0).any():
        raise ValueError('Grid consumption cannot be negative')
    gaps = result.index.to_series().diff().dropna() != pd.Timedelta(hours=1)
    if gaps.any():
        raise ValueError('Indian dataset must contain consecutive hourly intervals')
    if start_date is not None and end_date is not None:
        expected_days = (end_date - start_date).days + 1
        expected_hours = expected_days * 24
        if len(result) != expected_hours:
            raise ValueError(
                f'Selected date range {start_date} to {end_date} must contain exactly '
                f'{expected_hours} consecutive IST hours (found {len(result)})'
            )
    elif start_date is not None:
        if len(result) % 24 != 0 or result.index[0].hour != 0:
            raise ValueError(f'Selection starting at {start_date} must begin with hour 00:00:00')
    elif end_date is not None:
        if len(result) % 24 != 0 or result.index[-1].hour != 23:
            raise ValueError(f'Selection ending at {end_date} must end with hour 23:00:00')
    result.attrs['price_source'] = ['unspecified_custom_dataset']
    if 'price_source' in frame:
        result.attrs['price_source'] = frame['price_source'].unique().tolist()
    return result


def export_simulation_to_csv(results_df, filename):
    """Export actual and optimization prices, dispatch, and grid accounting."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    results_df.rename(columns={
        'price': 'Market_Price_INR_MWh',
        'optimization_price': 'Optimization_Price_INR_MWh',
        'load': 'Grid_Load_MW',
        'p_charge': 'Battery_Charge_MW',
        'p_discharge': 'Battery_Discharge_MW',
        'soc': 'State_Of_Charge_MWh',
        'net_grid_mw': 'Net_Grid_MW',
        'baseline_cost_inr': 'Baseline_Cost_INR',
        'optimized_cost_inr': 'Optimized_Cost_INR',
        'savings_inr': 'Savings_INR',
    }).to_csv(path, index_label='Timestamp')
    return path


def _cbc_solver():
    path = os.environ.get('CBC_PATH') or shutil.which('cbc')
    if not path:
        for prefix in ('/opt/homebrew', '/usr/local'):
            candidate = Path(prefix) / 'opt/cbc/bin/cbc'
            if candidate.is_file():
                path = str(candidate)
                break
    solver = pulp.COIN_CMD(path=path, msg=False) if path else pulp.PULP_CBC_CMD(msg=False)
    if not solver.available():
        raise RuntimeError('CBC is unavailable; install CBC or set CBC_PATH')
    return solver


class SolarnexBESSOptimizer:
    """Find the least-cost battery trades for one price window."""

    def __init__(self, capacity_mwh=config.CAPACITY_MWH,
                 max_power_mw=config.MAX_POWER_MW, rte=config.RTE):
        for name, value in [('capacity_mwh', capacity_mwh), ('max_power_mw', max_power_mw)]:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if not math.isfinite(rte) or not 0 < rte <= 1:
            raise ValueError('rte must be in (0, 1]')
        self.capacity = capacity_mwh
        self.max_power = max_power_mw
        self.rte = rte
        self.eff_factor = math.sqrt(rte)

    def optimize_window(self, df_window, initial_soc=None, terminal_soc=None):
        """Optimize hourly rows; SoC is end-of-hour MWh, power is grid-side MW.

        Export is allowed at the same market price as import. Optional terminal
        SoC prevents valuing depletion of initial stored energy as arbitrage.
        """
        if df_window.empty or not np.isfinite(df_window[['price', 'load']].to_numpy(dtype=float)).all():
            raise ValueError('Window must contain finite price and load values')
        if initial_soc is None:
            initial_soc = self.capacity * config.INITIAL_SOC_FRACTION
        for name, value in [('initial_soc', initial_soc), ('terminal_soc', terminal_soc)]:
            if value is not None and (not math.isfinite(value) or not 0 <= value <= self.capacity):
                raise ValueError(f'{name} must be within battery capacity')
        count = len(df_window)
        times = range(count)
        prices = df_window['price'].to_numpy()
        model = pulp.LpProblem('Solarnex_BESS_Dispatch', pulp.LpMinimize)
        charge = pulp.LpVariable.dicts('charge', times, 0, self.max_power)
        discharge = pulp.LpVariable.dicts('discharge', times, 0, self.max_power)
        soc = pulp.LpVariable.dicts('soc', range(count + 1), 0, self.capacity)
        charging = pulp.LpVariable.dicts('charging', times, cat='Binary')
        # The battery cannot change the base load, so only its trades affect the optimum.
        model += pulp.lpSum((charge[t] - discharge[t]) * prices[t] for t in times)
        model += soc[0] == initial_soc
        for t in times:
            model += soc[t + 1] == soc[t] + charge[t] * self.eff_factor - discharge[t] / self.eff_factor
            model += charge[t] <= self.max_power * charging[t]
            model += discharge[t] <= self.max_power * (1 - charging[t])
        if terminal_soc is not None:
            model += soc[count] == terminal_soc
        try:
            model.solve(_cbc_solver())
        except (OSError, pulp.PulpSolverError) as exc:
            raise RuntimeError('Cannot run CBC; set CBC_PATH to a compatible executable') from exc
        status = pulp.LpStatus[model.status]
        if status != 'Optimal':
            raise RuntimeError(f'Solarnex optimization failed: {status}')
        results = df_window.copy()
        results['p_charge'] = [charge[t].varValue for t in times]
        results['p_discharge'] = [discharge[t].varValue for t in times]
        results['soc'] = [soc[t + 1].varValue for t in times]
        results.attrs['status'] = status
        return results


class SolarnexRollingSimulator:
    """Commit dispatch in steps, carrying stored energy into the next window."""

    def __init__(self, full_df, optimizer, horizon_hours=config.HORIZON_HOURS,
                 step_hours=config.STEP_HOURS, spike_filter=True,
                 threshold_std=config.SPIKE_THRESHOLD_STD):
        valid_hours = all(
            isinstance(hours, int) and not isinstance(hours, bool) and hours > 0
            for hours in (horizon_hours, step_hours)
        )
        if not valid_hours or step_hours > horizon_hours:
            raise ValueError('Require positive integer step_hours <= horizon_hours')
        if not math.isfinite(threshold_std) or threshold_std <= 0:
            raise ValueError('threshold_std must be finite and positive')
        if full_df.empty:
            raise ValueError('Simulation requires at least one hourly record')
        self.full_df = full_df.copy()
        self.optimizer = optimizer
        self.horizon = horizon_hours
        self.step = step_hours
        self.spike_filter = spike_filter
        self.threshold_std = threshold_std

    def apply_spike_filter(self, window_df, threshold_std=None):
        """Clip price spikes against a five-hour local median and standard deviation."""
        threshold = self.threshold_std if threshold_std is None else threshold_std
        median = window_df['price'].rolling(5, center=True, min_periods=1).median()
        std = window_df['price'].rolling(5, center=True, min_periods=1).std().fillna(1)
        result = window_df.copy()
        result['price'] = result['price'].clip(median - threshold * std, median + threshold * std)
        return result

    def run_simulation(self):
        """Use observed future prices as a backtest, not a live forecast.

        Every window targets half-full terminal SoC, including the final partial
        window. Commit only the forward step and carry its actual ending SoC.
        """
        chunks = []
        target_soc = config.INITIAL_SOC_FRACTION * self.optimizer.capacity
        current_soc = target_soc
        for start in range(0, len(self.full_df), self.step):
            window = self.full_df.iloc[start:start + self.horizon]
            processed = self.apply_spike_filter(window) if self.spike_filter else window
            result = self.optimizer.optimize_window(processed, current_soc, target_soc)
            # Dispatch only the next step; the remaining hours are lookahead.
            committed = result.iloc[:self.step].copy()
            committed['optimization_price'] = committed['price'].to_numpy()
            # Settle trades at actual prices, even when the spike filter is on.
            committed['price'] = window['price'].iloc[:len(committed)].to_numpy()
            current_soc = float(committed['soc'].iloc[-1])
            # CBC rounds its output; keep boundary values inside physical bounds.
            current_soc = min(self.optimizer.capacity, max(0.0, current_soc))
            chunks.append(committed)
        results = pd.concat(chunks)
        results['net_grid_mw'] = results['load'] + results['p_charge'] - results['p_discharge']
        results['baseline_cost_inr'] = results['load'] * results['price']
        results['savings_inr'] = (results['p_discharge'] - results['p_charge']) * results['price']
        results['optimized_cost_inr'] = results['baseline_cost_inr'] - results['savings_inr']
        results.attrs['status'] = 'Optimal'
        return results


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError('Date must use YYYY-MM-DD') from None


def verify_results(results, optimizer):
    """Check continuity across rolling boundaries and actual-price cash flows."""
    charge = results.p_charge.to_numpy()
    discharge = results.p_discharge.to_numpy()
    soc = results.soc.to_numpy()
    target = optimizer.capacity * config.INITIAL_SOC_FRACTION
    previous = np.r_[target, soc[:-1]]
    if not np.isfinite(results.to_numpy(dtype=float)).all():
        raise ValueError('Simulation contains non-finite results')
    if not np.allclose(soc, previous + charge * optimizer.eff_factor - discharge / optimizer.eff_factor,
                       rtol=0, atol=2e-6):
        raise ValueError('Simulation violates stored-energy continuity')
    if min(charge.min(), discharge.min(), soc.min()) < -1e-6:
        raise ValueError('Simulation contains negative battery power or stored energy')
    if max(charge.max(), discharge.max()) > optimizer.max_power + 1e-6 or soc.max() > optimizer.capacity + 1e-6:
        raise ValueError('Simulation exceeds battery capacity or power')
    if (np.minimum(charge, discharge) > 1e-6).any():
        raise ValueError('Simulation charges and discharges simultaneously')
    if not np.isclose(soc[-1], target, rtol=0, atol=1e-6):
        raise ValueError('Final SoC does not equal initial SoC')
    expected_savings = (discharge - charge) * results.price.to_numpy()
    if not np.allclose(results.savings_inr, expected_savings, rtol=0, atol=1e-6):
        raise ValueError('Savings must be settled against actual market prices')
    if not np.allclose(results.net_grid_mw, results.load + charge - discharge, rtol=0, atol=1e-6):
        raise ValueError('Net-grid accounting mismatch')
    if not np.allclose(results.optimized_cost_inr,
                       results.baseline_cost_inr - results.savings_inr, rtol=0, atol=1e-6):
        raise ValueError('Cost accounting mismatch')


def export_summaries(results, optimizer, args):
    """Save daily costs and the settings needed to interpret this run."""
    daily = results.groupby(results.index.date).agg(
        charged_mwh=('p_charge', 'sum'),
        discharged_mwh=('p_discharge', 'sum'),
        baseline_cost_inr=('baseline_cost_inr', 'sum'),
        optimized_cost_inr=('optimized_cost_inr', 'sum'),
        gross_savings_inr=('savings_inr', 'sum'))
    daily.index.name = 'date_IST'
    daily.to_csv(args.output_dir / 'daily_summary.csv')
    summary = {
        'status': results.attrs['status'],
        'currency': 'INR',
        'timezone': config.TIMEZONE,
        'start': str(results.index[0]),
        'last_hour_start': str(results.index[-1]),
        'hours': len(results),
        'start_date': str(args.start_date) if args.start_date else (str(args.date) if args.date else None),
        'end_date': str(args.end_date) if args.end_date else (str(args.date) if args.date else None),
        'capacity_mwh': optimizer.capacity,
        'max_power_mw': optimizer.max_power,
        'round_trip_efficiency': optimizer.rte,
        'horizon_hours': args.horizon_hours,
        'step_hours': args.step_hours,
        'spike_filter_enabled': not args.no_spike_filter,
        'spike_threshold_std': config.SPIKE_THRESHOLD_STD,
        'filtered_committed_hours': int((np.abs(results.price - results.optimization_price) > 1e-8).sum()),
        'initial_soc_mwh': optimizer.capacity * config.INITIAL_SOC_FRACTION,
        'final_soc_mwh': float(results.soc.iloc[-1]),
        'charged_mwh': float(results.p_charge.sum()),
        'discharged_mwh': float(results.p_discharge.sum()),
        'battery_charge_cost_inr': float((results.p_charge * results.price).sum()),
        'battery_discharge_revenue_inr': float((results.p_discharge * results.price).sum()),
        'gross_arbitrage_savings_inr': float(results.savings_inr.sum()),
        'national_load_baseline_cost_inr': float(results.baseline_cost_inr.sum()),
        'national_load_optimized_cost_inr': float(results.optimized_cost_inr.sum()),
        'price_source': results.attrs.get('price_source', ['unspecified_custom_dataset']),
        'dataset': str(args.dataset.resolve()),
        'backtest': 'Rolling horizon using observed future prices; not a live forecast',
        'load_context': 'All-India national demand, not site consumption; battery does not enforce national network constraints',
        'excluded_costs': [
            'degradation', 'capital cost', 'exchange/trading fees',
            'transmission/wheeling charges', 'taxes/duties',
        ],
    }
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=config.DATASET_PATH)
    parser.add_argument('--date', type=parse_date, help='Select one IST date; default: full dataset')
    parser.add_argument('--start-date', type=parse_date, help='Select start date (IST) YYYY-MM-DD for date range')
    parser.add_argument('--end-date', type=parse_date, help='Select end date (IST) YYYY-MM-DD for date range')
    parser.add_argument('--output-dir', type=Path, default=config.OUTPUT_DIR)
    parser.add_argument('--capacity-mwh', type=float, default=config.CAPACITY_MWH)
    parser.add_argument('--max-power-mw', type=float, default=config.MAX_POWER_MW)
    parser.add_argument('--rte', type=float, default=config.RTE)
    parser.add_argument('--horizon-hours', type=int, default=config.HORIZON_HOURS)
    parser.add_argument('--step-hours', type=int, default=config.STEP_HOURS)
    parser.add_argument('--no-spike-filter', action='store_true')
    parser.add_argument('--no-show', action='store_true', help='Save charts without opening windows')
    parser.add_argument(
        '--animate', action=argparse.BooleanOptionalAction, default=True,
        help='Save animated GIF of the dispatch (default: True; use --no-animate to disable)',
    )
    parser.add_argument('--animation-hours', type=int, default=None, help='Hours shown in animation; default: all simulated hours')
    parser.add_argument('--gif-duration', type=float, default=config.GIF_DURATION_SECONDS, help='Total GIF animation duration in seconds; default 40.0')
    parser.add_argument('--gif-frame-duration', type=float, default=None, help='Duration of each frame in seconds (optional override)')
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse_args(argv)
    if args.date and (args.start_date or args.end_date):
        parser.error('Cannot specify both --date and --start-date/--end-date')
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error(f'--start-date {args.start_date} cannot be after --end-date {args.end_date}')
    if args.animation_hours is not None and args.animation_hours <= 0:
        parser.error('--animation-hours must be positive')
    if args.gif_duration <= 0:
        parser.error('--gif-duration must be positive')
    if args.gif_frame_duration is not None and args.gif_frame_duration <= 0:
        parser.error('--gif-frame-duration must be positive')
    os.environ.setdefault('MPLCONFIGDIR', str(config.BASE_DIR / '.matplotlib'))
    if args.no_show:
        import matplotlib
        matplotlib.use('Agg')
    if __package__:
        from .plots import plot_prices_curve, plot_dispatch, animate_dispatch
    else:
        from plots import plot_prices_curve, plot_dispatch, animate_dispatch

    try:
        data = load_market_data(
            args.dataset,
            selected_date=args.date,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        optimizer = SolarnexBESSOptimizer(args.capacity_mwh, args.max_power_mw, args.rte)
        simulator = SolarnexRollingSimulator(
            data, optimizer, args.horizon_hours, args.step_hours,
            spike_filter=not args.no_spike_filter,
        )
        print(f'Using {len(data):,} hourly Indian grid/IEX records from {args.dataset}', flush=True)
        results = simulator.run_simulation()
        results.attrs['price_source'] = data.attrs.get('price_source', ['unspecified_custom_dataset'])
        verify_results(results, optimizer)
        if not results.index.equals(data.index):
            raise ValueError('Rolling simulation did not commit each input hour exactly once')
        path = export_simulation_to_csv(results, args.output_dir / 'solarnex_bess_simulation_output.csv')
        summary = export_summaries(results, optimizer, args)
        plot_prices_curve(results, output_dir=args.output_dir, show=not args.no_show)
        plot_dispatch(results, capacity_mwh=optimizer.capacity, output_dir=args.output_dir, show=not args.no_show)
        gif_path = None
        if args.animate:
            anim_hours = args.animation_hours
            if anim_hours is None:
                if args.date or args.start_date or args.end_date:
                    anim_hours = len(results)
                else:
                    anim_hours = min(len(results), config.ANIMATION_HOURS)
            gif_path = animate_dispatch(
                results,
                capacity_mwh=optimizer.capacity,
                output_dir=args.output_dir,
                hours=anim_hours,
                duration_seconds=args.gif_duration,
                frame_duration_seconds=args.gif_frame_duration,
            )
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))
    print(f'Optimization status: {summary["status"]}; processed hours: {len(results):,}')
    print(f'Gross arbitrage savings at actual prices: INR {summary["gross_arbitrage_savings_inr"]:,.2f}')
    print(f'Results: {path}')
    if gif_path:
        print(f'Animated GIF: {gif_path}')
    print(f'Charts and summary: {args.output_dir}')


if __name__ == '__main__':
    main()
