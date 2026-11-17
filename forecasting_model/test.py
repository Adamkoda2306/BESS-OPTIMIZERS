"""
Testing and Evaluation Script for Deep Price Forecasting ANN Model.

Loads the saved best model weights, evaluates on the held-out test dataset
(and optionally on specific dates like April 2022), computes accuracy metrics,
saves predictions to CSV, and generates forecast plots.
"""

import os
import sys
import json
import argparse
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
import torch.nn as nn

import config
from model import build_model, DeepPriceForecastingANN
from dataset import get_dataloaders, get_window_by_date, PriceDataPipeline
from utils import calculate_metrics, plot_forecast_comparison


def load_trained_model(
    checkpoint_path: Path,
    device: str = "cpu",
) -> Tuple[DeepPriceForecastingANN, dict]:
    """Load model architecture and weights from checkpoint."""
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.exists():
        raise FileNotFoundError(
            f"Model checkpoint not found at {checkpoint_path}. Please run train.py first."
        )

    checkpoint = torch.load(checkpoint_path, map_location=device)
    in_features = checkpoint["in_features"]
    out_features = checkpoint.get("out_features", 24)
    hidden_dims = checkpoint.get("hidden_dims", config.HIDDEN_DIMS)
    dropout = checkpoint.get("dropout", config.DROPOUT)
    past_hours = checkpoint.get("past_hours", config.INPUT_HOURS)

    model = build_model(
        in_features=in_features,
        out_features=out_features,
        hidden_dims=hidden_dims,
        dropout=dropout,
        use_residual=config.USE_RESIDUAL,
        use_daily_highway=config.USE_DAILY_HIGHWAY,
        past_hours=past_hours,
        device=device,
    )
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model, checkpoint


def evaluate_test_set(
    model: nn.Module,
    test_loader,
    device: str,
) -> Tuple[dict, np.ndarray, np.ndarray, list]:
    """Run inference across the test DataLoader and calculate metrics."""
    model.eval()
    all_preds = []
    all_actuals = []
    all_timestamps = []

    with torch.no_grad():
        for features, targets, w_means, w_stds, raw_targets, timestamps in test_loader:
            features = features.to(device)
            norm_preds = model(features)

            # Inverse instance normalization:
            # pred_price = norm_pred * w_std + w_mean
            w_means_np = w_means.numpy()[:, None]
            w_stds_np = w_stds.numpy()[:, None]
            preds_np = norm_preds.cpu().numpy() * w_stds_np + w_means_np
            preds_np = np.clip(preds_np, 0.0, None)

            all_preds.append(preds_np)
            all_actuals.append(raw_targets.numpy())
            all_timestamps.extend(timestamps)

    all_preds_arr = np.concatenate(all_preds, axis=0)
    all_actuals_arr = np.concatenate(all_actuals, axis=0)

    metrics = calculate_metrics(all_actuals_arr, all_preds_arr)
    return metrics, all_actuals_arr, all_preds_arr, all_timestamps


def evaluate_april_2022(
    model: nn.Module,
    pipeline: PriceDataPipeline,
    device: str,
    past_days: int = 6,
) -> Tuple[dict, np.ndarray, np.ndarray, list]:
    """
    Specifically evaluate the April 2022 sequence mentioned in user prompt:
      Takes 2022-04-01 to 2022-04-06 to predict 2022-04-07,
      then 2022-04-02 to 2022-04-07 to predict 2022-04-08, and so on through April.
    """
    april_days = [f"2022-04-{d:02d}" for d in range(past_days + 1, 31)]
    preds_list = []
    actuals_list = []
    dates_list = []

    with torch.no_grad():
        for target_date in april_days:
            try:
                feat_vec, actual_prices, w_mean, w_std, dt, _ = get_window_by_date(
                    target_date, pipeline, past_days=past_days
                )
                feat_tensor = torch.from_numpy(feat_vec).float().unsqueeze(0).to(device)
                norm_pred = model(feat_tensor).cpu().numpy()[0]
                pred_price = norm_pred * w_std + w_mean
                pred_price = np.clip(pred_price, 0.0, None)

                preds_list.append(pred_price)
                actuals_list.append(actual_prices)
                dates_list.append(target_date)
            except Exception as e:
                # If date exceeds month boundary
                continue

    if not preds_list:
        return {}, np.array([]), np.array([]), []

    preds_arr = np.array(preds_list)
    actuals_arr = np.array(actuals_list)
    metrics = calculate_metrics(actuals_arr, preds_arr)
    return metrics, actuals_arr, preds_arr, dates_list


def print_metrics_table(metrics: dict, title: str = "TEST EVALUATION RESULTS"):
    """Format and print a clean metrics summary table."""
    print("\n" + "=" * 70)
    print(f"               {title.upper()}               ")
    print("=" * 70)
    print(f"  Primary Accuracy (1 - WAPE):       {metrics.get('accuracy_wapa_pct', 0.0):6.2f} %")
    print(f"  Mean Absolute Accuracy (1 - MAPE): {metrics.get('accuracy_mape_pct', 0.0):6.2f} %")
    print(f"  Normalized RMSE Accuracy:          {metrics.get('accuracy_nrmse_pct', 0.0):6.2f} %")
    print(f"  R-squared (R2 Score):              {metrics.get('r2_score_pct', 0.0):6.2f} %")
    print(f"  Directional Trend Accuracy:        {metrics.get('directional_accuracy_pct', 0.0):6.2f} %")
    print("-" * 70)
    print("  Tolerance-based Accuracies:")
    print(f"    Predictions within ±10% error:   {metrics.get('tolerance_within_10pct', 0.0):6.2f} %")
    print(f"    Predictions within ±15% error:   {metrics.get('tolerance_within_15pct', 0.0):6.2f} %")
    print(f"    Predictions within ±20% error:   {metrics.get('tolerance_within_20pct', 0.0):6.2f} %")
    print("-" * 70)
    print("  Error Metrics:")
    print(f"    Mean Absolute Error (MAE):       {metrics.get('mae', 0.0):8.2f} {config.PRICE_COLUMN}")
    print(f"    Root Mean Squared Error (RMSE):  {metrics.get('rmse', 0.0):8.2f} {config.PRICE_COLUMN}")
    print(f"    Mean Absolute Percentage (MAPE): {metrics.get('mape', 0.0):8.2f} %")
    print(f"    Weighted Absolute Error (WAPE):  {metrics.get('wape', 0.0):8.2f} %")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Test and Evaluate Deep Price Forecasting ANN")
    parser.add_argument("--checkpoint", type=str, default=str(config.BEST_MODEL_PATH), help="Path to model checkpoint")
    parser.add_argument("--device", type=str, default=config.DEVICE, help="Device: cpu, mps, cuda")
    parser.add_argument("--eval-april", action="store_true", default=True, help="Evaluate April 2022 sequence")
    args = parser.parse_args()

    print("=" * 70)
    print("         DEEP ANN PRICE FORECASTING MODEL - EVALUATION           ")
    print("=" * 70)

    # 1. Load model checkpoint
    print(f"\n[1/3] Loading checkpoint: {args.checkpoint}")
    model, checkpoint = load_trained_model(args.checkpoint, device=args.device)
    print(f"Model trained for {checkpoint.get('epoch', 'N/A')} epochs.")
    print(f"Validation accuracy at checkpoint: {checkpoint.get('best_val_accuracy_wapa', 0.0):.2f}%")

    # 2. Load test data
    print("\n[2/3] Loading test split from continuous dataset...")
    _, _, test_loader, _, pipeline = get_dataloaders(config)

    # 3. Evaluate on held-out test split
    print("\n[3/3] Running evaluation on held-out test split...")
    test_metrics, test_actuals, test_preds, test_timestamps = evaluate_test_set(
        model, test_loader, args.device
    )
    print_metrics_table(test_metrics, title="Held-Out Test Set (Chronological Final Split)")

    # Save test metrics
    with open(config.TEST_METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(test_metrics, f, indent=2)
    print(f"Test metrics saved to: {config.TEST_METRICS_PATH}")

    # Save predictions to CSV
    records = []
    for i, ts in enumerate(test_timestamps):
        date_prefix = ts.split()[0]
        for hour in range(24):
            records.append({
                "forecast_date": date_prefix,
                "hour": hour,
                "timestamp": f"{date_prefix} {hour:02d}:00:00",
                "actual_price": float(test_actuals[i, hour]),
                "predicted_price": float(test_preds[i, hour]),
                "absolute_error": float(abs(test_preds[i, hour] - test_actuals[i, hour])),
                "percentage_error": float(abs(test_preds[i, hour] - test_actuals[i, hour]) / (test_actuals[i, hour] + 1e-4) * 100.0),
            })
    pred_df = pd.DataFrame(records)
    pred_df.to_csv(config.TEST_PREDICTIONS_PATH, index=False)
    print(f"Saved {len(pred_df):,} hourly test predictions to: {config.TEST_PREDICTIONS_PATH}")

    # Plot sample comparisons
    sample_titles = [f"Forecast Date: {ts.split()[0]}" for ts in test_timestamps[:4]]
    plot_path = config.PLOTS_DIR / "test_forecast_comparison.png"
    plot_forecast_comparison(
        test_actuals[:4],
        test_preds[:4],
        sample_titles,
        plot_path,
        price_unit="INR/MWh",
    )
    print(f"Comparison plot saved to: {plot_path}")

    # Optional: Evaluate April 2022 sequence
    if args.eval_april:
        apr_metrics, apr_acts, apr_preds, apr_dates = evaluate_april_2022(
            model, pipeline, args.device, past_days=config.PAST_DAYS
        )
        if apr_metrics:
            print_metrics_table(apr_metrics, title="April 2022 Day-Ahead Benchmark Sequence")
            apr_plot_path = config.PLOTS_DIR / "april_2022_forecast_comparison.png"
            apr_titles = [f"April 2022 Benchmark: {d}" for d in apr_dates[:4]]
            plot_forecast_comparison(
                apr_acts[:4],
                apr_preds[:4],
                apr_titles,
                apr_plot_path,
                price_unit="INR/MWh",
            )
            print(f"April 2022 benchmark plot saved to: {apr_plot_path}")


if __name__ == "__main__":
    from typing import Tuple
    main()
