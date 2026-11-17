"""Plots for forecast comparison and forecast-driven BESS dispatch."""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def plot_forecast_comparison(frame, output_path, *, show=True):
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(frame["timestamp"], frame["forecast_price_inr_mwh"], marker="o", ms=3,
            color="#1769aa", label="Forecast price")
    actual_mask = frame["actual_price_inr_mwh"].notna()
    if actual_mask.any():
        ax.plot(frame.loc[actual_mask, "timestamp"], frame.loc[actual_mask, "actual_price_inr_mwh"],
                marker="o", ms=3, color="#e76f51", label="Actual price")
    ax.set_title("Actual and Forecast Electricity Prices")
    ax.set_xlabel("Hour")
    ax.set_ylabel("Price (INR/MWh)")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend()
    fig.autofmt_xdate()
    _save(fig, output_path, show)


def plot_dispatch(results, output_path, *, capacity_mwh, show=True):
    fig, (power_ax, soc_ax) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    hours = np.arange(len(results))
    power_ax.bar(hours, results["Discharge (MW)"], color="#2a9d8f", label="Discharge")
    power_ax.bar(hours, -results["Charge (MW)"], color="#e76f51", label="Charge")
    price_ax = power_ax.twinx()
    price_ax.plot(hours, results["forecast_price_inr_mwh"], color="#222", linestyle="--",
                  label="Forecast price")
    power_ax.set_ylabel("Dispatch (MW)")
    price_ax.set_ylabel("Forecast price (INR/MWh)")
    power_ax.set_title(f"Forecast Driven BESS Dispatch — {results['timestamp'].iloc[0].date()}")
    power_ax.legend(loc="upper left")
    price_ax.legend(loc="upper right")
    power_ax.grid(True, linestyle="--", alpha=0.35)
    soc_ax.plot(hours, results["SoC (MWh)"], marker="o", color="#1769aa", label="SoC")
    soc_ax.axhline(capacity_mwh, color="gray", linestyle=":", label="Capacity")
    soc_ax.set_xlabel("Hour of day")
    soc_ax.set_ylabel("Stored energy (MWh)")
    soc_ax.set_xticks(range(24))
    soc_ax.grid(True, linestyle="--", alpha=0.35)
    soc_ax.legend()
    fig.tight_layout()
    _save(fig, output_path, show)


def _save(fig, output_path, show):
    try:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=180, bbox_inches="tight")
        if show:
            plt.show()
    finally:
        plt.close(fig)
