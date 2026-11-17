"""SolarNex price-source, dispatch, and accounting charts."""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def plot_market_prices(frame, output_path, *, show=True):
    fig, ax = plt.subplots(figsize=(13, 5))
    source = frame["price_source"]
    for label, color in (("IEX_observed", "#222222"), ("forecast", "#1769aa")):
        mask = source.eq(label)
        if mask.any():
            ax.plot(frame.index[mask], frame.loc[mask, "price"], marker="." if len(frame) < 72 else None,
                    linewidth=1.7, color=color, label="IEX observed" if label == "IEX_observed" else "Forecast fallback")
    ax.set_title("IEX Observed and Forecast Fallback Prices")
    ax.set_xlabel("Local time (IST)")
    ax.set_ylabel("Price (INR/MWh)")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend()
    fig.autofmt_xdate()
    _save(fig, output_path, show)


def plot_dispatch(results, capacity_mwh, output_path, *, show=True):
    fig, (power_ax, soc_ax) = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    power_ax.bar(results.index, results["p_discharge"], width=0.03 if len(results) > 48 else 0.025,
                 color="#2a9d8f", label="Discharge")
    power_ax.bar(results.index, -results["p_charge"], width=0.03 if len(results) > 48 else 0.025,
                 color="#e76f51", label="Charge")
    price_ax = power_ax.twinx()
    price_ax.plot(results.index, results["price"], color="#222", linestyle="--", linewidth=1.4, label="Settled price")
    power_ax.set_ylabel("Battery dispatch (MW)")
    price_ax.set_ylabel("Price (INR/MWh)")
    power_ax.set_title("SolarNex Rolling BESS Dispatch")
    power_ax.legend(loc="upper left")
    price_ax.legend(loc="upper right")
    power_ax.grid(True, linestyle="--", alpha=0.35)
    soc_ax.plot(results.index, results["soc"], color="#1769aa", linewidth=1.8, label="State of charge")
    soc_ax.axhline(capacity_mwh, color="gray", linestyle=":", label="Capacity")
    soc_ax.set_ylabel("Stored energy (MWh)")
    soc_ax.set_xlabel("Local time (IST)")
    soc_ax.grid(True, linestyle="--", alpha=0.35)
    soc_ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    _save(fig, output_path, show)


def _save(fig, output_path, show):
    try:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=200, bbox_inches="tight")
        if show:
            plt.show()
    finally:
        plt.close(fig)
