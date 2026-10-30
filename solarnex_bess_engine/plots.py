"""Price charts, battery dispatch plots, and an optional dispatch animation."""

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
import numpy as np
import pandas as pd

if __package__:
    from . import config
else:
    import config

# Matplotlib otherwise renders timezone-aware timestamps using UTC by default.
plt.rcParams['timezone'] = config.TIMEZONE


def _dispatch_figure(results, capacity_mwh):
    """Create a dispatch figure matching the Grid BESS visual style."""
    fig, (power_ax, soc_ax) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    discharge = results['p_discharge'].to_numpy()
    charge = results['p_charge'].to_numpy()
    prices = results['price'].to_numpy()
    soc = results['soc'].to_numpy()

    # Charging appears below zero; discharging appears above it (matching Grid BESS style)
    width = pd.Timedelta(hours=0.8)
    bars_discharge = power_ax.bar(
        results.index, discharge, width=width,
        color='#2ca02c', alpha=0.8, label='Discharge (Selling)',
    )
    bars_charge = power_ax.bar(
        results.index, -charge, width=width,
        color='#d62728', alpha=0.8, label='Charge (Buying)',
    )
    power_ax.set_ylabel('Power Dispatch (MW)', fontsize=11, fontweight='bold')
    power_ax.legend(loc='upper left')
    power_ax.grid(True, linestyle='--', alpha=0.5)

    price_ax = power_ax.twinx()
    price_line, = price_ax.plot(
        results.index, prices, color='black',
        linestyle='--', linewidth=1.5, label='Price (INR/MWh)',
    )
    price_ax.set_ylabel('Market Price (INR/MWh)', fontsize=11)
    price_ax.legend(loc='upper right')
    power_ax.set_title(
        'Solarnex BESS: Optimal Arbitrage Dispatch & Market Prices',
        fontsize=14, fontweight='bold', pad=15,
    )

    marker = 'o' if len(results) <= 48 else None
    soc_line, = soc_ax.plot(
        results.index, soc, color='#1f77b4', marker=marker,
        linewidth=2.0, label='State of Energy (SoC)',
    )
    soc_ax.axhline(capacity_mwh, color='grey', linestyle=':', label='Max Capacity')
    soc_ax.set_ylabel('Energy Storage (MWh)', fontsize=11, fontweight='bold')
    soc_ax.set_xlabel('Local Time (India, IST)', fontsize=11, fontweight='bold')
    soc_ax.set_ylim(0, capacity_mwh * 1.1)
    soc_ax.legend(loc='upper left')
    soc_ax.grid(True, linestyle='--', alpha=0.5)

    fig.autofmt_xdate()
    fig.tight_layout()
    return fig, (bars_discharge, bars_charge, price_line, soc_line)


def plot_prices_curve(results, *, output_dir=config.OUTPUT_DIR, show=True):
    path = Path(output_dir) / 'market_prices_chart.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 4))
    try:
        marker = 'o' if len(results) <= 48 else None
        ax.plot(results.index, results['price'], color='black', linewidth=1.8, marker=marker)
        ax.set_title('India IEX Day-Ahead Electricity Prices (IST)', fontsize=12, fontweight='bold')
        ax.set_xlabel('Local Time (India, IST)', fontsize=11, fontweight='bold')
        ax.set_ylabel('Price (INR/MWh)', fontsize=11, fontweight='bold')
        ax.grid(True, linestyle='--', alpha=0.6)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(path, dpi=300)
        if show:
            plt.show()
    finally:
        plt.close(fig)
    return path


def plot_dispatch(results, *, capacity_mwh=config.CAPACITY_MWH,
                  output_dir=config.OUTPUT_DIR, show=True):
    path = Path(output_dir) / 'bess_dispatch_chart.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, _ = _dispatch_figure(results, capacity_mwh)
    try:
        fig.savefig(path, dpi=300)
        if show:
            plt.show()
    finally:
        plt.close(fig)
    return path


def animate_dispatch(
    results, *, capacity_mwh=config.CAPACITY_MWH,
    output_dir=config.OUTPUT_DIR, hours=None,
    duration_seconds=config.GIF_DURATION_SECONDS, frame_duration_seconds=None,
):
    if results.empty:
        raise ValueError('Animation requires nonempty results')
    if hours is not None and hours > 0:
        data = results.iloc[:hours]
    else:
        data = results

    path = Path(output_dir) / 'solarnex_bess_simulation.gif'
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, (power_ax, soc_ax) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    price_ax = power_ax.twinx()

    discharge = data['p_discharge'].to_numpy()
    charge = data['p_charge'].to_numpy()
    prices = data['price'].to_numpy()
    soc = data['soc'].to_numpy()
    times = data.index

    # Fixed scales across the entire animation
    max_p = max(float(discharge.max()), float(charge.max()), 1.0) * 1.15
    power_ax.set_ylim(-max_p, max_p)
    power_ax.set_ylabel('Power Dispatch (MW)', fontsize=11, fontweight='bold')
    power_ax.grid(True, linestyle='--', alpha=0.5)

    p_min, p_max = float(prices.min()), float(prices.max())
    p_span = max(p_max - p_min, 100.0)
    price_ax.set_ylim(p_min - 0.08 * p_span, p_max + 0.08 * p_span)
    price_ax.set_ylabel('Market Price (INR/MWh)', fontsize=11)

    soc_ax.set_ylim(0, capacity_mwh * 1.15)
    soc_ax.set_ylabel('Energy Storage (MWh)', fontsize=11, fontweight='bold')
    soc_ax.set_xlabel('Local Time (India, IST)', fontsize=11, fontweight='bold')
    soc_ax.axhline(capacity_mwh, color='grey', linestyle=':', label='Max Capacity')
    soc_ax.grid(True, linestyle='--', alpha=0.5)

    width = pd.Timedelta(hours=0.8)
    bars_discharge = power_ax.bar(
        times, np.zeros(len(times)), width=width,
        color='#2ca02c', alpha=0.8, label='Discharge (Selling)',
    )
    bars_charge = power_ax.bar(
        times, np.zeros(len(times)), width=width,
        color='#d62728', alpha=0.8, label='Charge (Buying)',
    )
    price_line, = price_ax.plot([], [], color='black', linestyle='--', linewidth=1.5, label='Price (INR/MWh)')

    marker = 'o' if len(data) <= 48 else None
    soc_line, = soc_ax.plot([], [], color='#1f77b4', marker=marker, linewidth=2.0, label='State of Energy (SoC)')

    power_ax.legend(loc='upper left')
    price_ax.legend(loc='upper right')
    soc_ax.legend(loc='upper left')
    power_ax.set_title(
        'Solarnex BESS: Optimal Arbitrage Dispatch & Market Prices',
        fontsize=14, fontweight='bold', pad=15,
    )

    fig.autofmt_xdate()
    fig.tight_layout()

    total_hours = len(data)
    # Frame selection
    if total_hours <= 120:
        frames = list(range(1, total_hours + 1))
    else:
        step = max(1, total_hours // 100)
        frames = sorted(set([*range(1, total_hours + 1, step), total_hours]))

    # Frame timing
    if frame_duration_seconds is not None and frame_duration_seconds > 0:
        fps = 1.0 / frame_duration_seconds
        interval_ms = int(frame_duration_seconds * 1000)
    else:
        duration = max(1.0, float(duration_seconds))
        fps = max(0.1, len(frames) / duration)
        interval_ms = int(1000.0 / fps)

    try:
        def update(count):
            for i in range(count):
                bars_discharge[i].set_height(discharge[i])
                bars_charge[i].set_height(-charge[i])
            price_line.set_data(times[:count], prices[:count])
            soc_line.set_data(times[:count], soc[:count])
            return [*bars_discharge, *bars_charge, price_line, soc_line]

        anim = FuncAnimation(fig, update, frames=frames, interval=interval_ms, blit=False)
        anim.save(path, writer=PillowWriter(fps=fps), dpi=100)
    finally:
        plt.close(fig)
    return path
