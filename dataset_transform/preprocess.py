"""Clean SCADA measurements, average them by hour, and attach observed IEX prices."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from . import config


def normalize(value):
    """Ignore punctuation and case when comparing column names."""
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def parse_times(
    values, input_timezone=config.TIMEZONE, timestamp_format=None, day_first=False,
):
    explicit_offsets = values.astype(str).str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$', regex=True)
    if explicit_offsets.any() and not explicit_offsets.all():
        raise ValueError('Do not mix unzoned timestamps and timestamps with explicit offsets')
    times = pd.to_datetime(
        values, format=timestamp_format or 'mixed', dayfirst=day_first,
        errors='raise', utc=bool(explicit_offsets.all()),
    )
    index = pd.DatetimeIndex(times)
    if index.isna().any():
        raise ValueError('Missing timestamps are not allowed')
    if index.tz is None:
        index = index.tz_localize(input_timezone, ambiguous='raise', nonexistent='raise')
    return index.tz_convert(config.TIMEZONE).rename('timestamp_local_IN')


def read_source(
    path, column_map=None, input_timezone=config.TIMEZONE,
    timestamp_format=None, day_first=False, *, allow_missing=False,
):
    """Recognize channel IDs, descriptive headers, or explicitly mapped columns."""
    frame = pd.read_csv(path, low_memory=False)
    mapping = {}
    overrides = column_map or {}
    unknown = set(overrides) - set(config.COLUMN_ALIASES)
    if unknown:
        raise ValueError(f'Unknown canonical column-map keys: {sorted(unknown)}')
    for field, aliases in config.COLUMN_ALIASES.items():
        if field in overrides:
            column = overrides[field]
            if column not in frame:
                raise ValueError(f'Mapped column {column!r} for {field} was not found')
            mapping[field] = column
        else:
            # Alias order keeps duplicate display columns from replacing channel data.
            for alias in aliases:
                matches = [column for column in frame if normalize(column) == normalize(alias)]
                if matches:
                    mapping[field] = matches[0]
                    break
    if 'timestamp' not in mapping:
        raise ValueError('Cannot detect timestamp column; provide --column-map JSON')
    time_column = mapping['timestamp']
    second_header = False
    if not frame.empty and pd.isna(frame[time_column].iloc[0]):
        # SCADA exports can have a second header row containing display names.
        label_values = {normalize(v) for v in frame.iloc[0].dropna()}
        known = {normalize(v) for values in config.COLUMN_ALIASES.values() for v in values}
        if len(label_values & known) >= 3:
            frame = frame.iloc[1:].copy()
            second_header = True
    if frame.empty:
        raise ValueError('CSV contains no data records')
    frame.index = parse_times(frame[time_column], input_timezone, timestamp_format, day_first)
    if frame.index.has_duplicates:
        raise ValueError('Duplicate timestamps must be resolved before preprocessing')
    frame = frame.sort_index()
    result = pd.DataFrame(index=frame.index)
    for field, column in mapping.items():
        if field != 'timestamp':
            result[field] = pd.to_numeric(frame[column], errors='raise').astype(float)
    if allow_missing:
        result = result.replace([np.inf, -np.inf], np.nan)
    elif not np.isfinite(result.to_numpy()).all():
        raise ValueError('Source contains missing or non-finite numeric measurements')
    return result, {'column_mapping': mapping, 'descriptive_header_skipped': second_header}


def infer_cadence(index):
    """Find the regular sample interval and check that it divides an hour."""
    if len(index) < 2:
        raise ValueError('At least two samples are required to infer sampling frequency')
    differences = np.diff(index.asi8)
    cadence_ns = int(pd.Series(differences).mode().iloc[0])
    hour_ns = pd.Timedelta(hours=1).value
    if cadence_ns <= 0 or cadence_ns > hour_ns or hour_ns % cadence_ns:
        raise ValueError('Sampling interval must be regular, no longer than one hour and divide an hour exactly')
    if (differences % cadence_ns).any():
        raise ValueError('Irregular sampling: provide measurements on a regular sampling grid')
    # Samples mark interval starts; an offset sampling grid would bias hourly means.
    if ((index.asi8 - index.floor('h').asi8) % cadence_ns).any():
        raise ValueError('Samples must align with hour boundaries at the detected interval')
    return pd.Timedelta(cadence_ns, unit='ns')


def impute_power_readings(raw, cadence, top_values=20, seed=42):
    """Fill invalid power readings from each month's most frequent valid values."""
    if not isinstance(top_values, int) or not 10 <= top_values <= 20:
        raise ValueError('imputation_top_values must be an integer from 10 to 20')
    if not isinstance(seed, int) or seed < 0:
        raise ValueError('imputation_seed must be a nonnegative integer')

    index = pd.date_range(raw.index[0], raw.index[-1], freq=cadence, name=raw.index.name)
    filled = raw.reindex(index).copy()
    invalid = filled.isna() | filled.lt(0)
    dropout = filled.clip(lower=0).eq(0).all(axis=1)
    invalid.loc[dropout, :] = True
    months = filled.index.tz_localize(None).to_period('M')
    rng = np.random.default_rng(seed)
    candidates = {}

    for month in months.unique():
        month_rows = months == month
        candidates[str(month)] = {}
        for column in filled:
            replace = month_rows & invalid[column].to_numpy()
            if not replace.any():
                continue
            observed = filled.loc[month_rows & ~invalid[column].to_numpy(), column]
            counts = observed.value_counts().rename_axis('value').reset_index(name='count')
            # Break frequency ties by value so the same seed gives the same result.
            counts = counts.sort_values(['count', 'value'], ascending=[False, True])
            pool = counts['value'].head(top_values).to_numpy()
            if not len(pool):
                raise ValueError(f'No valid {column} readings available for imputation in {month}')
            filled.loc[replace, column] = rng.choice(pool, size=int(replace.sum()), replace=True)
            candidates[str(month)][column] = pool.tolist()

    return filled, invalid, {
        'enabled': True,
        'method': 'Uniform random sampling from each column and month\'s most frequent valid values',
        'top_values': top_values,
        'seed': seed,
        'candidate_values_mw': candidates,
        'frequency_tie_break': 'Ascending numeric value',
        'fewer_than_requested_candidates': 'Use all available distinct valid values',
    }


def aggregate_scada(
    raw_path, *, column_map=None, input_timezone=config.TIMEZONE,
    timestamp_format=None, day_first=False, power_unit=config.POWER_UNIT,
    min_coverage=config.MIN_COVERAGE, drop_partial_hours=False,
    impute_missing=False, imputation_top_values=20, imputation_seed=42,
):
    """Average usable measurements into IST hours and record cleaning counts."""
    if power_unit not in ('MW', 'kW'):
        raise ValueError('power_unit must be MW or kW')
    if not np.isfinite(min_coverage) or not 0 < min_coverage <= 1:
        raise ValueError('min_coverage must be in (0, 1]')
    data, details = read_source(
        raw_path, column_map, input_timezone, timestamp_format, day_first,
        allow_missing=impute_missing,
    )
    missing = set(config.REQUIRED_POWER_COLUMNS) - set(data)
    if missing:
        raise ValueError(f'Missing required power columns {sorted(missing)}; use --column-map JSON')
    cadence = infer_cadence(data.index)
    expected_count = int(pd.Timedelta(hours=1) / cadence)
    power_columns = [column for column in data if column.endswith('_mw')]
    raw = data[power_columns].copy()
    if power_unit == 'kW':
        raw /= 1000
    negative_solar = raw.pv_mw < 0
    imputation = {'enabled': False}
    if impute_missing:
        # Preserve the original coverage; estimated samples are counted separately.
        dropout = raw.clip(lower=0).eq(0).all(axis=1)
        cleaned, estimated, imputation = impute_power_readings(
            raw, cadence, imputation_top_values, imputation_seed,
        )
        valid = (~estimated.any(axis=1)).resample('h').sum()
    else:
        raw['pv_mw'] = raw.pv_mw.clip(lower=0)
        if (raw < 0).any().any():
            raise ValueError('Negative non-solar power measurements need manual review')
        dropout = raw.eq(0).all(axis=1)
        cleaned = raw.mask(dropout, axis=0)
        estimated = pd.DataFrame(False, index=raw.index, columns=raw.columns)
        valid = (~dropout).resample('h').sum()
    hourly = cleaned.resample('h').mean()
    hourly['sample_count'] = raw.resample('h').size()
    hourly['valid_sample_count'] = valid
    hourly['scada_coverage_fraction'] = valid / expected_count
    hourly['low_scada_coverage_flag'] = valid / expected_count < config.LOW_COVERAGE_THRESHOLD
    hourly['dropout_sample_count'] = dropout.resample('h').sum()
    hourly['solar_clipped_sample_count'] = 0 if impute_missing else negative_solar.resample('h').sum()
    hourly['imputed_sample_count'] = estimated.any(axis=1).resample('h').sum()
    hourly['estimated_data_flag'] = hourly.imputed_sample_count > 0
    for column in power_columns:
        hourly[f'{column}_imputed_sample_count'] = estimated[column].resample('h').sum()
    generation_columns = ['thermal_mw', 'hydro_mw', 'gas_mw', 'nuclear_mw', 'wind_mw', 'pv_mw']
    review_enabled = set(generation_columns).issubset(raw.columns)
    suspect = pd.Series(False, index=raw.index)
    if review_enabled:
        generation = raw[generation_columns].sum(axis=1)
        suspect = (~dropout) & (raw.consumption_mw > generation * config.DEMAND_REVIEW_MULTIPLIER)
    hourly['demand_review_sample_count'] = suspect.resample('h').sum()
    partial = pd.Series(False, index=hourly.index)
    if raw.index[0] != raw.index[0].floor('h'):
        partial.iloc[0] = True
    if raw.index[-1] + cadence != raw.index[-1].floor('h') + pd.Timedelta(hours=1):
        partial.iloc[-1] = True
    dropped = [str(t) for t in hourly.index[partial]]
    if partial.any():
        if not drop_partial_hours:
            raise ValueError('Input starts or ends inside an hour; use --drop-partial-hours to exclude boundary hours')
        hourly = hourly.loc[~partial]
    if hourly.empty:
        raise ValueError('No complete hourly intervals remain')
    insufficient_coverage = hourly.scada_coverage_fraction < min_coverage
    if (insufficient_coverage.any() and not impute_missing) or hourly.isna().any().any():
        examples = [str(t) for t in hourly.index[insufficient_coverage][:3]]
        raise ValueError(
            f'Insufficient usable coverage (minimum {min_coverage:.0%}) '
            f'in hours {examples}; no interpolation performed'
        )
    hourly['renewable_mw'] = hourly.wind_mw + hourly.pv_mw
    hourly['residual_load_mw'] = hourly.consumption_mw - hourly.renewable_mw
    quality = {
        **details,
        'raw_rows': len(raw),
        'hourly_rows': len(hourly),
        'sampling_interval_seconds': cadence.total_seconds(),
        'expected_samples_per_hour': expected_count,
        'start': str(hourly.index[0]),
        'last_hour_start': str(hourly.index[-1]),
        'input_timezone_for_unzoned_timestamps': input_timezone,
        'input_power_unit': power_unit,
        'minimum_required_coverage': min_coverage,
        'coverage_threshold_enforced': not impute_missing,
        'dropped_partial_boundary_hours': dropped,
        'simultaneous_zero_dropout_samples_excluded': int(dropout.sum()),
        'negative_solar_samples_clipped': 0 if impute_missing else int(negative_solar.sum()),
        'demand_samples_flagged_for_review_not_removed': int(suspect.sum()),
        'demand_review_enabled': review_enabled,
        'minimum_valid_hourly_coverage_fraction': float(hourly.scada_coverage_fraction.min()),
        'imputation': {
            **imputation,
            'estimated_hours': int(hourly.estimated_data_flag.sum()),
            'estimated_samples': int(hourly.imputed_sample_count.sum()),
            'estimated_samples_by_column': {
                column: int(hourly[f'{column}_imputed_sample_count'].sum())
                for column in power_columns
            },
        },
    }
    return hourly, quality


def load_prices(records, required_hours=None):
    """Validate official IEX hour 1..24 records without assuming month or price cap."""
    prices = pd.DataFrame(records)
    if prices.empty:
        raise ValueError('Price source returned no records')
    hours = pd.to_numeric(prices['hour'], errors='raise')
    if not hours.between(1, 24).all() or not (hours == np.floor(hours)).all():
        raise ValueError('IEX hour must be an integer from 1 through 24')
    times = pd.to_datetime(prices['date'], format='%d-%m-%Y', errors='raise') + pd.to_timedelta(hours - 1, unit='h')
    prices.index = pd.DatetimeIndex(times).tz_localize(config.TIMEZONE).rename('timestamp_local_IN')
    prices = prices.sort_index()
    values = pd.to_numeric(prices.mcp, errors='raise').astype(float)
    if prices.index.has_duplicates or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError('IEX prices must be finite, nonnegative and have unique hourly timestamps')
    result = pd.DataFrame({'price_inr_mwh': values, 'price_inr_kwh': values / 1000})
    if required_hours is not None:
        missing = required_hours.difference(result.index)
        if len(missing):
            raise ValueError(f'Missing observed prices for {len(missing)} requested hours, beginning {missing[0]}')
        result = result.reindex(required_hours)
    return result


def hourly_csv_prices(
    path, required_hours, *, column_map=None, input_timezone=config.TIMEZONE,
    timestamp_format=None, day_first=False,
):
    """Convert a supplied price CSV to complete hourly INR/MWh values."""
    data, _ = read_source(path, column_map, input_timezone, timestamp_format, day_first)
    if 'price_inr_mwh' not in data and 'price_inr_kwh' not in data:
        raise ValueError('No price_inr_mwh or price_inr_kwh column detected')
    if 'price_inr_mwh' in data and 'price_inr_kwh' in data:
        if not np.allclose(data.price_inr_mwh, data.price_inr_kwh * 1000, rtol=1e-6, atol=1e-5):
            raise ValueError('INR/kWh and INR/MWh prices disagree')
    values = data.price_inr_mwh if 'price_inr_mwh' in data else data.price_inr_kwh * 1000
    if (values < 0).any():
        raise ValueError('Indian prices must be nonnegative')
    if len(data) == 1 and data.index[0] == data.index[0].floor('h'):
        cadence = pd.Timedelta(hours=1)
    else:
        cadence = infer_cadence(data.index)
    count = values.resample('h').count()
    prices = values.resample('h').mean().reindex(required_hours)
    if prices.isna().any() or not count.reindex(required_hours).eq(int(pd.Timedelta(hours=1) / cadence)).all():
        raise ValueError('Supplied prices must cover every requested hour with complete regular samples')
    return pd.DataFrame({'price_inr_mwh': prices, 'price_inr_kwh': prices / 1000})


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def market_url(start, end):
    return config.IEX_MARKET_URL + '?' + urlencode({
        'interval': 'ONE_HOUR',
        'dp': 'SELECT_RANGE',
        'fromDate': start.strftime('%d-%m-%Y'),
        'toDate': end.strftime('%d-%m-%Y'),
        'showGraph': 'false',
    })


def extract_records(html):
    """Read the hourly records embedded in the IEX page response."""
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html):
        block = json.loads(match.group(1))
        if len(block) > 1 and isinstance(block[1], str) and '"reportData":' in block[1]:
            # The site embeds reportData inside a Next.js script payload.
            payload = block[1].split('"reportData":', 1)[1]
            records = json.JSONDecoder().raw_decode(payload)[0]['records']
            load_prices(records)
            return records
    raise ValueError('IEX page no longer contains recognizable hourly reportData')


def download_records(start, end):
    """Fetch up to 31 delivery days and keep the validated response in memory."""
    start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    if end < start or (end - start).days >= config.MAX_FETCH_DAYS:
        raise ValueError('Each IEX request must cover 1 to 31 delivery days')
    url = market_url(start, end)
    expected = pd.date_range(
        str(start), str(end) + ' 23:00', freq='h',
        tz=config.TIMEZONE, name='timestamp_local_IN',
    )
    # Some macOS Python installs need the system certificate bundle explicitly.
    context = ssl.create_default_context()
    if context.cert_store_stats()['x509_ca'] == 0:
        for ca_file in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
            if Path(ca_file).is_file():
                context.load_verify_locations(cafile=ca_file)
                break
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urlopen(request, timeout=config.REQUEST_TIMEOUT_SECONDS, context=context) as response:
            html = response.read().decode('utf-8')
        records = extract_records(html)
        prices = load_prices(records)
        if not prices.index.equals(expected):
            raise ValueError('IEX returned a different date range or incomplete hours')
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f'Cannot obtain observed IEX prices for {start} through {end}: {exc}. '
            'Provide --prices with matching observed INR prices or retry online.'
        ) from exc
    return prices, {
        'url': url,
        'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
        'start': str(start),
        'end': str(end),
        'hours': len(prices),
    }


def prices_for_hours(hours):
    """Fetch the requested delivery dates from IEX and keep prices in memory."""
    if hours.empty:
        raise ValueError('At least one hourly timestamp is required to fetch prices')

    # Split delivery dates into consecutive requests within IEX's date-range limit.
    groups = []
    for day in sorted(set(hours.date)):
        if (
            not groups
            or (day - groups[-1][-1]).days != 1
            or len(groups[-1]) == config.MAX_FETCH_DAYS
        ):
            groups.append([])
        groups[-1].append(day)

    frames = []
    requests = []
    for group in groups:
        print(f'Fetching IEX hourly DAM prices: {group[0]} through {group[-1]}', flush=True)
        frame, details = download_records(group[0], group[-1])
        frames.append(frame)
        requests.append(details)

    selected = pd.concat(frames).sort_index().reindex(hours)
    if selected.isna().any().any():
        raise ValueError('Observed prices are missing from the IEX response')
    return selected, {
        'price_requests': requests,
        'price_publisher': 'Indian Energy Exchange Limited',
        'price_market': 'Conventional day-ahead market, unconstrained hourly MCP',
    }


def build_dataset(
    raw_path=config.RAW_DATA, price_path=None, output_path=config.DATASET,
    **options,
):
    """Build the hourly CSV using supplied prices, embedded prices, or live IEX data."""
    raw_path = Path(raw_path)
    output_path = Path(output_path)
    input_paths = [raw_path]
    if price_path is not None:
        input_paths.append(Path(price_path))
    if any(output_path.resolve() == path.resolve() for path in input_paths):
        raise ValueError('Hourly output must not overwrite an input file')
    hourly, quality = aggregate_scada(raw_path, **options)
    source_options = {
        name: value for name, value in options.items()
        if name in ('column_map', 'input_timezone', 'timestamp_format', 'day_first')
    }
    # Use prices in the raw CSV when no separate price file was supplied.
    source, _ = read_source(
        raw_path, **source_options, allow_missing=options.get('impute_missing', False),
    )
    price_details = {}
    if price_path is not None:
        price_path = Path(price_path)
        if price_path.suffix.lower() == '.json':
            prices = load_prices(json.loads(price_path.read_text()), hourly.index)
        else:
            # A raw-data column map does not apply to the separate price CSV.
            external_options = {k: v for k, v in source_options.items() if k != 'column_map'}
            prices = hourly_csv_prices(price_path, hourly.index, **external_options)
        kind = 'user_supplied_price_file'
        price_details = {'prices_file': str(price_path.resolve()), 'prices_sha256': sha256(price_path)}
    elif 'price_inr_mwh' in source or 'price_inr_kwh' in source:
        prices = hourly_csv_prices(raw_path, hourly.index, **source_options)
        kind = 'input_CSV_observed_prices'
    else:
        prices, price_details = prices_for_hours(hourly.index)
        kind = 'IEX_DAM_observed_hourly_MCP'
    dataset = hourly.join(prices, how='left', validate='one_to_one')
    if dataset.isna().any().any():
        raise ValueError('Missing measurements or prices after joining')
    core = ['wind_mw', 'pv_mw', 'gas_mw', 'consumption_mw', 'price_inr_mwh']
    extra_columns = [column for column in dataset if column not in core]
    dataset = dataset[core + extra_columns]
    dataset['price_source'] = kind
    quality.update({
        **price_details,
        'raw_file': str(raw_path.resolve()),
        'raw_sha256': sha256(raw_path),
        'price_kind': kind,
        'synthetic_prices_used': False,
        'currency': 'INR',
        'timezone': config.TIMEZONE,
        'hourly_price_mean_inr_kwh': float(prices.price_inr_kwh.mean()),
        'hourly_price_min_inr_kwh': float(prices.price_inr_kwh.min()),
        'hourly_price_max_inr_kwh': float(prices.price_inr_kwh.max()),
        'aggregation': (
            'Arithmetic mean after random imputation of invalid regular interval power samples in each IST hour'
            if options.get('impute_missing', False)
            else 'Arithmetic mean over usable regular interval samples in each left-closed IST hour'
        ),
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(output_path, float_format='%.8f')
    output_path.with_suffix('.metadata.json').write_text(json.dumps(quality, indent=2) + '\n')
    return dataset, quality
