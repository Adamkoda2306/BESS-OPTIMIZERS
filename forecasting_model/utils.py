"""
Utility functions for metrics calculation, plotting, and reproducibility.
"""

import os
import json
import random
from pathlib import Path
from typing import Dict, Any, List, Optional
import numpy as np
import torch

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def set_seed(seed: int = 42) -> None:
    """Set random seeds for reproducibility."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def calculate_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, float]:
    """
    Compute comprehensive accuracy and error metrics for price forecasting.

    Args:
        y_true: Ground truth actual prices (shape: N, 24 or flattened)
        y_pred: Forecasted prices (shape: N, 24 or flattened)

    Returns:
        Dictionary of accuracy percentages and error values.
    """
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)

    # Avoid negative price predictions for safety
    y_pred = np.clip(y_pred, 0, None)

    # Core error values
    errors = y_pred - y_true
    abs_errors = np.abs(errors)
    mean_actual = np.mean(y_true)

    mae = float(np.mean(abs_errors))
    mse = float(np.mean(errors ** 2))
    rmse = float(np.sqrt(mse))

    # Avoid division by zero in MAPE
    denom = np.where(y_true == 0, 1e-4, y_true)
    rel_errors = abs_errors / denom
    mape = float(np.mean(rel_errors) * 100.0)

    # Weighted Absolute Percentage Error (WAPE)
    sum_actual = np.sum(np.abs(y_true))
    wape = float((np.sum(abs_errors) / (sum_actual + 1e-6)) * 100.0)

    # 1. Forecasting Accuracy based on WAPE (WAPA = 100 - WAPE)
    accuracy_wapa = float(max(0.0, 100.0 - wape))

    # 2. Forecasting Accuracy based on MAPE (100 - MAPE)
    accuracy_mape = float(max(0.0, 100.0 - mape))

    # 3. Normalized Root Mean Squared Accuracy
    accuracy_nrmse = float(max(0.0, (1.0 - (rmse / (mean_actual + 1e-6))) * 100.0))

    # 4. R-squared (Coefficient of Determination)
    ss_res = np.sum(errors ** 2)
    ss_tot = np.sum((y_true - mean_actual) ** 2)
    r2 = float(1.0 - (ss_res / (ss_tot + 1e-6))) * 100.0

    # 5. Tolerance Accuracies (Percentage of hours predicted within ±X% error)
    tol_10 = float(np.mean(rel_errors <= 0.10) * 100.0)
    tol_15 = float(np.mean(rel_errors <= 0.15) * 100.0)
    tol_20 = float(np.mean(rel_errors <= 0.20) * 100.0)

    # 6. Directional Accuracy (hourly trend agreement)
    if y_true.ndim == 2 and y_true.shape[1] > 1:
        true_diff = np.diff(y_true, axis=1)
        pred_diff = np.diff(y_pred, axis=1)
        dir_matches = np.sign(true_diff) == np.sign(pred_diff)
        directional_acc = float(np.mean(dir_matches) * 100.0)
    else:
        directional_acc = 0.0

    return {
        "accuracy_wapa_pct": round(accuracy_wapa, 2),
        "accuracy_mape_pct": round(accuracy_mape, 2),
        "accuracy_nrmse_pct": round(accuracy_nrmse, 2),
        "r2_score_pct": round(r2, 2),
        "tolerance_within_10pct": round(tol_10, 2),
        "tolerance_within_15pct": round(tol_15, 2),
        "tolerance_within_20pct": round(tol_20, 2),
        "directional_accuracy_pct": round(directional_acc, 2),
        "mae": round(mae, 2),
        "rmse": round(rmse, 2),
        "mape": round(mape, 2),
        "wape": round(wape, 2),
    }


def plot_training_history(history: Dict[str, List[float]], save_path: Path) -> None:
    """Plot and save training/validation loss and accuracy curves."""
    epochs = list(range(1, len(history.get("train_loss", [])) + 1))
    if not epochs:
        return

    plt.figure(figsize=(14, 5))

    # Loss plot
    plt.subplot(1, 2, 1)
    plt.plot(epochs, history.get("train_loss", []), label="Train Loss", color="#1f77b4", linewidth=1.5)
    if "val_loss" in history and history["val_loss"]:
        plt.plot(epochs, history["val_loss"], label="Val Loss", color="#ff7f0e", linewidth=1.5)
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.title("Training & Validation Loss")
    plt.legend()
    plt.grid(True, alpha=0.3)

    # Accuracy plot
    plt.subplot(1, 2, 2)
    if "val_accuracy_wapa" in history and history["val_accuracy_wapa"]:
        plt.plot(epochs, history["val_accuracy_wapa"], label="Val Accuracy (1 - WAPE) %", color="#2ca02c", linewidth=1.5)
    if "val_accuracy_mape" in history and history["val_accuracy_mape"]:
        plt.plot(epochs, history["val_accuracy_mape"], label="Val Accuracy (1 - MAPE) %", color="#9467bd", linewidth=1.5)
    plt.axhline(91.0, color="red", linestyle="--", alpha=0.8, label="91% Target Accuracy")
    plt.xlabel("Epoch")
    plt.ylabel("Accuracy (%)")
    plt.title("Validation Accuracy Over Epochs")
    plt.legend()
    plt.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=200)
    plt.close()


def plot_forecast_comparison(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    day_titles: List[str],
    save_path: Path,
    price_unit: str = "INR/MWh",
) -> None:
    """
    Plot actual vs predicted 24-hour price curves for multiple test sample days.
    """
    n_samples = min(len(y_true), len(day_titles), 4)
    if n_samples == 0:
        return

    fig, axes = plt.subplots(n_samples, 1, figsize=(13, 3.2 * n_samples), sharex=True)
    if n_samples == 1:
        axes = [axes]

    hours = list(range(24))

    for idx in range(n_samples):
        ax = axes[idx]
        actual = y_true[idx]
        pred = y_pred[idx]
        title = day_titles[idx]

        # Calculate sample accuracy
        err = np.abs(actual - pred)
        sample_wapa = max(0.0, (1.0 - np.sum(err) / (np.sum(actual) + 1e-6)) * 100.0)

        ax.plot(hours, actual, marker="o", markersize=4, color="#1f77b4", linewidth=2.0, label="Actual Price")
        ax.plot(hours, pred, marker="s", markersize=4, color="#d62728", linestyle="--", linewidth=2.0, label="Forecasted Price")
        ax.fill_between(hours, actual, pred, color="#d62728", alpha=0.15, label="Prediction Gap")

        ax.set_title(f"{title} | Accuracy: {sample_wapa:.2f}%", fontsize=11, fontweight="bold")
        ax.set_ylabel(f"Price ({price_unit})", fontsize=10)
        ax.set_xticks(hours)
        ax.set_xticklabels([f"{h:02d}:00" for h in hours], rotation=45, fontsize=8)
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(loc="upper right", framealpha=0.9)

    plt.xlabel("Hour of Day", fontsize=10)
    plt.tight_layout()
    plt.savefig(save_path, dpi=200)
    plt.close()
