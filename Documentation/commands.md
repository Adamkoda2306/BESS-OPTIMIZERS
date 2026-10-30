# Running dataset preparation and the BESS models

Run the commands below from the repository root, which contains `env/`,
`dataset_transform/`, `grid_bess_optimizer/`, and `solarnex_bess_engine/`.
Paths containing spaces must stay inside quotes. In multiline shell commands,
keep the backslash as the last character on each continued line.

The raw files currently live in `datasets/original/`. Supply `--raw-data`
explicitly: the preparation configuration still has the older root-level
September sample path as its fallback.

For model details, see [Grid BESS](grid_bess.md) and [Solarnex BESS](solarnex_bess.md).

## Environment and help

Use the existing environment with `env/bin/python`; activation is optional.
Dataset preparation needs NumPy and pandas. The optimizers also need PuLP,
Matplotlib, and a working CBC solver. GIF generation uses Pillow.

If dependencies are missing from the environment:

```bash
env/bin/python -m pip install -r requirements.txt
```

Show the available options:

```bash
env/bin/python -m dataset_transform --help
env/bin/python grid_bess_optimizer/model.py --help
env/bin/python solarnex_bess_engine/model.py --help
```

The battery packages have four source files and use `model.py` as their command
entry point. Use the script commands above rather than `python -m grid_bess_optimizer`
or `python -m solarnex_bess_engine`, since neither has a `__main__.py`.

## Prepare a dataset and run both models

This sequence generates the shared default input file, runs one selected day
through Grid BESS, and runs the whole generated dataset through Solarnex:

```bash
env/bin/python -m dataset_transform \
  --raw-data "datasets/original/September 2023 - Sheet1.csv" \
  --output datasets/converted/september_2023_hourly.csv

env/bin/python grid_bess_optimizer/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --date 2023-09-15 \
  --no-show

env/bin/python solarnex_bess_engine/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --no-show
```

The preparation step writes the CSV and `september_2023_hourly.metadata.json`. The two
models write to `output/grid_bess_optimizer/` and `output/solarnex_bess_engine/`.
`--no-show` saves charts without opening chart windows. Omit it to display them.
Repeated runs into the same output directory replace files with the same names.

## Dataset preparation commands

### Fetch matching prices automatically

For August 2023, use a separate output filename to retain the September dataset:

```bash
env/bin/python -m dataset_transform \
  --raw-data "datasets/original/August 2023 - Sheet1.csv" \
  --output datasets/converted/august_2023_hourly.csv
```

Input dates are inferred from the timestamps. The code supports other months
and years with the same input structure. For an August 2022 CSV placed at the
repository root, for example:

```bash
env/bin/python -m dataset_transform \
  --raw-data "August 2022 - Sheet1.csv" \
  --output datasets/converted/august_2022_hourly.csv
```

The August 2022 file is an example input name; provide the file before running
that command. Historical price retrieval must return all requested hours for
the selected dates or the run stops with an error.

Price selection follows this order:

1. The file explicitly supplied with `--prices`.
2. A recognized price column already present in the raw CSV.
3. An online request to IEX for matching delivery dates.

There is no offline switch or price cache. Online responses stay in memory;
raw HTML and downloaded record files are not saved. The generated dataset still
contains hourly prices, and its metadata records the request URLs and retrieval
times. A run using supplied or embedded prices does not need an IEX request.

### Use a supplied price file

```bash
env/bin/python -m dataset_transform \
  --raw-data "August 2022 - Sheet1.csv" \
  --prices "august_2022_prices.csv" \
  --output datasets/converted/august_2022_hourly.csv
```

A price CSV needs a recognizable timestamp column, such as `timestamp`, `Time`,
or `timestamp_local_IN`, and `price_inr_mwh` or `price_inr_kwh`. It must cover
every required hour with complete, regular samples. If both price units are
present, their values must agree: INR/MWh equals INR/kWh multiplied by 1,000.
Use explicit timezone offsets when the price file's timezone differs from the
raw measurements.

An explicitly supplied JSON file is also accepted. It must contain an array of
IEX-style records with `date` as `DD-MM-YYYY`, `hour` as 1 through 24, and `mcp`
in INR/MWh. Hour 1 starts at 00:00 IST. The program reads this file without
copying it into a cache.

### Fetch and validate prices without generating a dataset

```bash
env/bin/python -m dataset_transform \
  --fetch-prices \
  --start 2023-08-01 \
  --end 2023-08-31
```

Both dates are inclusive. Longer ranges are split into requests of at most
31 delivery days. This command prints the validated hour count and source
information; it does not create a price file or a dataset. `--start` and `--end`
require `--fetch-prices`. Do not combine this mode with `--raw-data`, `--prices`,
or `--column-map`.

### Use a column map or adjust cleaning

The supplied mapping example describes the existing SCADA channel names:

```bash
env/bin/python -m dataset_transform \
  --raw-data "datasets/original/September 2023 - Sheet1.csv" \
  --column-map dataset_transform/column_map.example.json \
  --input-timezone Asia/Kolkata \
  --power-unit MW \
  --min-coverage 0.85 \
  --drop-partial-hours \
  --output datasets/converted/september_2023_hourly.csv
```

Use your own JSON mapping for differently named columns. Required measurements
are wind, solar, gas, and demand, plus a timestamp. Thermal, hydro, and nuclear
columns are retained when available. Regular sampling intervals must divide an
hour exactly and cannot exceed one hour. Samples mark the start of each interval.

| Option | Meaning and default |
| --- | --- |
| `-h`, `--help` | Show usage and exit. |
| `--raw-data PATH` | Raw SCADA CSV. Defaults to `RAW_DATA` in `dataset_transform/config.py`; use an explicit path for the current folder layout. |
| `--prices PATH` | Optional price CSV or IEX-style JSON. |
| `--output PATH` | Generated CSV; default is `dataset_transform/datasets/grid_india_hourly.csv`. |
| `--column-map PATH` | JSON mapping standard field names to raw CSV column names. |
| `--input-timezone NAME` | Timezone for timestamps without offsets; default `Asia/Kolkata`. Explicit offsets take precedence. |
| `--timestamp-format FORMAT` | Explicit pandas timestamp format, for example `"%d/%m/%Y %H:%M:%S"`; otherwise mixed formats are parsed. |
| `--day-first` | Interpret ambiguous dates as day/month/year. |
| `--power-unit MW\|kW` | Unit of raw power measurements; default `MW`. kW is converted to MW. |
| `--min-coverage FRACTION` | Required usable samples per hour, in `(0, 1]`; default `0.85`. |
| `--drop-partial-hours` | Drop incomplete boundary hours. By default these cause an error. |
| `--impute-missing` | Fill missing/invalid power readings by randomly sampling frequent valid values from the same column and month. Adds estimate flags and sample counts. |
| `--imputation-top-values INTEGER` | Number of most frequent distinct valid values to sample from, from 10 to 20; default `20`. Used with `--impute-missing`. |
| `--imputation-seed INTEGER` | Nonnegative random seed for repeatable imputation; default `42`. |
| `--fetch-prices` | Retrieve and validate prices only. |
| `--start YYYY-MM-DD` | First delivery date for price-only mode. |
| `--end YYYY-MM-DD` | Last delivery date for price-only mode. |

By default, cleaning clips negative solar readings to zero and excludes simultaneous-zero
telemetry dropouts from hourly means. Negative non-solar measurements, duplicate
timestamps, insufficient hourly coverage, and missing prices cause errors.
No interpolation is performed. Coverage below 95% is flagged even when it meets
the chosen minimum. The metadata records cleaning counts and source information.

To fill missing or invalid measurements with the requested frequent-value method:

```bash
env/bin/python -m dataset_transform \
  --raw-data "datasets/original/July 2022 - Sheet1.csv" \
  --impute-missing \
  --imputation-top-values 20 \
  --imputation-seed 42 \
  --output datasets/converted/july_2022_hourly.csv
```

This mode fills absent samples, missing/non-finite power values, negative power
values (including negative solar), and simultaneous-zero telemetry dropouts.
For each column and calendar month, it selects the 20 most frequent distinct
valid values and draws uniformly from that set with replacement. Frequency ties
are ordered by numeric value. If fewer distinct valid values exist, it uses all
of them; a column with no valid values in that month causes an error.

Imputation happens at the original sampling interval, before hourly averaging.
Observed valid values are retained. The CSV includes `estimated_data_flag`,
`imputed_sample_count`, and a `<column>_imputed_sample_count` for each power
column. Derived renewable and residual-load values may also include estimates
when their inputs were imputed. Coverage columns continue to describe original
usable measurements; low coverage is flagged rather than rejected in this mode.
The metadata records the method, seed, candidate values, and estimate counts.
Prices are always observed values and are never imputed. Random imputation
does not preserve time-of-day patterns or relationships between power columns;
use the flags when interpreting downstream results.

The direct script entry point accepts the same arguments:

```bash
env/bin/python dataset_transform/generate_dataset.py \
  --raw-data "datasets/original/September 2023 - Sheet1.csv" \
  --output datasets/converted/september_2023_hourly.csv
```

## Grid BESS commands

Run one day using custom battery settings and a separate result directory:

```bash
env/bin/python grid_bess_optimizer/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --date 2023-09-15 \
  --capacity-mwh 2 \
  --max-power-mw 1 \
  --efficiency 0.92 \
  --output-dir output/grid_bess_optimizer/september_15 \
  --no-show
```

| Option | Meaning and default |
| --- | --- |
| `-h`, `--help` | Show usage and exit. |
| `--dataset PATH` | Prepared hourly CSV; default `dataset_transform/datasets/new_hourly.csv`. |
| `--date YYYY-MM-DD` | Select exactly one complete IST day; default is the first date in the file. |
| `--capacity-mwh VALUE` | Battery energy capacity; default `2.0`. |
| `--max-power-mw VALUE` | Charge and discharge power limit; default `1.0`. |
| `--efficiency FRACTION` | Round-trip efficiency in `(0, 1]`; default `0.92`. |
| `--output-dir PATH` | Result folder; default `output/grid_bess_optimizer/`. |
| `--no-show` | Save charts without opening windows. |

Omitting `--date` does not optimize the entire file. The selected date must have
24 hourly records. To study another day, run the command again with another date
and output directory.

## Solarnex BESS commands

Run the full input with rolling dispatch and an animation of the first week:

```bash
env/bin/python solarnex_bess_engine/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --capacity-mwh 10 \
  --max-power-mw 2.5 \
  --rte 0.90 \
  --horizon-hours 48 \
  --step-hours 24 \
  --animate \
  --animation-hours 168 \
  --no-show
```

Run one selected day with price filtering disabled:

```bash
env/bin/python solarnex_bess_engine/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --date 2023-09-15 \
  --no-spike-filter \
  --output-dir output/solarnex_bess_engine/september_15 \
  --no-show
```

Run a multi-day date range (e.g. 15–23 September 2023 inclusive):

```bash
env/bin/python solarnex_bess_engine/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --start-date 2023-09-15 \
  --end-date 2023-09-23 \
  --output-dir output/solarnex_bess_engine/september_15_23 \
  --no-show
```

| Option | Meaning and default |
| --- | --- |
| `-h`, `--help` | Show usage and exit. |
| `--dataset PATH` | Prepared hourly CSV; default `dataset_transform/datasets/new_hourly.csv`. |
| `--date YYYY-MM-DD` | Optional complete IST day; default uses the full input. |
| `--start-date YYYY-MM-DD` | Optional start date (IST) for selecting a date range. |
| `--end-date YYYY-MM-DD` | Optional end date (IST) for selecting a date range. |
| `--capacity-mwh VALUE` | Battery energy capacity; default `10.0`. |
| `--max-power-mw VALUE` | Charge and discharge power limit; default `2.5`. |
| `--rte FRACTION` | Round-trip efficiency in `(0, 1]`; default `0.90`. |
| `--horizon-hours INTEGER` | Lookahead window; default `48`. |
| `--step-hours INTEGER` | Hours committed before solving again; default `24`. Must be positive and no greater than the horizon. |
| `--no-spike-filter` | Disable the window price filter; filtering is enabled by default. |
| `--output-dir PATH` | Result folder; default `output/solarnex_bess_engine/`. |
| `--no-show` | Save charts without opening windows. |
| `--animate`, `--no-animate` | Save the dispatch GIF (default: enabled; use `--no-animate` to disable). |
| `--animation-hours INTEGER` | Number of hours shown in the GIF; default: all simulated hours (or first week for full monthly run). |
| `--gif-duration SECONDS` | Total duration of the animated GIF in seconds; default `40.0`. |
| `--gif-frame-duration SECONDS` | Optional override for the duration of each individual frame in seconds. |

## Paths and common problems

The preparation tool's default output filename differs from the optimizers'
default input filename. The first example explicitly writes `new_hourly.csv`
so all three commands agree. For any other output filename, pass that same
path to the optimizer with `--dataset`.

Default model output paths are resolved from the repository location, even
when a script is called from elsewhere. Relative paths supplied on the command
line are resolved from the shell's current directory.

If CBC is unavailable, install a compatible CBC executable or use a PuLP
installation that supplies one. Point the models to a specific installation
when needed:

```bash
CBC_PATH=/absolute/path/to/cbc env/bin/python grid_bess_optimizer/model.py \
  --dataset datasets/converted/september_2023_hourly.csv \
  --date 2023-09-15 \
  --no-show
```

Both models first check `CBC_PATH`, then `cbc` on `PATH`, then known Homebrew
locations, and finally PuLP's bundled solver. A solver result must be `Optimal`.

For a missing input file, check the quoted path and generate the hourly dataset
before starting a model. For a date-selection error, choose a complete day
present in the CSV. If IEX retrieval fails or returns a different date range,
retry or supply matching observed prices using `--prices`; the program does
not substitute prices from another month.
