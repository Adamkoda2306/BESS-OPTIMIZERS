"""Paths, column names, and cleaning defaults for the hourly dataset."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RAW_DATA = BASE_DIR.parent / 'September 2023 - Sheet1.csv'
DATASET = BASE_DIR / 'datasets' / 'grid_india_hourly.csv'
TIMEZONE = 'Asia/Kolkata'

IEX_MARKET_URL = 'https://www.iexindia.com/market-data/day-ahead-market/market-snapshot'
MAX_FETCH_DAYS = 31
REQUEST_TIMEOUT_SECONDS = 45

# Reject hours below the minimum; flag marginal coverage for later review.
MIN_COVERAGE = 0.85
LOW_COVERAGE_THRESHOLD = 0.95
DEMAND_REVIEW_MULTIPLIER = 1.5
POWER_UNIT = 'MW'

SCADA_CHANNELS = {
    'SCADA/ANALOG/044MQ068/0': 'thermal_mw',
    'SCADA/ANALOG/044MQ062/0': 'hydro_mw',
    'SCADA/ANALOG/044MQ063/0': 'gas_mw',
    'SCADA/ANALOG/044MQ064/0': 'nuclear_mw',
    'SCADA/ANALOG/044MQ070/0': 'wind_mw',
    'SCADA/ANALOG/044MQ206/0': 'pv_mw',
    'SCADA/ANALOG/044MQ067/0': 'consumption_mw',
}
COLUMN_ALIASES = {
    'timestamp': ['Time', 'timestamp', 'datetime', 'date_time', 'timestamp_local_IN'],
    'thermal_mw': ['thermal_mw', 'TOTAL_THM_ONLY|P', 'thermal'],
    'hydro_mw': ['hydro_mw', 'TOTAL_NLDC_HYD|P', 'hydro'],
    'gas_mw': ['gas_mw', 'TOTAL_NLDC_GAS|P', 'gas'],
    'nuclear_mw': ['nuclear_mw', 'TOTAL_NLDC_NPC|P', 'nuclear'],
    'wind_mw': ['wind_mw', 'ALL_INDIA_WIND|P', 'wind', 'wind_onshore_mw'],
    'pv_mw': ['pv_mw', 'ALL_IND_SOLAR|P', 'solar', 'solar_mw'],
    'consumption_mw': ['consumption_mw', 'NLDC_DEMAND|P', 'demand_mw', 'load_mw', 'demand', 'load'],
    'price_inr_mwh': ['price_inr_mwh', 'Market_Price_INR_MWh', 'MCP (Rs/MWh)', 'price_rs_mwh'],
    'price_inr_kwh': ['price_inr_kwh', 'price_rs_kwh', 'tariff_inr_kwh'],
}
# Prefer the SCADA channel when a CSV also contains a descriptive duplicate.
for channel, field in SCADA_CHANNELS.items():
    COLUMN_ALIASES[field].insert(0, channel)
REQUIRED_POWER_COLUMNS = ('wind_mw', 'pv_mw', 'gas_mw', 'consumption_mw')
