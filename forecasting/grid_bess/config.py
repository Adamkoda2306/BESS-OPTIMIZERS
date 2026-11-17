"""Settings for battery optimization from hourly price forecasts."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parents[1]
FORECAST_DIR = PROJECT_DIR / "forecasting_model" / "saved_models" / "predicted"
DEFAULT_DATASET = FORECAST_DIR / "forecast_2026-11-17.csv"
OUTPUT_DIR = PROJECT_DIR / "output" / "forecasting_grid_bess"
TIMEZONE = "Asia/Kolkata"

# Inherit the physical defaults used by the existing grid BESS optimizer.
CAPACITY_MWH = 2.0
MAX_POWER_MW = 1.0
EFFICIENCY = 0.92

FORECAST_COLUMNS = (
    "forecast_price_inr_mwh",
    "predicted_price",
    "forecast_price",
    "forecasted_price",
    "price_forecast",
    "price_inr_mwh",
)
ACTUAL_COLUMNS = (
    "actual_price_inr_mwh",
    "actual_price",
    "actual_price_inr_kwh",
)
TIMESTAMP_COLUMNS = ("timestamp", "timestamp_local_IN", "datetime", "date_time")
DATE_COLUMNS = ("target_date", "forecast_date", "date")
