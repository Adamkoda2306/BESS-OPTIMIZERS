"""Fetch IEX prices, fall back to forecasting, and run SolarNex BESS dispatch.

Example:
    python forecasting_solarnex_bess.py --start-date 2026-11-14 --end-date 2026-11-16 --no-show
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

import pandas as pd

# Keep imports stable when this runner is launched from the forecasting folder.
PROJECT_DIR = next(
    (parent for parent in Path(__file__).resolve().parents
     if (parent / "solarnex_bess_engine").is_dir()
     and (parent / "forecasting_model").is_dir()),
    Path(__file__).resolve().parents[1],
)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from forecasting.solarnex_bess import config
from forecasting.solarnex_bess.model import build_simulation_input, run_solarnex_simulation
from forecasting.solarnex_bess.plots import plot_dispatch, plot_market_prices


def _parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=_parse_date, required=True,
                        help="Inclusive start delivery date in IST (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=_parse_date, required=True,
                        help="Inclusive end delivery date in IST (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    parser.add_argument("--capacity-mwh", type=float, default=config.CAPACITY_MWH)
    parser.add_argument("--max-power-mw", type=float, default=config.MAX_POWER_MW)
    parser.add_argument("--rte", type=float, default=config.RTE)
    parser.add_argument("--horizon-hours", type=int, default=config.HORIZON_HOURS)
    parser.add_argument("--step-hours", type=int, default=config.STEP_HOURS)
    parser.add_argument("--no-spike-filter", action="store_true")
    parser.add_argument("--no-forecast-generation", action="store_true",
                        help="Use saved forecast CSVs only; do not run forecasting_model/predict.py")
    parser.add_argument("--no-show", action="store_true", help="Save plots without opening windows")
    return parser, parser.parse_args(argv)


def run(argv=None):
    parser, args = parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("--start-date cannot be after --end-date")
    if args.horizon_hours <= 0 or args.step_hours <= 0 or args.step_hours > args.horizon_hours:
        parser.error("Require positive --step-hours <= --horizon-hours")
    if args.capacity_mwh <= 0 or args.max_power_mw <= 0 or not 0 < args.rte <= 1:
        parser.error("Capacity and power must be positive; --rte must be in (0, 1]")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(args.output_dir / ".matplotlib"))
    if args.no_show:
        import matplotlib
        matplotlib.use("Agg")

    try:
        data, provenance = build_simulation_input(
            args.start_date,
            args.end_date,
            output_dir=args.output_dir,
            use_forecaster=not args.no_forecast_generation,
        )
        print(
            f"Collected {len(data)} hourly prices: "
            f"{provenance['price_source_hours'].get('IEX_observed', 0)} IEX and "
            f"{provenance['price_source_hours'].get('forecast', 0)} forecast hours",
            flush=True,
        )
        results, optimizer = run_solarnex_simulation(
            data,
            capacity_mwh=args.capacity_mwh,
            max_power_mw=args.max_power_mw,
            rte=args.rte,
            horizon_hours=args.horizon_hours,
            step_hours=args.step_hours,
            spike_filter=not args.no_spike_filter,
        )
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))

    price_sources = provenance["price_source_by_hour"]
    load_sources = provenance["load_source_by_hour"]
    exported = results.copy()
    exported["price_source"] = price_sources
    exported["load_source"] = load_sources
    exported = exported.rename(columns={
        "price": "Market_Price_INR_MWh",
        "optimization_price": "Optimization_Price_INR_MWh",
        "load": "Grid_Load_MW",
        "p_charge": "Battery_Charge_MW",
        "p_discharge": "Battery_Discharge_MW",
        "soc": "State_Of_Charge_MWh",
        "net_grid_mw": "Net_Grid_MW",
        "baseline_cost_inr": "Baseline_Cost_INR",
        "optimized_cost_inr": "Optimized_Cost_INR",
        "savings_inr": "Savings_INR",
    })
    result_path = args.output_dir / "solarnex_bess_results.csv"
    exported.to_csv(result_path, index_label="Timestamp")

    daily = results.groupby(results.index.date).agg(
        hours=("price", "size"),
        charged_mwh=("p_charge", "sum"),
        discharged_mwh=("p_discharge", "sum"),
        baseline_cost_inr=("baseline_cost_inr", "sum"),
        optimized_cost_inr=("optimized_cost_inr", "sum"),
        savings_inr=("savings_inr", "sum"),
    )
    daily.index.name = "date_IST"
    daily.to_csv(args.output_dir / "daily_summary.csv")

    summary = {
        "status": results.attrs.get("status", "Optimal"),
        "start_date_ist": args.start_date.isoformat(),
        "end_date_ist": args.end_date.isoformat(),
        "timezone": config.TIMEZONE,
        "currency": "INR",
        "hours": len(results),
        "capacity_mwh": optimizer.capacity,
        "max_power_mw": optimizer.max_power,
        "round_trip_efficiency": optimizer.rte,
        "horizon_hours": args.horizon_hours,
        "step_hours": args.step_hours,
        "spike_filter_enabled": not args.no_spike_filter,
        "initial_soc_mwh": optimizer.capacity * config.INITIAL_SOC_FRACTION,
        "final_soc_mwh": float(results["soc"].iloc[-1]),
        "charged_mwh": float(results["p_charge"].sum()),
        "discharged_mwh": float(results["p_discharge"].sum()),
        "battery_charge_cost_inr": float((results["p_charge"] * results["price"]).sum()),
        "battery_discharge_revenue_inr": float((results["p_discharge"] * results["price"]).sum()),
        "gross_arbitrage_savings_inr": float(results["savings_inr"].sum()),
        "baseline_grid_cost_inr": float(results["baseline_cost_inr"].sum()),
        "optimized_grid_cost_inr": float(results["optimized_cost_inr"].sum()),
        "data_provenance": {
            "price_source_hours": provenance["price_source_hours"],
            "load_source_hours": provenance["load_source_hours"],
            "iex_range_error": provenance["iex_range_error"],
        },
        "model_note": "SolarNex rolling-horizon dispatch; actual IEX prices are used when available, otherwise forecast prices",
        "load_note": "Historical exact or same-hour median national grid load is context for cost accounting, not site load",
        "excluded_costs": ["battery degradation", "capital cost", "exchange/trading fees", "transmission/wheeling charges", "taxes/duties"],
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    plot_input = data.copy()
    plot_input["price_source"] = price_sources
    plot_market_prices(plot_input, args.output_dir / "market_prices_sources.png", show=not args.no_show)
    plot_dispatch(results, optimizer.capacity, args.output_dir / "bess_dispatch.png", show=not args.no_show)

    print(f"Optimization status: {summary['status']}; processed {len(results)} hours")
    print(f"SolarNex arbitrage savings: INR {summary['gross_arbitrage_savings_inr']:,.2f}")
    print(f"Results and summary saved to: {args.output_dir}")
    return summary


if __name__ == "__main__":
    run()
