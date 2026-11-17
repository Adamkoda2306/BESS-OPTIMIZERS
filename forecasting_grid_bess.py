"""Optimize a grid battery against one or more days of forecast prices.

Examples:
    python forecasting_grid_bess.py --dataset path/to/forecast.csv
    python forecasting_grid_bess.py --dataset path/to/forecast.csv --date 2026-11-17 --no-show

The input CSV needs hourly timestamps (or a target_date/forecast_date and hour pair)
and forecast prices. Actual prices are optional and are used for comparison metrics
and plots when present.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path

import pandas as pd

from forecasting.grid_bess import config
from forecasting.grid_bess.model import (
    forecast_accuracy,
    load_forecast_dataset,
    optimize_forecast_day,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=config.DEFAULT_DATASET,
                        help="Forecast CSV; defaults to forecasting_model/saved_models/predicted/forecast_2026-11-17.csv")
    parser.add_argument("--forecast-column", help="Forecast price column (auto-detected by default)")
    parser.add_argument("--actual-column", help="Actual price column (auto-detected by default)")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="Optimize only this delivery date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    parser.add_argument("--capacity-mwh", type=float, default=config.CAPACITY_MWH)
    parser.add_argument("--max-power-mw", type=float, default=config.MAX_POWER_MW)
    parser.add_argument("--efficiency", type=float, default=config.EFFICIENCY,
                        help="Battery round-trip efficiency, in (0, 1]")
    parser.add_argument("--no-show", action="store_true", help="Save plots without opening windows")
    return parser, parser.parse_args(argv)


def run(argv=None):
    parser, args = parse_args(argv)
    try:
        dataset = load_forecast_dataset(
            args.dataset,
            forecast_column=args.forecast_column,
            actual_column=args.actual_column,
        )
        if args.date:
            dataset = dataset.loc[dataset["timestamp"].dt.date == args.date].copy()
            if dataset.empty:
                raise ValueError(f"No rows found for date {args.date}")

        daily_results = []
        statuses = []
        for _, day in dataset.groupby(dataset["timestamp"].dt.date, sort=True):
            day_results, status = optimize_forecast_day(
                day,
                capacity_mwh=args.capacity_mwh,
                max_power_mw=args.max_power_mw,
                efficiency=args.efficiency,
            )
            daily_results.append(day_results)
            statuses.append(status)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))

    results = pd.concat(daily_results, ignore_index=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results["Price (INR/kWh)"] = results["forecast_price_inr_mwh"] / 1000.0
    results.to_csv(args.output_dir / "forecast_bess_results.csv", index=False)

    daily_summary = results.assign(date=results["timestamp"].dt.date).groupby("date").agg(
        hours=("timestamp", "size"),
        forecast_mean_price_inr_mwh=("forecast_price_inr_mwh", "mean"),
        charged_mwh=("Charge (MW)", "sum"),
        discharged_mwh=("Discharge (MW)", "sum"),
        charge_cost_inr=("Charge cost (INR)", "sum"),
        discharge_revenue_inr=("Discharge revenue (INR)", "sum"),
        gross_arbitrage_inr=("Gross arbitrage (INR)", "sum"),
    )
    daily_summary.to_csv(args.output_dir / "daily_summary.csv")

    accuracy = forecast_accuracy(dataset)
    summary = {
        "status": "Optimal" if all(s == "Optimal" for s in statuses) else statuses,
        "dataset": str(args.dataset.resolve()),
        "timezone": config.TIMEZONE,
        "currency": "INR",
        "forecast_price_column": args.forecast_column or "auto-detected",
        "actual_price_column": args.actual_column or ("auto-detected" if accuracy else None),
        "days_optimized": len(daily_results),
        "hours_optimized": len(results),
        "capacity_mwh": args.capacity_mwh,
        "max_power_mw": args.max_power_mw,
        "round_trip_efficiency": args.efficiency,
        "initial_soc_mwh_each_day": args.capacity_mwh / 2,
        "gross_arbitrage_inr": float(results["Gross arbitrage (INR)"].sum()),
        "charged_mwh": float(results["Charge (MW)"].sum()),
        "discharged_mwh": float(results["Discharge (MW)"].sum()),
        "forecast_accuracy_against_actual": accuracy,
        "dispatch_basis": "Forecast prices; actual prices are only used for evaluation and plotting",
        "excluded_costs": ["battery degradation", "capital cost", "market fees", "taxes and duties"],
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    os.environ.setdefault("MPLCONFIGDIR", str(args.output_dir / ".matplotlib"))
    if args.no_show:
        import matplotlib
        matplotlib.use("Agg")
    from forecasting.grid_bess.plots import plot_dispatch, plot_forecast_comparison

    show = not args.no_show
    plot_forecast_comparison(dataset, args.output_dir / "actual_vs_forecast.png", show=show)
    for day, day_results in results.groupby(results["timestamp"].dt.date, sort=True):
        plot_dispatch(
            day_results.reset_index(drop=True),
            args.output_dir / f"dispatch_{day}.png",
            capacity_mwh=args.capacity_mwh,
            show=show,
        )

    print(f"Optimization: {summary['status']} across {summary['days_optimized']} day(s)")
    print(f"Gross arbitrage based on forecast: INR {summary['gross_arbitrage_inr']:,.2f}")
    if accuracy:
        print(f"Forecast MAE against actual: {accuracy['mae_inr_mwh']:,.2f} INR/MWh")
        print(f"Forecast WAPE against actual: {accuracy['wape_pct']:.2f}%")
    print(f"Results, summary and charts saved to: {args.output_dir}")
    return summary


if __name__ == "__main__":
    run()
