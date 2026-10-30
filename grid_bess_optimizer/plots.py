"""Price and dispatch charts for the selected day."""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

if __package__:
    from . import config
else:
    import config


def plot_prices_curve(hours, prices, *, output_dir=config.OUTPUT_DIR, show=True):
    """Save the selected day's market prices."""
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(hours, prices, marker='o', color='black', linewidth=2)
    ax.set_title('India IEX Day-Ahead Electricity Prices (IST)', fontsize=12, fontweight='bold')
    ax.set_xlabel('Hour of selected day (IST)')
    ax.set_ylabel('Price (INR/MWh)')
    ax.grid(True, linestyle='--', alpha=0.6)
    fig.tight_layout()
    _save_figure(fig, output_dir, 'market_prices_chart.png', show)


def plot_dispatch(
    hours, discharge, charge, prices, soc, *,
    capacity_mwh=config.CAPACITY_MWH, output_dir=config.OUTPUT_DIR, show=True,
):
    """Show charge, discharge, and stored energy alongside market prices."""
    fig, (power_ax, soc_ax) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    # Charging appears below zero; discharging appears above it.
    power_ax.bar(hours, discharge, color='#2ca02c', alpha=0.8, label='Discharge (Selling)')
    power_ax.bar(hours, -np.asarray(charge), color='#d62728', alpha=0.8, label='Charge (Buying)')
    power_ax.set_ylabel('Power Dispatch (MW)', fontsize=11, fontweight='bold')
    power_ax.legend(loc='upper left')
    power_ax.grid(True, linestyle='--', alpha=0.5)

    price_ax = power_ax.twinx()
    price_ax.plot(hours, prices, color='black', linestyle='--', linewidth=1.5, label='Price (INR/MWh)')
    price_ax.set_ylabel('Market Price (INR/MWh)', fontsize=11)
    price_ax.legend(loc='upper right')
    power_ax.set_title(
        'India BESS Optimal Arbitrage Dispatch & Market Prices',
        fontsize=14, fontweight='bold', pad=15,
    )

    soc_ax.plot(hours, soc, color='#1f77b4', marker='o', linewidth=2.5, label='State of Energy (SoC)')
    soc_ax.axhline(capacity_mwh, color='grey', linestyle=':', label='Max Capacity')
    soc_ax.set_ylabel('Energy Storage (MWh)', fontsize=11, fontweight='bold')
    soc_ax.set_xlabel('Dispatch Hour (SoC at end of hour)', fontsize=11, fontweight='bold')
    soc_ax.legend(loc='upper left')
    soc_ax.grid(True, linestyle='--', alpha=0.5)

    fig.tight_layout()
    _save_figure(fig, output_dir, 'bess_dispatch_chart.png', show)


def _save_figure(fig, output_dir, filename, show):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        fig.savefig(Path(output_dir) / filename, dpi=300)
        if show:
            plt.show()
    finally:
        plt.close(fig)
