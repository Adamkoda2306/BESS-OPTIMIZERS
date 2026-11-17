"""
Inference & Prediction Script for Electricity Price Forecasting.

Fetches historical hourly prices from the Indian Energy Exchange (IEX)
for the preceding 6 days up to the requested target date, inputs them into
the trained Deep Residual Highway ANN model, and forecasts the next day's 24-hour
hourly prices (both INR/MWh and INR/kWh).

If intermediate dates are not yet published on IEX (e.g. forecasting dates further
in the future like Day T+2 or T+3), it seamlessly engages an Autoregressive /
Recursive Rolling Forecast:
    [Actual IEX Data up to T_latest] ──> Forecast Next Day (T_latest + 1)
                                                 │
                                                 ▼
    [Actual + Intermediate Forecasts] ──> Forecast Target Day

Saves the forecast to a CSV file and generates a publication-ready forecast plot.

Example Usage:
    python forecasting_model/predict.py --date 2026-11-17
    python forecasting_model/predict.py --date 2026-11-20
    python forecasting_model/predict.py
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Tuple, Dict, List, Optional, Any

# Ensure matplotlib uses a writable cache directory
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

# Add current and parent directories to sys.path
CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

import numpy as np
import pandas as pd
import torch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config
from model import build_model, DeepPriceForecastingANN
from dataset import PriceDataPipeline, get_window_by_date
from test import load_trained_model
from dataset_transform.preprocess import download_records


def fetch_iex_prices(start_date: date, end_date: date) -> pd.DataFrame:
    """
    Fetch hourly observed prices from the Indian Energy Exchange (IEX) for a date range.
    Falls back to local dataset if internet / IEX is unreachable.
    """
    try:
        print(f"[INFO] Fetching observed IEX hourly prices from {start_date} to {end_date}...")
        prices, _ = download_records(start_date, end_date)
        return prices
    except Exception as exc:
        print(f"[WARNING] Live IEX fetch failed ({exc}). Checking local dataset fallback...")
        pipeline = PriceDataPipeline(data_dir=config.DATASET_DIR)
        pipeline.load_raw_data()
        df = pipeline.df
        if df is not None:
            tz = df["timestamp"].dt.tz
            start_ts = pd.Timestamp(start_date).tz_localize(tz)
            end_ts = pd.Timestamp(end_date).tz_localize(tz) + pd.Timedelta(hours=23)
            sub = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].copy()
            if len(sub) == ((end_date - start_date).days + 1) * 24:
                sub.index = pd.DatetimeIndex(sub["timestamp"]).rename("timestamp_local_IN")
                return pd.DataFrame({
                    "price_inr_mwh": sub[pipeline.price_column].values,
                    "price_inr_kwh": sub[pipeline.price_column].values / 1000.0,
                }, index=sub.index)
        raise RuntimeError(
            f"Could not retrieve IEX prices for {start_date} through {end_date}. "
            f"Original error: {exc}"
        )


def find_latest_available_iex_date() -> date:
    """
    Determine the most recent delivery date available on IEX DAM.
    Since DAM clears at most 1 day forward, checks candidate dates starting from tomorrow backwards.
    """
    # Current time in IST (UTC+5:30)
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    candidate = now_ist.date() + timedelta(days=1)

    for _ in range(5):
        try:
            p, _ = download_records(candidate, candidate)
            if len(p) == 24:
                return candidate
        except Exception:
            candidate -= timedelta(days=1)

    # Fallback to current IST date
    return now_ist.date()


def fetch_actual_target_prices(target_date: date) -> Optional[np.ndarray]:
    """
    Attempt to fetch actual published IEX prices for the target date to calculate accuracy.
    Returns None if the prices are not yet published (future date).
    """
    try:
        prices, _ = download_records(target_date, target_date)
        if len(prices) == 24:
            return prices["price_inr_mwh"].values.astype(np.float32)
    except Exception:
        # Check local dataset as fallback
        try:
            pipeline = PriceDataPipeline(data_dir=config.DATASET_DIR)
            pipeline.load_raw_data()
            df = pipeline.df
            if df is not None:
                tz = df["timestamp"].dt.tz
                start_ts = pd.Timestamp(target_date).tz_localize(tz)
                end_ts = start_ts + pd.Timedelta(hours=23)
                sub = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)]
                if len(sub) == 24:
                    return sub[pipeline.price_column].values.astype(np.float32)
        except Exception:
            pass
    return None


def prepare_features(
    past_prices: np.ndarray,
    target_date: date,
    required_past_hours: int = 168,
    use_feature_engineering: bool = True,
) -> Tuple[np.ndarray, float, float]:
    """
    Prepare normalized feature vector and statistical scaling parameters for the model.
    Adapts 144 hours (6 days) to 168 hours (7 days) if the model requires a 168-hour input window.
    """
    if len(past_prices) < required_past_hours:
        # If 144 hours (6 days) provided and 168 hours (7 days) needed, prepend day 1
        needed_pad = required_past_hours - len(past_prices)
        if needed_pad == 24:
            past_prices = np.concatenate([past_prices[:24], past_prices])
        else:
            raise ValueError(
                f"Insufficient historical prices: received {len(past_prices)} hours, "
                f"model requires {required_past_hours} hours."
            )

    w_mean = float(past_prices.mean())
    w_std = float(past_prices.std() + 1e-4)
    norm_past = (past_prices - w_mean) / w_std

    if use_feature_engineering:
        # 1. Diurnal 24-hour profile averaged over past days
        daily_profile = past_prices.reshape(-1, 24).mean(axis=0)
        norm_daily_profile = (daily_profile - daily_profile.mean()) / (daily_profile.std() + 1e-4)

        # 2. Yesterday's 24 hours
        yesterday = past_prices[-24:]
        norm_yesterday = (yesterday - yesterday.mean()) / (yesterday.std() + 1e-4)

        # 3. Cyclical target day-of-week encoding
        target_dow = target_date.weekday()
        dow_sin = np.sin(2.0 * np.pi * target_dow / 7.0)
        dow_cos = np.cos(2.0 * np.pi * target_dow / 7.0)

        # 4. Cyclical target month encoding
        target_month = target_date.month
        month_sin = np.sin(2.0 * np.pi * target_month / 12.0)
        month_cos = np.cos(2.0 * np.pi * target_month / 12.0)

        # 5. Trend and scale metrics
        trend_ratio = float(yesterday.mean() / (w_mean + 1e-4))
        log_level = float(np.log1p(w_mean))
        log_volatility = float(np.log1p(w_std))

        feat_vec = np.concatenate([
            norm_past,
            norm_daily_profile,
            norm_yesterday,
            [dow_sin, dow_cos, month_sin, month_cos, trend_ratio, log_level, log_volatility],
        ])
    else:
        feat_vec = norm_past

    return feat_vec.astype(np.float32), w_mean, w_std


def predict_date_iex(
    target_date_str: str,
    model: DeepPriceForecastingANN,
    device: str = "cpu",
    past_days: int = 6,
    past_hours_model: int = 168,
    use_feature_engineering: bool = True,
) -> Tuple[np.ndarray, Optional[np.ndarray], pd.DataFrame, Dict[str, Any]]:
    """
    Collect the preceding `past_days` (default 6 days) of IEX prices,
    run model inference, and return forecasted prices and DataFrame.

    If intermediate dates are missing on IEX (e.g. target date is further ahead),
    it runs Autoregressive / Recursive Rolling Forecasting to project intermediate days.
    """
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    needed_preceding_end = target_date - timedelta(days=1)
    needed_preceding_start = target_date - timedelta(days=past_days)

    print(f"\n[INFO] Target Date: {target_date}")

    # Determine latest available date on IEX
    latest_avail = find_latest_available_iex_date()
    print(f"[INFO] Latest delivery date currently available on IEX: {latest_avail}")

    meta = {
        "target_date": target_date,
        "is_recursive": False,
        "rolled_from": None,
        "intermediate_dates": [],
        "intermediate_forecasts": {},
    }

    # Case 1: Preceding data is fully available on IEX (standard 1-step forecast)
    if latest_avail >= needed_preceding_end:
        print(f"[INFO] Collecting IEX data for preceding {past_days} days: {needed_preceding_start} to {needed_preceding_end}...")
        iex_prices = fetch_iex_prices(needed_preceding_start, needed_preceding_end)
        print(f"[INFO] Successfully retrieved {len(iex_prices)} hourly prices from IEX.")
        past_prices_arr = iex_prices["price_inr_mwh"].values.astype(np.float32)

        feat_vec, w_mean, w_std = prepare_features(
            past_prices=past_prices_arr,
            target_date=target_date,
            required_past_hours=past_hours_model,
            use_feature_engineering=use_feature_engineering,
        )

        feat_tensor = torch.from_numpy(feat_vec).float().unsqueeze(0).to(device)
        with torch.no_grad():
            norm_pred = model(feat_tensor).cpu().numpy()[0]

        pred_prices = norm_pred * w_std + w_mean
        pred_prices = np.clip(pred_prices, 0.0, None)

    # Case 2: Intermediate dates are missing on IEX (Autoregressive Multi-Step Roll)
    else:
        meta["is_recursive"] = True
        meta["rolled_from"] = latest_avail

        # Identify missing intermediate dates
        dates_to_roll = []
        curr = latest_avail + timedelta(days=1)
        while curr < target_date:
            dates_to_roll.append(curr)
            curr += timedelta(days=1)

        meta["intermediate_dates"] = dates_to_roll

        print(f"[NOTE] The preceding day ({needed_preceding_end}) is not yet auctioned/cleared on IEX.")
        print(f"[INFO] Engaging Autoregressive Multi-Step Rolling Forecaster:")
        print(f"       1. Start with actual IEX prices up to {latest_avail}.")
        if dates_to_roll:
            date_chain = " -> ".join([str(d) for d in dates_to_roll])
            print(f"       2. Sequentially predict intermediate dates: {date_chain}.")
        print(f"       3. Roll the historical window forward to forecast final target date {target_date}.\n")

        # Fetch initial historical lookback window up to latest_avail
        start_hist = latest_avail - timedelta(days=past_days - 1)
        print(f"[FETCH] Retrieving base IEX historical series: {start_hist} to {latest_avail}...")
        hist_df = fetch_iex_prices(start_hist, latest_avail)
        rolling_prices = hist_df["price_inr_mwh"].values.astype(np.float32)

        # Roll through each intermediate day
        step_idx = 1
        total_steps = len(dates_to_roll) + 1
        for inter_date in dates_to_roll:
            print(f"  [Step {step_idx}/{total_steps}] Forecasting intermediate day {inter_date}...")
            input_window = rolling_prices[-(past_days * 24):]
            feat_vec, wm, ws = prepare_features(
                past_prices=input_window,
                target_date=inter_date,
                required_past_hours=past_hours_model,
                use_feature_engineering=use_feature_engineering,
            )
            feat_t = torch.from_numpy(feat_vec).float().unsqueeze(0).to(device)
            with torch.no_grad():
                n_pred = model(feat_t).cpu().numpy()[0]
            inter_pred = np.clip(n_pred * ws + wm, 0.0, None)

            # Record intermediate forecast and append to rolling series
            meta["intermediate_forecasts"][str(inter_date)] = inter_pred
            rolling_prices = np.concatenate([rolling_prices, inter_pred])
            print(f"            ✓ Projected {inter_date} (mean: {inter_pred.mean():.2f} INR/MWh). Window rolled forward.")
            step_idx += 1

        # Forecast final target date using the fully rolled series
        print(f"  [Step {step_idx}/{total_steps}] Forecasting target date {target_date}...")
        input_window = rolling_prices[-(past_days * 24):]
        feat_vec, wm, ws = prepare_features(
            past_prices=input_window,
            target_date=target_date,
            required_past_hours=past_hours_model,
            use_feature_engineering=use_feature_engineering,
        )
        feat_t = torch.from_numpy(feat_vec).float().unsqueeze(0).to(device)
        with torch.no_grad():
            n_pred = model(feat_t).cpu().numpy()[0]
        pred_prices = np.clip(n_pred * ws + wm, 0.0, None)
        print(f"            ✓ Successfully generated final forecast for {target_date}!")

    # Check for actual prices for the target date if available
    actual_prices = fetch_actual_target_prices(target_date)
    if actual_prices is not None:
        print(f"[INFO] Observed actual prices for {target_date} detected. Accuracy metrics will be calculated.")
    else:
        print(f"[INFO] Target date {target_date} is future/unreleased. Generating pure forecast curve.")

    # Construct forecast DataFrame
    records = []
    has_actuals = actual_prices is not None and len(actual_prices) == 24
    for hour in range(24):
        rec = {
            "target_date": str(target_date),
            "hour": f"{hour:02d}:00",
            "forecast_price_inr_mwh": round(float(pred_prices[hour]), 2),
            "forecast_price_inr_kwh": round(float(pred_prices[hour] / 1000.0), 4),
        }
        if has_actuals:
            act = float(actual_prices[hour])
            rec["actual_price_inr_mwh"] = round(act, 2)
            rec["actual_price_inr_kwh"] = round(act / 1000.0, 4)
            err = abs(float(pred_prices[hour]) - act)
            rec["absolute_error_inr_mwh"] = round(err, 2)
            pct_err = (err / (act + 1e-4)) * 100.0
            rec["percentage_error"] = round(pct_err, 2)
            rec["accuracy_pct"] = round(max(0.0, 100.0 - pct_err), 2)
        records.append(rec)

    forecast_df = pd.DataFrame(records)
    return pred_prices, actual_prices if has_actuals else None, forecast_df, meta


def predict_latest(
    model: DeepPriceForecastingANN,
    pipeline: PriceDataPipeline,
    device: str = "cpu",
    past_days: int = 7,
) -> Tuple[np.ndarray, pd.DataFrame]:
    """Generate forecast for the very next day after the end of the local dataset."""
    if pipeline.df is None:
        pipeline.load_raw_data()

    df = pipeline.df
    past_hours = past_days * 24
    input_slice = df.iloc[-past_hours:].reset_index(drop=True)
    past_prices = input_slice[pipeline.price_column].values.astype(np.float32)

    last_ts = df["timestamp"].iloc[-1]
    next_day_start = last_ts + pd.Timedelta(hours=1)
    target_date = next_day_start.date()

    feat_vec, w_mean, w_std = prepare_features(
        past_prices=past_prices,
        target_date=target_date,
        required_past_hours=past_hours,
        use_feature_engineering=True,
    )

    feat_tensor = torch.from_numpy(feat_vec).float().unsqueeze(0).to(device)
    with torch.no_grad():
        norm_pred = model(feat_tensor).cpu().numpy()[0]

    pred_prices = norm_pred * w_std + w_mean
    pred_prices = np.clip(pred_prices, 0.0, None)

    records = []
    for hour in range(24):
        records.append({
            "target_date": str(target_date),
            "hour": f"{hour:02d}:00",
            "forecast_price_inr_mwh": round(float(pred_prices[hour]), 2),
            "forecast_price_inr_kwh": round(float(pred_prices[hour] / 1000.0), 4),
        })
    return pred_prices, pd.DataFrame(records)


def plot_single_day_forecast(
    pred_prices: np.ndarray,
    actual_prices: Optional[np.ndarray],
    title: str,
    save_path: Path,
    is_recursive: bool = False,
    rolled_from: Optional[date] = None,
):
    """Generate a clean, high-resolution 24-hour forecast curve plot."""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    hours = list(range(24))

    fig, ax = plt.subplots(figsize=(12, 5.5), dpi=200)

    if actual_prices is not None:
        ax.plot(
            hours, actual_prices,
            marker="o", color="#1f77b4", linewidth=2.2, markersize=5,
            label="Actual IEX Price", zorder=3,
        )
        ax.plot(
            hours, pred_prices,
            marker="s", linestyle="--", color="#d62728", linewidth=2.2, markersize=5,
            label="ANN Forecast Price", zorder=4,
        )
        ax.fill_between(
            hours, actual_prices, pred_prices,
            color="#d62728", alpha=0.15, label="Prediction Gap (Residual Error)", zorder=2,
        )
        wape_acc = max(0.0, (1.0 - np.sum(np.abs(pred_prices - actual_prices)) / np.sum(actual_prices)) * 100.0)
        mape_acc = max(0.0, (1.0 - np.mean(np.abs(pred_prices - actual_prices) / actual_prices)) * 100.0)
        subtitle = f"Day Accuracy: {wape_acc:.2f}% (1 - WAPE) | Mean Absolute Accuracy: {mape_acc:.2f}%"
    else:
        ax.plot(
            hours, pred_prices,
            marker="s", linestyle="-", color="#007acc", linewidth=2.5, markersize=6,
            label="24-Hour Forecast (INR/MWh)", zorder=3,
        )
        ax.fill_between(hours, 0, pred_prices, color="#007acc", alpha=0.12, label="Forecasted Price Level", zorder=2)
        if is_recursive and rolled_from is not None:
            subtitle = (
                f"Autoregressive Rolling Forecast (Rolled from IEX actuals up to {rolled_from})\n"
                f"Mean Forecasted Price: {pred_prices.mean():.2f} INR/MWh ({pred_prices.mean()/1000.0:.3f} INR/kWh)"
            )
        else:
            subtitle = f"Mean Forecasted Price: {pred_prices.mean():.2f} INR/MWh ({pred_prices.mean()/1000.0:.3f} INR/kWh)"

    # Highlight min and max forecasted hours
    min_h = int(np.argmin(pred_prices))
    max_h = int(np.argmax(pred_prices))
    ax.scatter([min_h], [pred_prices[min_h]], color="#2ca02c", s=100, zorder=5, label=f"Min: {pred_prices[min_h]:.1f} @ {min_h:02d}:00")
    ax.scatter([max_h], [pred_prices[max_h]], color="#d62728", s=100, zorder=5, label=f"Peak: {pred_prices[max_h]:.1f} @ {max_h:02d}:00")

    ax.set_title(f"{title}\n{subtitle}", fontsize=12, fontweight="bold", pad=12)
    ax.set_xlabel("Hour of Day (IST: 00:00 to 23:00)", fontsize=11, fontweight="medium")
    ax.set_ylabel("Price (INR/MWh)", fontsize=11, fontweight="medium")
    ax.set_xticks(hours)
    ax.set_xticklabels([f"{h:02d}:00" for h in hours], rotation=45, ha="right", fontsize=9)
    ax.set_xlim(-0.5, 23.5)
    ax.grid(True, linestyle=":", alpha=0.6, zorder=1)
    ax.legend(loc="upper left", frameon=True, framealpha=0.9, fontsize=9)

    plt.tight_layout()
    plt.savefig(save_path)
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Forecast Next Day Hourly Electricity Prices using IEX Data and Trained Deep ANN"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target delivery date to forecast (YYYY-MM-DD, e.g. 2026-11-20). Prompted interactively if omitted.",
    )
    parser.add_argument(
        "--latest", action="store_true",
        help="Forecast for the next day after dataset end without specifying a date.",
    )
    parser.add_argument(
        "--past-days", type=int, default=6,
        help="Number of lookback days to collect from IEX (default: 6 days).",
    )
    parser.add_argument(
        "--checkpoint", type=str, default=str(config.BEST_MODEL_PATH),
        help="Path to trained model checkpoint (.pth).",
    )
    parser.add_argument(
        "--device", type=str, default=config.DEVICE,
        help="Inference device: cpu, mps, cuda.",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Optional custom save path for output forecast CSV.",
    )
    parser.add_argument(
        "--plot-output", type=str, default=None,
        help="Optional custom save path for output forecast plot.",
    )
    args = parser.parse_args()

    # Determine target date: CLI argument or prompt user
    target_date_str = args.date
    if not args.latest and (target_date_str is None or not str(target_date_str).strip()):
        while True:
            try:
                user_input = input("Enter target date to forecast (YYYY-MM-DD, e.g. 2026-11-20): ").strip()
                if not user_input:
                    print("Date cannot be empty. Please enter a valid date in YYYY-MM-DD format.")
                    continue
                datetime.strptime(user_input, "%Y-%m-%d")
                target_date_str = user_input
                break
            except ValueError:
                print("Invalid format. Please use YYYY-MM-DD (e.g. 2026-11-20).")
            except (KeyboardInterrupt, EOFError):
                print("\nOperation cancelled.")
                return

    # Load model checkpoint
    model, checkpoint = load_trained_model(args.checkpoint, device=args.device)
    past_hours_model = checkpoint.get("past_hours", config.INPUT_HOURS)
    use_fe = checkpoint.get("use_feature_engineering", config.USE_FEATURE_ENGINEERING)

    if args.latest:
        pipeline = PriceDataPipeline(
            data_dir=config.DATASET_DIR,
            price_column=config.PRICE_COLUMN,
            past_days=args.past_days,
            forecast_days=config.FORECAST_DAYS,
            use_feature_engineering=use_fe,
        )
        pipeline.load_raw_data()
        preds, df = predict_latest(model, pipeline, device=args.device, past_days=args.past_days)
        target_date_str = df["target_date"].iloc[0]
        actuals = None
        meta = {"is_recursive": False, "rolled_from": None, "intermediate_dates": []}
    else:
        try:
            datetime.strptime(target_date_str, "%Y-%m-%d")
        except ValueError:
            print(f"Error: Invalid date '{target_date_str}'. Expected format YYYY-MM-DD (e.g. 2026-11-20).")
            return

        preds, actuals, df, meta = predict_date_iex(
            target_date_str=target_date_str,
            model=model,
            device=args.device,
            past_days=args.past_days,
            past_hours_model=past_hours_model,
            use_feature_engineering=use_fe,
        )

    # Print clean results table
    print("\n" + "=" * 84)
    mode_str = " (AUTOREGRESSIVE ROLLING FORECAST)" if meta["is_recursive"] else ""
    print(f"       24-HOUR HOURLY PRICE FORECAST FOR {target_date_str}{mode_str}       ")
    print("=" * 84)
    if actuals is not None:
        print(f"{'Hour':>6} | {'Forecast (INR/MWh)':>19} | {'Actual (INR/MWh)':>17} | {'Error (INR)':>12} | {'Accuracy %':>12}")
        print("-" * 84)
        for _, row in df.iterrows():
            print(
                f"{row['hour']:>6} | {row['forecast_price_inr_mwh']:19.2f} | "
                f"{row['actual_price_inr_mwh']:17.2f} | {row['absolute_error_inr_mwh']:12.2f} | "
                f"{row['accuracy_pct']:11.2f}%"
            )
        print("-" * 84)
        total_wapa = max(0.0, (1.0 - np.sum(np.abs(preds - actuals)) / np.sum(actuals)) * 100.0)
        mape_acc = max(0.0, (1.0 - np.mean(np.abs(preds - actuals) / actuals)) * 100.0)
        print(f"Overall Day Accuracy (1 - WAPE): {total_wapa:.2f}% | Mean Absolute Accuracy (1 - MAPE): {mape_acc:.2f}%")
    else:
        print(f"{'Hour':>6} | {'Forecast (INR/MWh)':>20} | {'Forecast (INR/kWh)':>20}")
        print("-" * 84)
        for _, row in df.iterrows():
            print(f"{row['hour']:>6} | {row['forecast_price_inr_mwh']:20.2f} | {row['forecast_price_inr_kwh']:20.4f}")
    print("=" * 84)

    # Save target CSV
    out_path = Path(args.output) if args.output else config.PREDICTED_SAVED_MODELS_DIR / f"forecast_{target_date_str}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\n[OUTPUT] Target forecast CSV saved to: {out_path}")

    # Generate and save target plot
    plot_path = Path(args.plot_output) if args.plot_output else config.PREDICTED_PLOTS_DIR / f"forecast_{target_date_str}.png"
    plot_single_day_forecast(
        pred_prices=preds,
        actual_prices=actuals,
        title=f"24-Hour Price Forecast for {target_date_str}",
        save_path=plot_path,
        is_recursive=meta["is_recursive"],
        rolled_from=meta["rolled_from"],
    )
    print(f"[OUTPUT] Target forecast plot saved to: {plot_path}")

    # If intermediate days were projected, save them as well for the user
    if meta["is_recursive"] and meta["intermediate_forecasts"]:
        print(f"\n[INFO] Saving {len(meta['intermediate_forecasts'])} intermediate forecast(s) generated during rolling:")
        for inter_d_str, inter_p in meta["intermediate_forecasts"].items():
            inter_recs = []
            for h in range(24):
                inter_recs.append({
                    "target_date": inter_d_str,
                    "hour": f"{h:02d}:00",
                    "forecast_price_inr_mwh": round(float(inter_p[h]), 2),
                    "forecast_price_inr_kwh": round(float(inter_p[h] / 1000.0), 4),
                })
            inter_df = pd.DataFrame(inter_recs)
            inter_csv = config.PREDICTED_SAVED_MODELS_DIR / f"forecast_{inter_d_str}.csv"
            inter_df.to_csv(inter_csv, index=False)

            inter_plot = config.PREDICTED_PLOTS_DIR / f"forecast_{inter_d_str}.png"
            plot_single_day_forecast(
                pred_prices=inter_p,
                actual_prices=None,
                title=f"24-Hour Price Forecast for {inter_d_str}",
                save_path=inter_plot,
                is_recursive=True,
                rolled_from=meta["rolled_from"],
            )
            print(f"  • {inter_d_str}: CSV -> {inter_csv.name} | Plot -> {inter_plot.name}")


if __name__ == "__main__":
    main()
