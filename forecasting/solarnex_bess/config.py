"""Configuration for date-range IEX/forecast driven SolarNex BESS runs."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _find_project_dir():
    """Locate the repository root even when this package lives under forecasting/."""
    for parent in (BASE_DIR, *BASE_DIR.parents):
        if ((parent / "forecasting_model" / "predict.py").is_file()
                and (parent / "dataset_transform" / "preprocess.py").is_file()):
            return parent
    # Expected layout: <project>/forecasting/solarnex_bess/config.py.
    return BASE_DIR.parents[1]


PROJECT_DIR = _find_project_dir()
OUTPUT_DIR = PROJECT_DIR / "output" / "solarnex_bess"
LOCAL_DATA_DIR = PROJECT_DIR / "datasets" / "converted"
FORECAST_DIR = PROJECT_DIR / "forecasting_model" / "saved_models" / "predicted"
FORECAST_SCRIPT = PROJECT_DIR / "forecasting_model" / "predict.py"
TIMEZONE = "Asia/Kolkata"

# Match the settings of solarnex_bess_engine/config.py.
CAPACITY_MWH = 10.0
MAX_POWER_MW = 2.5
RTE = 0.90
INITIAL_SOC_FRACTION = 0.5
HORIZON_HOURS = 48
STEP_HOURS = 24
SPIKE_THRESHOLD_STD = 2.5

FORECAST_COLUMN = "forecast_price_inr_mwh"
