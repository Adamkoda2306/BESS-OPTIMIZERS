"""Forecast aware input normalization and battery dispatch."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

from . import config

def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...], label: str) -> str | None:
    by_lower = {str(column).strip().lower(): column for column in frame.columns}
    for candidate in candidates:
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    if label == "forecast":
        for column in frame.columns:
            name = str(column).lower()
            if ("forecast" in name or "predict" in name) and "price" in name and "kwh" not in name:
                return column
    return None


def load_forecast_dataset(
    path: str | Path,
    *,
    forecast_column: str | None = None,
    actual_column: str | None = None,
    timezone: str = config.TIMEZONE,
) -> pd.DataFrame:
    """Load a CSV and normalize it to timestamp, forecast price, and optional actual price."""
    path = Path(path)
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError(f"Dataset is empty: {path}")

    forecast_column = forecast_column or _find_column(frame, config.FORECAST_COLUMNS, "forecast")
    if forecast_column is None or forecast_column not in frame.columns:
        raise ValueError(
            "Cannot find forecast prices. Provide --forecast-column or a column such as "
            "forecast_price_inr_mwh, predicted_price, or price_inr_mwh."
        )
    actual_column = actual_column or _find_column(frame, config.ACTUAL_COLUMNS, "actual")
    if actual_column is not None and actual_column not in frame.columns:
        raise ValueError(f"Actual-price column {actual_column!r} is not in the dataset")

    timestamp_col = _find_column(frame, config.TIMESTAMP_COLUMNS, "timestamp")
    if timestamp_col:
        timestamps = pd.to_datetime(frame[timestamp_col], errors="coerce")
    else:
        date_col = _find_column(frame, config.DATE_COLUMNS, "date")
        if date_col is None or "hour" not in {str(c).lower() for c in frame.columns}:
            raise ValueError("Dataset needs a timestamp column or date/target_date plus an hour column")
        hour_col = next(c for c in frame.columns if str(c).lower() == "hour")
        hour_values = frame[hour_col]
        numeric_hours = pd.to_numeric(hour_values, errors="coerce")
        if numeric_hours.notna().all():
            hour_offsets = pd.to_timedelta(numeric_hours, unit="h")
        else:
            # Forecast exports commonly encode the hour as "00:00", "01:00", etc.
            clock_values = hour_values.astype(str).str.strip().str.replace(
                r"^(\d{1,2}:\d{2})$", r"\1:00", regex=True)
            hour_offsets = pd.to_timedelta(clock_values, errors="coerce")
        timestamps = pd.to_datetime(frame[date_col], errors="coerce") + hour_offsets

    # Forecasting files may contain naive IST timestamps; treat these as local market time.
    if timestamps.isna().any():
        raise ValueError("Dataset contains invalid timestamps or hours")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
    else:
        timestamps = timestamps.dt.tz_convert(timezone)

    forecasts = pd.to_numeric(frame[forecast_column], errors="coerce")
    if actual_column is not None:
        actuals = pd.to_numeric(frame[actual_column], errors="coerce")
        if "kwh" in str(actual_column).lower() and "mwh" not in str(actual_column).lower():
            actuals = actuals * 1000.0
    else:
        actuals = pd.Series(np.nan, index=frame.index, dtype=float)
    if "kwh" in str(forecast_column).lower() and "mwh" not in str(forecast_column).lower():
        forecasts = forecasts * 1000.0

    result = pd.DataFrame({
        "timestamp": timestamps,
        "forecast_price_inr_mwh": forecasts,
        "actual_price_inr_mwh": actuals,
    }).sort_values("timestamp").reset_index(drop=True)
    if result["timestamp"].duplicated().any():
        raise ValueError("Dataset timestamps must be unique")
    if not np.isfinite(result["forecast_price_inr_mwh"].to_numpy(dtype=float)).all():
        raise ValueError("Forecast prices must all be finite numeric values")
    observed_actuals = result["actual_price_inr_mwh"].dropna().to_numpy(dtype=float)
    if not np.isfinite(observed_actuals).all():
        raise ValueError("Actual prices, when supplied, must be finite numeric values")
    if not result["timestamp"].eq(result["timestamp"].dt.floor("h")).all():
        raise ValueError("Forecast timestamps must mark the start of each hour")
    return result


def optimize_forecast_day(
    day: pd.DataFrame,
    *,
    capacity_mwh: float = config.CAPACITY_MWH,
    max_power_mw: float = config.MAX_POWER_MW,
    efficiency: float = config.EFFICIENCY,
) -> tuple[pd.DataFrame, str]:
    """Optimize one full 24-hour day using forecast prices."""
    # Reuse the established MILP and CBC handling; import only when dispatch is run.
    project_dir = str(config.PROJECT_DIR)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    from grid_bess_optimizer.model import optimize_bess_arbitrage

    day = day.sort_values("timestamp").reset_index(drop=True)
    if len(day) != 24:
        day_label = str(day["timestamp"].iloc[0].date()) if len(day) else "unknown"
        raise ValueError(f"Date {day_label} must contain exactly 24 hourly rows; got {len(day)}")
    if not day["timestamp"].diff().iloc[1:].eq(pd.Timedelta(hours=1)).all():
        raise ValueError("Each forecast day must contain consecutive hourly timestamps")
    dispatch, status = optimize_bess_arbitrage(
        day["forecast_price_inr_mwh"].to_numpy(dtype=float),
        capacity_mwh=capacity_mwh,
        max_power_mw=max_power_mw,
        efficiency=efficiency,
    )
    result = day.copy()
    result["Hour"] = np.arange(24)
    result["Price (INR/MWh)"] = result["forecast_price_inr_mwh"].to_numpy()
    for column in ("Charge (MW)", "Discharge (MW)", "SoC (MWh)"):
        result[column] = dispatch[column].to_numpy()
    prices = result["forecast_price_inr_mwh"].to_numpy()
    result["Charge cost (INR)"] = result["Charge (MW)"].to_numpy() * prices
    result["Discharge revenue (INR)"] = result["Discharge (MW)"].to_numpy() * prices
    result["Gross arbitrage (INR)"] = result["Discharge revenue (INR)"] - result["Charge cost (INR)"]
    result["Cumulative gross arbitrage (INR)"] = result["Gross arbitrage (INR)"].cumsum()
    return result, status


def forecast_accuracy(frame: pd.DataFrame) -> dict | None:
    """Return standard error metrics when actual prices are present."""
    mask = frame["actual_price_inr_mwh"].notna()
    if not mask.any():
        return None
    actual = frame.loc[mask, "actual_price_inr_mwh"].to_numpy(dtype=float)
    predicted = frame.loc[mask, "forecast_price_inr_mwh"].to_numpy(dtype=float)
    error = predicted - actual
    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error ** 2)))
    denom = float(np.sum(np.abs(actual)))
    return {
        "hours_with_actuals": int(mask.sum()),
        "mae_inr_mwh": mae,
        "rmse_inr_mwh": rmse,
        "wape_pct": float(np.sum(np.abs(error)) / denom * 100.0) if denom else None,
    }
