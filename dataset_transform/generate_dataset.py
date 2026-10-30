"""Prepare hourly data, or fetch prices alone with --fetch-prices --start/--end.

Run from the repository root:
    env/bin/python dataset_transform/generate_dataset.py --raw-data input.csv
    env/bin/python -m dataset_transform --raw-data input.csv --output hourly.csv
"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

import pandas as pd

if __package__:
    from . import config
    from .preprocess import build_dataset, prices_for_hours
else:
    # Direct script execution needs the repository root on the import path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dataset_transform import config
    from dataset_transform.preprocess import build_dataset, prices_for_hours


def add_preprocessing_arguments(parser):
    parser.add_argument(
        '--raw-data', type=Path,
        help='Input SCADA CSV to clean, average by hour, and attach prices',
    )
    parser.add_argument('--prices', type=Path, help='Optional observed-price CSV or IEX record JSON')
    parser.add_argument(
        '--column-map', type=Path,
        help='JSON mapping standard field names to input column names',
    )
    parser.add_argument(
        '--input-timezone', default=config.TIMEZONE,
        help='Timezone of input timestamps that have no UTC offset',
    )
    parser.add_argument('--timestamp-format', help='Optional explicit pandas timestamp format')
    parser.add_argument('--day-first', action='store_true', help='Interpret ambiguous dates as day/month/year')
    parser.add_argument('--power-unit', choices=['MW', 'kW'], default=config.POWER_UNIT)
    parser.add_argument('--min-coverage', type=float, default=config.MIN_COVERAGE)
    parser.add_argument('--drop-partial-hours', action='store_true', help='Drop incomplete first/last hours')
    parser.add_argument(
        '--impute-missing', action='store_true',
        help='Randomly fill invalid power readings from frequent valid values and flag estimates',
    )
    parser.add_argument('--imputation-top-values', type=int, choices=range(10, 21), default=20)
    parser.add_argument('--imputation-seed', type=int, default=42)


def preprocessing_options(args):
    mapping = json.loads(args.column_map.read_text()) if args.column_map else None
    if mapping is not None and not isinstance(mapping, dict):
        raise ValueError('--column-map must contain a JSON object')
    return {
        'column_map': mapping,
        'input_timezone': args.input_timezone,
        'timestamp_format': args.timestamp_format,
        'day_first': args.day_first,
        'power_unit': args.power_unit,
        'min_coverage': args.min_coverage,
        'drop_partial_hours': args.drop_partial_hours,
        'impute_missing': args.impute_missing,
        'imputation_top_values': args.imputation_top_values,
        'imputation_seed': args.imputation_seed,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_preprocessing_arguments(parser)
    parser.add_argument('--output', type=Path, default=config.DATASET)
    parser.add_argument(
        '--fetch-prices', action='store_true',
        help='Fetch or validate prices without preparing SCADA data',
    )
    parser.add_argument(
        '--start', type=date.fromisoformat,
        help='First IST delivery date YYYY-MM-DD for --fetch-prices',
    )
    parser.add_argument(
        '--end', type=date.fromisoformat,
        help='Last IST delivery date YYYY-MM-DD for --fetch-prices',
    )
    args = parser.parse_args(argv)
    if args.fetch_prices:
        if args.start is None or args.end is None:
            parser.error('--fetch-prices requires both --start and --end')
        if args.end < args.start:
            parser.error('--end must be on or after --start')
        if args.raw_data or args.prices or args.column_map:
            parser.error('--fetch-prices cannot be combined with SCADA input options')
    elif args.start is not None or args.end is not None:
        parser.error('--start and --end require --fetch-prices')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        if args.fetch_prices:
            hours = pd.date_range(
                str(args.start), str(args.end) + ' 23:00', freq='h',
                tz=config.TIMEZONE, name='timestamp_local_IN',
            )
            prices, details = prices_for_hours(hours)
            print(f'Validated {len(prices)} timestamp-matched hourly IEX prices')
            print(json.dumps(details, indent=2))
            return

        frame, quality = build_dataset(
            args.raw_data or config.RAW_DATA, args.prices, args.output,
            **preprocessing_options(args),
        )
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))
    print(f'Generated {len(frame)} hourly records: {args.output}')
    print(json.dumps(quality, indent=2))


if __name__ == '__main__':
    main()
