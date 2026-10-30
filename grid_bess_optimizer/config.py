"""Input paths and default battery settings for this project."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATASET = BASE_DIR.parent / 'datasets' / 'converted' / 'new_hourly.csv'
DATASET_PATH = DATASET
OUTPUT_DIR = BASE_DIR.parent / 'output' / 'grid_bess_optimizer'
TIMEZONE = 'Asia/Kolkata'

# Capacity is in MWh, power is in MW, and efficiency includes both directions.
CAPACITY_MWH = 2.0
MAX_POWER_MW = 1.0
EFFICIENCY = 0.92
