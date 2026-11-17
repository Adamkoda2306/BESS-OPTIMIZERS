"""IEX collection, forecast fallback, load context, and SolarNex dispatch."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

from . import config

# The package can be nested under forecasting/ while its engine and data
# collector remain at the repository root.
project_dir = str(config.PROJECT_DIR)
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)


def _localize_index(index, timezone=config.TIMEZONE):
    index = pd.DatetimeIndex(index)
    if index.tz is None:
        index = index.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
    return index.tz_convert(timezone)


def load_historical_load(local_data_dir=config.LOCAL_DATA_DIR) -> pd.DataFrame:
    """Load prepared historical timestamps and grid consumption from local CSVs."""
    pieces = []
    for path in sorted(Path(local_data_dir).glob("*.csv")):
        try:
            frame = pd.read_csv(path, usecols=lambda c: c in {"timestamp_local_IN", "consumption_mw"})
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        if not {"timestamp_local_IN", "consumption_mw"}.issubset(frame.columns):
            continue
        timestamps = pd.to_datetime(frame["timestamp_local_IN"], utc=True, errors="coerce")
        loads = pd.to_numeric(frame["consumption_mw"], errors="coerce")
        good = timestamps.notna() & np.isfinite(loads) & (loads >= 0)
        if good.any():
            pieces.append(pd.DataFrame({
                "load": loads.loc[good].to_numpy(dtype=float),
            }, index=timestamps.loc[good].dt.tz_convert(config.TIMEZONE)))
    if not pieces:
        raise ValueError(f"No prepared consumption_mw data found under {local_data_dir}")
    history = pd.concat(pieces).sort_index()
    history = history.loc[~history.index.duplicated(keep="last")]
    history.index.name = "Timestamp"
    return history


def load_context_for_hours(hours: pd.DatetimeIndex, history: pd.DataFrame) -> tuple[pd.Series, list[str]]:
    """Use exact local load where available and a historical hour profile otherwise."""
    hours = _localize_index(hours)
    exact = history["load"].reindex(hours)
    exact_mask = exact.notna()
    profile = history.groupby(history.index.hour)["load"].median()
    profile_values = np.array([profile.get(timestamp.hour, np.nan) for timestamp in hours], dtype=float)
    combined = exact.to_numpy(dtype=float)
    combined[~exact_mask.to_numpy()] = profile_values[~exact_mask.to_numpy()]
    if not np.isfinite(combined).all():
        raise ValueError("Could not supply load context for every requested hour")
    sources = ["historical_exact" if found else "historical_hour_median" for found in exact_mask]
    return pd.Series(combined, index=hours, name="load"), sources


def _forecast_file_for_day(day: date, *, run_forecaster=True, output_dir=config.OUTPUT_DIR) -> pd.DataFrame:
    """Read a saved forecast or invoke the repository forecasting CLI for one day."""
    forecast_path = config.FORECAST_DIR / f"forecast_{day.isoformat()}.csv"
    if not forecast_path.is_file() and run_forecaster:
        if not config.FORECAST_SCRIPT.is_file():
            raise FileNotFoundError(f"Forecast script not found: {config.FORECAST_SCRIPT}")
        generated_dir = Path(output_dir) / "forecasts"
        generated_dir.mkdir(parents=True, exist_ok=True)
        forecast_path = generated_dir / f"forecast_{day.isoformat()}.csv"
        plot_path = generated_dir / f"forecast_{day.isoformat()}.png"
        command = [
            sys.executable, str(config.FORECAST_SCRIPT), "--date", day.isoformat(),
            "--output", str(forecast_path), "--plot-output", str(plot_path),
        ]
        completed = subprocess.run(
            command, cwd=config.PROJECT_DIR, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Forecast generation failed for {day}: {detail[-1200:]}")
    if not forecast_path.is_file():
        raise FileNotFoundError(f"No saved forecast for {day}: {forecast_path}")

    # The forecasting package handles its supported timestamp/date/hour schemas and INR units.
    from forecasting.grid_bess.model import load_forecast_dataset
    forecast = load_forecast_dataset(forecast_path)
    forecast = forecast.loc[forecast["timestamp"].dt.date == day]
    if len(forecast) != 24:
        raise ValueError(f"Forecast for {day} must contain exactly 24 hourly prices (found {len(forecast)})")
    forecast = forecast.sort_values("timestamp").copy()
    return forecast


def collect_market_prices(
    start_date: date,
    end_date: date,
    *,
    output_dir=config.OUTPUT_DIR,
    use_forecaster=True,
) -> tuple[pd.DataFrame, dict]:
    """Fetch IEX prices for an inclusive range and fill unavailable days with forecasts."""
    if end_date < start_date:
        raise ValueError("End date cannot be before start date")
    expected_index = pd.date_range(
        str(start_date), str(end_date) + " 23:00", freq="h", tz=config.TIMEZONE, name="Timestamp")
    prices: dict[pd.Timestamp, float] = {}
    sources: dict[pd.Timestamp, str] = {}

    # Try one request for the complete range; retry per day if any date is unavailable.
    iex_frame = None
    try:
        from dataset_transform.preprocess import download_records
        iex_frame, _ = download_records(start_date, end_date)
    except Exception as range_error:
        range_error_text = str(range_error)
    else:
        iex_frame = iex_frame.copy()
        iex_frame.index = _localize_index(iex_frame.index)
        if iex_frame.index.equals(expected_index):
            for timestamp, value in iex_frame["price_inr_mwh"].items():
                prices[timestamp] = float(value)
                sources[timestamp] = "IEX_observed"

    if len(prices) != len(expected_index):
        # Preserve a partially useful range response, then fetch each unresolved day.
        if iex_frame is not None:
            for timestamp, value in iex_frame["price_inr_mwh"].items():
                timestamp = _localize_index([timestamp])[0]
                if timestamp in expected_index and np.isfinite(value):
                    prices[timestamp] = float(value)
                    sources[timestamp] = "IEX_observed"
        for day in (start_date + timedelta(days=n) for n in range((end_date - start_date).days + 1)):
            day_hours = expected_index[expected_index.date == day]
            missing = [timestamp for timestamp in day_hours if timestamp not in prices]
            if not missing:
                continue
            try:
                from dataset_transform.preprocess import download_records
                observed, _ = download_records(day, day)
                observed = observed.copy()
                observed.index = _localize_index(observed.index)
                if not observed.index.equals(day_hours):
                    raise ValueError(f"IEX response for {day} is incomplete")
                for timestamp, value in observed["price_inr_mwh"].items():
                    prices[timestamp] = float(value)
                    sources[timestamp] = "IEX_observed"
            except Exception:
                forecast = _forecast_file_for_day(day, run_forecaster=use_forecaster, output_dir=output_dir)
                forecast.index = _localize_index(forecast["timestamp"])
                for timestamp, value in forecast["forecast_price_inr_mwh"].items():
                    if timestamp not in prices:
                        prices[timestamp] = float(value)
                        sources[timestamp] = "forecast"

    result = pd.DataFrame(index=expected_index)
    result.index.name = "Timestamp"
    result["price"] = [prices.get(timestamp, np.nan) for timestamp in expected_index]
    result["price_source"] = [sources.get(timestamp, "missing") for timestamp in expected_index]
    if not np.isfinite(result["price"].to_numpy(dtype=float)).all():
        raise ValueError("IEX/forecast fallback did not provide a finite price for each requested hour")
    provenance = {
        "price_source_hours": result["price_source"].value_counts().to_dict(),
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "iex_range_error": range_error_text if "range_error_text" in locals() else None,
    }
    return result, provenance


def build_simulation_input(
    start_date: date,
    end_date: date,
    *,
    output_dir=config.OUTPUT_DIR,
    use_forecaster=True,
    local_data_dir=config.LOCAL_DATA_DIR,
) -> tuple[pd.DataFrame, dict]:
    """Combine market prices, provenance, and local load context for SolarNex."""
    market, provenance = collect_market_prices(
        start_date, end_date, output_dir=output_dir, use_forecaster=use_forecaster)
    history = load_historical_load(local_data_dir)
    loads, load_sources = load_context_for_hours(market.index, history)
    simulation = pd.DataFrame({"price": market["price"], "load": loads}, index=market.index)
    provenance["load_source_hours"] = pd.Series(load_sources).value_counts().to_dict()
    provenance["price_source_by_hour"] = market["price_source"].tolist()
    provenance["load_source_by_hour"] = load_sources
    return simulation, provenance


def run_solarnex_simulation(
    data: pd.DataFrame,
    *,
    capacity_mwh=config.CAPACITY_MWH,
    max_power_mw=config.MAX_POWER_MW,
    rte=config.RTE,
    horizon_hours=config.HORIZON_HOURS,
    step_hours=config.STEP_HOURS,
    spike_filter=True,
):
    """Run the existing SolarNex rolling optimizer over normalized input data."""
    from solarnex_bess_engine.model import SolarnexBESSOptimizer, SolarnexRollingSimulator, verify_results
    optimizer = SolarnexBESSOptimizer(capacity_mwh, max_power_mw, rte)
    simulator = SolarnexRollingSimulator(
        data, optimizer, horizon_hours, step_hours, spike_filter=spike_filter)
    results = simulator.run_simulation()
    verify_results(results, optimizer)
    if not results.index.equals(data.index):
        raise ValueError("Rolling simulation did not commit each input hour exactly once")
    return results, optimizer
