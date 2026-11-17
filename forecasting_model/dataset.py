"""
Dataset loader and sliding-window preprocessor for Electricity Price Forecasting.

Loads all continuous monthly hourly datasets from datasets/converted/,
generates historical 1-week input windows (168h / 144h) to predict the next 24h,
and builds PyTorch DataLoaders with feature engineering.
"""

import os
import sys
import glob
import json
from pathlib import Path

# Add current and parent directory to sys.path for direct script execution
CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from typing import Tuple, List, Dict, Optional, Any
import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader


class HourlyPriceDataset(Dataset):
    """
    PyTorch Dataset for hourly price forecasting.
    Yields:
      features: Tensor of engineered features (shape: in_features)
      targets: Tensor of normalized target 24h prices (shape: 24)
      w_mean: Scalar float, window mean price for inverse transformation
      w_std: Scalar float, window std price for inverse transformation
      raw_target: Tensor of actual target 24h prices (shape: 24)
    """

    def __init__(
        self,
        features: np.ndarray,
        targets: np.ndarray,
        w_means: np.ndarray,
        w_stds: np.ndarray,
        raw_targets: np.ndarray,
        target_timestamps: List[str],
    ):
        self.features = torch.from_numpy(features).float()
        self.targets = torch.from_numpy(targets).float()
        self.w_means = torch.from_numpy(w_means).float()
        self.w_stds = torch.from_numpy(w_stds).float()
        self.raw_targets = torch.from_numpy(raw_targets).float()
        self.target_timestamps = target_timestamps

    def __len__(self) -> int:
        return len(self.features)

    def __getitem__(self, idx: int):
        return (
            self.features[idx],
            self.targets[idx],
            self.w_means[idx],
            self.w_stds[idx],
            self.raw_targets[idx],
            self.target_timestamps[idx],
        )


class PriceDataPipeline:
    """
    Manages loading, cleaning, feature engineering, windowing,
    and train/val/test splitting for price time series data.
    """

    def __init__(
        self,
        data_dir: Path,
        price_column: str = "price_inr_mwh",
        past_days: int = 7,
        forecast_days: int = 1,
        use_feature_engineering: bool = True,
    ):
        self.data_dir = Path(data_dir)
        self.price_column = price_column
        self.past_days = past_days
        self.input_hours = past_days * 24
        self.forecast_hours = forecast_days * 24
        self.use_feature_engineering = use_feature_engineering

        self.df: Optional[pd.DataFrame] = None
        self.global_stats: Dict[str, float] = {}

    def load_raw_data(self) -> pd.DataFrame:
        """Load and concatenate all hourly CSV files chronologically."""
        csv_files = sorted(glob.glob(str(self.data_dir / "*_hourly.csv")))
        if not csv_files:
            raise FileNotFoundError(f"No *_hourly.csv files found in {self.data_dir}")

        dfs = []
        for file_path in csv_files:
            sub_df = pd.read_csv(file_path)
            dfs.append(sub_df)

        combined_df = pd.concat(dfs, ignore_index=True)

        # Parse timestamp column
        time_col = None
        for col in ["timestamp_local_IN", "timestamp", "datetime", "date"]:
            if col in combined_df.columns:
                time_col = col
                break

        if time_col is None:
            raise KeyError(f"Could not find a timestamp column in {csv_files[0]}")

        combined_df["timestamp"] = pd.to_datetime(combined_df[time_col])
        combined_df = combined_df.sort_values("timestamp").reset_index(drop=True)

        # Check price column
        if self.price_column not in combined_df.columns:
            fallback = "price_inr_kwh" if self.price_column == "price_inr_mwh" else "price_inr_mwh"
            if fallback in combined_df.columns:
                print(f"Warning: '{self.price_column}' not found, falling back to '{fallback}'")
                self.price_column = fallback
            else:
                raise KeyError(f"Price column '{self.price_column}' not found in dataset")

        # Forward fill or interpolate any potential missing prices
        combined_df[self.price_column] = combined_df[self.price_column].interpolate(method="linear")

        self.df = combined_df
        self.global_stats = {
            "price_min": float(combined_df[self.price_column].min()),
            "price_max": float(combined_df[self.price_column].max()),
            "price_mean": float(combined_df[self.price_column].mean()),
            "price_std": float(combined_df[self.price_column].std()),
            "total_hours": len(combined_df),
            "start_time": str(combined_df["timestamp"].min()),
            "end_time": str(combined_df["timestamp"].max()),
        }
        return combined_df

    def create_sliding_windows(
        self,
        step_hours: int = 1,
        start_idx: int = 0,
        end_idx: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, List[str]]:
        """
        Create sliding window features and targets.
        Args:
            step_hours: Stride in hours (1 for dense training, 24 for daily steps)
            start_idx: Starting row index in the combined DataFrame
            end_idx: Ending row index in the combined DataFrame
        """
        if self.df is None:
            self.load_raw_data()

        sub_df = self.df.iloc[start_idx:end_idx].reset_index(drop=True)
        prices = sub_df[self.price_column].values.astype(np.float32)
        timestamps = sub_df["timestamp"]
        dows = timestamps.dt.dayofweek.values.astype(np.float32)
        months = timestamps.dt.month.values.astype(np.float32)

        total_len = len(prices)
        feature_list = []
        norm_target_list = []
        raw_target_list = []
        w_means = []
        w_stds = []
        target_timestamps = []

        max_start = total_len - self.input_hours - self.forecast_hours + 1
        for i in range(0, max_start, step_hours):
            past_prices = prices[i : i + self.input_hours]
            future_prices = prices[i + self.input_hours : i + self.input_hours + self.forecast_hours]
            target_ts_str = str(timestamps.iloc[i + self.input_hours])

            # Window statistics (Instance Normalization)
            w_mean = float(past_prices.mean())
            w_std = float(past_prices.std() + 1e-4)

            # Normalized past prices
            norm_past = (past_prices - w_mean) / w_std

            if self.use_feature_engineering:
                # 1. Diurnal 24-hour profile averaged over past days
                daily_profile = past_prices.reshape(-1, 24).mean(axis=0)
                norm_daily_profile = (daily_profile - daily_profile.mean()) / (daily_profile.std() + 1e-4)

                # 2. Yesterday's 24 hours
                yesterday = past_prices[-24:]
                norm_yesterday = (yesterday - yesterday.mean()) / (yesterday.std() + 1e-4)

                # 3. Target day-of-week cyclical encoding (sin/cos)
                target_dow = dows[i + self.input_hours]
                dow_sin = np.sin(2.0 * np.pi * target_dow / 7.0)
                dow_cos = np.cos(2.0 * np.pi * target_dow / 7.0)

                # 4. Target month cyclical encoding (sin/cos)
                target_month = months[i + self.input_hours]
                month_sin = np.sin(2.0 * np.pi * target_month / 12.0)
                month_cos = np.cos(2.0 * np.pi * target_month / 12.0)

                # 5. Short-term momentum: yesterday mean vs weekly mean
                trend_ratio = float(yesterday.mean() / (w_mean + 1e-4))

                # 6. Global scale hints (log-transformed level and volatility)
                log_level = float(np.log1p(w_mean))
                log_volatility = float(np.log1p(w_std))

                sample_feat = np.concatenate([
                    norm_past,            # input_hours (e.g. 168 or 144)
                    norm_daily_profile,   # 24
                    norm_yesterday,       # 24
                    [dow_sin, dow_cos, month_sin, month_cos, trend_ratio, log_level, log_volatility],  # 7
                ])
            else:
                sample_feat = norm_past

            # Normalized target prices using the input window's mean and std
            norm_target = (future_prices - w_mean) / w_std

            feature_list.append(sample_feat)
            norm_target_list.append(norm_target)
            raw_target_list.append(future_prices)
            w_means.append(w_mean)
            w_stds.append(w_std)
            target_timestamps.append(target_ts_str)

        X = np.array(feature_list, dtype=np.float32)
        y_norm = np.array(norm_target_list, dtype=np.float32)
        y_raw = np.array(raw_target_list, dtype=np.float32)
        w_means_arr = np.array(w_means, dtype=np.float32)
        w_stds_arr = np.array(w_stds, dtype=np.float32)

        return X, y_norm, w_means_arr, w_stds_arr, y_raw, target_timestamps

    def save_scaler_metadata(self, save_path: Path) -> None:
        """Save dataset statistics and normalization parameters to JSON."""
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "price_column": self.price_column,
            "past_days": self.past_days,
            "input_hours": self.input_hours,
            "forecast_hours": self.forecast_hours,
            "use_feature_engineering": self.use_feature_engineering,
            "global_stats": self.global_stats,
        }
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)


def get_dataloaders(config) -> Tuple[DataLoader, DataLoader, DataLoader, int, PriceDataPipeline]:
    """
    Build and return Train, Validation, and Test DataLoaders.
    """
    pipeline = PriceDataPipeline(
        data_dir=config.DATASET_DIR,
        price_column=config.PRICE_COLUMN,
        past_days=config.PAST_DAYS,
        forecast_days=config.FORECAST_DAYS,
        use_feature_engineering=config.USE_FEATURE_ENGINEERING,
    )
    df = pipeline.load_raw_data()
    total_len = len(df)
    total_days = total_len // 24

    # Day-aligned chronological partition to ensure every window starts cleanly at 00:00:00
    train_ratio, val_ratio, _ = config.SPLIT_RATIOS
    train_days = int(total_days * train_ratio)
    val_days = int(total_days * val_ratio)
    test_days = total_days - train_days - val_days

    train_end = train_days * 24
    val_end = (train_days + val_days) * 24

    print(f"Loaded {total_len:,} continuous hourly price records ({total_days} total days).")
    print(f"Dataset range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(
        f"Chronological day-aligned split: Train ({train_days} days, 0..{train_end:,}h), "
        f"Val ({val_days} days, {train_end:,}..{val_end:,}h), "
        f"Test ({test_days} days, {val_end:,}..{total_len:,}h)"
    )

    # 1. Train dataset: Dense sliding windows (step=1) for deep learning training
    X_train, y_train, wm_tr, ws_tr, y_raw_tr, ts_tr = pipeline.create_sliding_windows(
        step_hours=config.TRAIN_STEP_HOURS,
        start_idx=0,
        end_idx=train_end,
    )
    train_ds = HourlyPriceDataset(X_train, y_train, wm_tr, ws_tr, y_raw_tr, ts_tr)

    # 2. Validation dataset: Step=24 (or train_step_hours)
    X_val, y_val, wm_va, ws_va, y_raw_val, ts_va = pipeline.create_sliding_windows(
        step_hours=config.TEST_STEP_HOURS,
        start_idx=train_end - pipeline.input_hours,
        end_idx=val_end,
    )
    val_ds = HourlyPriceDataset(X_val, y_val, wm_va, ws_va, y_raw_val, ts_va)

    # 3. Test dataset: Daily steps (step=24) matching Day 1..7 -> Day 8
    X_test, y_test, wm_te, ws_te, y_raw_te, ts_te = pipeline.create_sliding_windows(
        step_hours=config.TEST_STEP_HOURS,
        start_idx=val_end - pipeline.input_hours,
        end_idx=total_len,
    )
    test_ds = HourlyPriceDataset(X_test, y_test, wm_te, ws_te, y_raw_te, ts_te)

    train_loader = DataLoader(train_ds, batch_size=config.BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=config.BATCH_SIZE, shuffle=False)
    test_loader = DataLoader(test_ds, batch_size=config.BATCH_SIZE, shuffle=False)

    feature_dim = X_train.shape[1]
    print(f"Training samples: {len(train_ds):,} | Val samples: {len(val_ds):,} | Test samples: {len(test_ds):,}")
    print(f"Feature vector length: {feature_dim}")

    # Save scaler metadata
    pipeline.save_scaler_metadata(config.SCALER_PATH)

    return train_loader, val_loader, test_loader, feature_dim, pipeline


def get_window_by_date(
    date_str: str,
    pipeline: PriceDataPipeline,
    past_days: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray, float, float, pd.Timestamp, pd.DataFrame]:
    """
    Extract the historical 1-week window immediately preceding a target date.
    Example:
      If date_str = "2022-04-07" and past_days = 6:
      Extracts 2022-04-01 00:00 to 2022-04-06 23:00 to predict 2022-04-07 00:00..23:00.
    """
    if pipeline.df is None:
        pipeline.load_raw_data()

    df = pipeline.df
    target_dt = pd.to_datetime(date_str)
    # Ensure timezone match if present
    if df["timestamp"].dt.tz is not None and target_dt.tzinfo is None:
        target_dt = target_dt.tz_localize(df["timestamp"].dt.tz)

    target_date_only = target_dt.date()
    target_start = pd.Timestamp(target_date_only).tz_localize(df["timestamp"].dt.tz)

    p_days = past_days if past_days is not None else pipeline.past_days
    past_hours = p_days * 24

    # Find the target index
    target_idx = df.index[df["timestamp"] == target_start]
    if len(target_idx) == 0:
        raise ValueError(f"Date {target_start} not found in dataset")
    target_idx = target_idx[0]

    start_idx = target_idx - past_hours
    if start_idx < 0:
        raise ValueError(f"Not enough historical data before {target_start} (needs {past_hours} hours)")

    input_slice = df.iloc[start_idx:target_idx].reset_index(drop=True)
    actual_slice = df.iloc[target_idx : target_idx + 24].reset_index(drop=True)

    past_prices = input_slice[pipeline.price_column].values.astype(np.float32)
    actual_prices = actual_slice[pipeline.price_column].values.astype(np.float32)

    w_mean = float(past_prices.mean())
    w_std = float(past_prices.std() + 1e-4)
    norm_past = (past_prices - w_mean) / w_std

    if pipeline.use_feature_engineering:
        daily_profile = past_prices.reshape(-1, 24).mean(axis=0)
        norm_daily_profile = (daily_profile - daily_profile.mean()) / (daily_profile.std() + 1e-4)

        yesterday = past_prices[-24:]
        norm_yesterday = (yesterday - yesterday.mean()) / (yesterday.std() + 1e-4)

        target_dow = target_start.dayofweek
        dow_sin = np.sin(2.0 * np.pi * target_dow / 7.0)
        dow_cos = np.cos(2.0 * np.pi * target_dow / 7.0)

        target_month = target_start.month
        month_sin = np.sin(2.0 * np.pi * target_month / 12.0)
        month_cos = np.cos(2.0 * np.pi * target_month / 12.0)

        trend_ratio = float(yesterday.mean() / (w_mean + 1e-4))
        log_level = float(np.log1p(w_mean))
        log_volatility = float(np.log1p(w_std))

        feature_vector = np.concatenate([
            norm_past,
            norm_daily_profile,
            norm_yesterday,
            [dow_sin, dow_cos, month_sin, month_cos, trend_ratio, log_level, log_volatility],
        ])
    else:
        feature_vector = norm_past

    return feature_vector, actual_prices, w_mean, w_std, target_start, input_slice


if __name__ == "__main__":
    try:
        import config
    except ImportError:
        from forecasting_model import config
    tr_l, va_l, te_l, f_dim, pipe = get_dataloaders(config)
    print("Dataset verification passed successfully!")
