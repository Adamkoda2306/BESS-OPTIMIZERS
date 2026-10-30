"""Input paths and default battery settings for this project."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATASET = BASE_DIR.parent / 'datasets' / 'converted' / 'new_hourly.csv'
DATASET_PATH = DATASET
OUTPUT_DIR = BASE_DIR.parent / 'output' / 'solarnex_bess_engine'
TIMEZONE = 'Asia/Kolkata'

# Capacity is in MWh, power is in MW, and efficiency includes both directions.
CAPACITY_MWH = 10.0
MAX_POWER_MW = 2.5
RTE = 0.90
INITIAL_SOC_FRACTION = 0.5

# Look ahead two days, then commit one day before solving again.
HORIZON_HOURS = 48
STEP_HOURS = 24
SPIKE_THRESHOLD_STD = 2.5
ANIMATION_HOURS = 120
GIF_DURATION_SECONDS = 40.0

