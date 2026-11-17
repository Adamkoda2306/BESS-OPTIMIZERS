"""
Training Script for Deep Price Forecasting ANN Model.

Trains the deep neural network for 5000 epochs (configurable), logs metrics,
saves the best model checkpoint and training history, and generates convergence plots.
"""

import os
import sys
import time
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
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

import config
from model import build_model, DeepPriceForecastingANN
from dataset import get_dataloaders
from utils import set_seed, calculate_metrics, plot_training_history


def train_epoch(
    model: nn.Module,
    train_loader: DataLoader,
    optimizer: optim.Optimizer,
    criterion: nn.Module,
    device: str,
) -> float:
    """Train the model for one single epoch."""
    model.train()
    running_loss = 0.0
    total_batches = 0

    for features, targets, w_means, w_stds, raw_targets, _ in train_loader:
        features = features.to(device)
        targets = targets.to(device)

        optimizer.zero_grad()
        predictions = model(features)
        loss = criterion(predictions, targets)
        loss.backward()

        # Gradient clipping to ensure stable deep training
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()

        running_loss += loss.item()
        total_batches += 1

    return running_loss / max(1, total_batches)


def evaluate(
    model: nn.Module,
    val_loader: DataLoader,
    criterion: nn.Module,
    device: str,
) -> Tuple[float, dict, np.ndarray, np.ndarray]:
    """Evaluate model on validation or test dataset."""
    model.eval()
    running_loss = 0.0
    total_batches = 0

    all_preds = []
    all_actuals = []

    with torch.no_grad():
        for features, targets, w_means, w_stds, raw_targets, _ in val_loader:
            features = features.to(device)
            targets = targets.to(device)

            norm_preds = model(features)
            loss = criterion(norm_preds, targets)
            running_loss += loss.item()
            total_batches += 1

            # De-normalize back to original price scale (INR/MWh or INR/kWh)
            # pred_price = norm_pred * w_std + w_mean
            w_means_np = w_means.numpy()[:, None]
            w_stds_np = w_stds.numpy()[:, None]
            preds_np = norm_preds.cpu().numpy() * w_stds_np + w_means_np
            preds_np = np.clip(preds_np, 0.0, None)

            all_preds.append(preds_np)
            all_actuals.append(raw_targets.numpy())

    all_preds_arr = np.concatenate(all_preds, axis=0)
    all_actuals_arr = np.concatenate(all_actuals, axis=0)
    metrics = calculate_metrics(all_actuals_arr, all_preds_arr)
    avg_loss = running_loss / max(1, total_batches)

    return avg_loss, metrics, all_preds_arr, all_actuals_arr


def main():
    parser = argparse.ArgumentParser(description="Train Deep ANN for Electricity Price Forecasting")
    parser.add_argument("--epochs", type=int, default=config.EPOCHS, help="Number of training epochs (default: 5000)")
    parser.add_argument("--batch-size", type=int, default=config.BATCH_SIZE, help="Batch size (default: 128)")
    parser.add_argument("--lr", type=float, default=config.LEARNING_RATE, help="Initial learning rate (default: 1e-3)")
    parser.add_argument("--device", type=str, default=config.DEVICE, help="Device to use: cpu, mps, cuda")
    parser.add_argument("--seed", type=int, default=config.SEED, help="Random seed (default: 42)")
    parser.add_argument("--eval-freq", type=int, default=10, help="Evaluate every N epochs (default: 10)")
    parser.add_argument("--print-freq", type=int, default=50, help="Print metrics every N epochs (default: 50)")
    args = parser.parse_args()

    # Set random seeds
    set_seed(args.seed)

    print("=" * 78)
    print("           DEEP ARTIFICIAL NEURAL NETWORK (ANN) PRICE FORECASTER           ")
    print("=" * 78)
    print(f"Device: {args.device.upper()}")
    print(f"Target Epochs: {args.epochs:,}")
    print(f"Batch Size: {args.batch_size}")
    print(f"Initial Learning Rate: {args.lr}")
    print(f"Price Target Column: {config.PRICE_COLUMN}")
    print(f"Input Window: {config.PAST_DAYS} days ({config.INPUT_HOURS} hours)")
    print(f"Forecast Horizon: {config.FORECAST_DAYS} day ({config.FORECAST_HOURS} hours)")
    print(f"Hidden Dimensions: {config.HIDDEN_DIMS} ({len(config.HIDDEN_DIMS)} hidden layers)")
    print("=" * 78)

    # 1. Load Data
    print("\n[1/4] Loading and preparing continuous hourly dataset...")
    train_loader, val_loader, test_loader, in_features, pipeline = get_dataloaders(config)

    # 2. Build Model
    print("\n[2/4] Constructing Deep ANN Architecture with Residual Highway blocks...")
    model = build_model(
        in_features=in_features,
        out_features=config.FORECAST_HOURS,
        hidden_dims=config.HIDDEN_DIMS,
        dropout=config.DROPOUT,
        use_residual=config.USE_RESIDUAL,
        use_daily_highway=config.USE_DAILY_HIGHWAY,
        past_hours=config.INPUT_HOURS,
        device=args.device,
    )
    param_count = model.count_parameters()
    print(f"Model successfully built. Total trainable parameters: {param_count:,}")

    # 3. Setup Loss, Optimizer, and Scheduler
    criterion = nn.SmoothL1Loss(beta=0.1)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=config.WEIGHT_DECAY)

    if config.LR_SCHEDULER == "cosine":
        scheduler = optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=args.epochs, eta_min=config.ETA_MIN
        )
    else:
        scheduler = None

    # Checkpoint tracking
    best_val_loss = float("inf")
    best_val_accuracy = 0.0
    history = {
        "epoch": [],
        "train_loss": [],
        "val_loss": [],
        "val_accuracy_wapa": [],
        "val_accuracy_mape": [],
        "val_r2": [],
        "val_mae": [],
        "lr": [],
    }

    print("\n[3/4] Starting model training for 5000 epochs...")
    print(f"{'Epoch':>6} | {'Train Loss':>11} | {'Val Loss':>9} | {'Val Acc(1-WAPE)':>15} | {'Val Acc(1-MAPE)':>15} | {'Val R2%':>8} | {'Best Acc':>9} | {'LR':>8}")
    print("-" * 95)

    start_time = time.time()
    try:
        for epoch in range(1, args.epochs + 1):
            train_loss = train_epoch(model, train_loader, optimizer, criterion, args.device)

            if scheduler is not None:
                current_lr = scheduler.get_last_lr()[0]
                scheduler.step()
            else:
                current_lr = optimizer.param_groups[0]["lr"]

            # Periodic evaluation
            is_eval = (epoch % args.eval_freq == 0) or (epoch == 1) or (epoch == args.epochs)
            if is_eval:
                val_loss, val_metrics, _, _ = evaluate(model, val_loader, criterion, args.device)
                val_acc_wapa = val_metrics["accuracy_wapa_pct"]
                val_acc_mape = val_metrics["accuracy_mape_pct"]
                val_r2 = val_metrics["r2_score_pct"]
                val_mae = val_metrics["mae"]

                history["epoch"].append(epoch)
                history["train_loss"].append(round(train_loss, 6))
                history["val_loss"].append(round(val_loss, 6))
                history["val_accuracy_wapa"].append(val_acc_wapa)
                history["val_accuracy_mape"].append(val_acc_mape)
                history["val_r2"].append(val_r2)
                history["val_mae"].append(val_mae)
                history["lr"].append(current_lr)

                # Save best checkpoint
                # We optimize primary criterion: highest validation accuracy (or lowest val loss)
                is_best = val_acc_wapa > best_val_accuracy or (val_acc_wapa == best_val_accuracy and val_loss < best_val_loss)
                if is_best:
                    best_val_accuracy = val_acc_wapa
                    best_val_loss = val_loss
                    checkpoint = {
                        "epoch": epoch,
                        "model_state_dict": model.state_dict(),
                        "optimizer_state_dict": optimizer.state_dict(),
                        "best_val_loss": best_val_loss,
                        "best_val_accuracy_wapa": best_val_accuracy,
                        "best_val_accuracy_mape": val_acc_mape,
                        "val_metrics": val_metrics,
                        "in_features": in_features,
                        "out_features": config.FORECAST_HOURS,
                        "hidden_dims": config.HIDDEN_DIMS,
                        "dropout": config.DROPOUT,
                        "past_hours": config.INPUT_HOURS,
                        "price_column": config.PRICE_COLUMN,
                        "use_feature_engineering": config.USE_FEATURE_ENGINEERING,
                    }
                    torch.save(checkpoint, config.BEST_MODEL_PATH)

                # Printing progress
                if (epoch % args.print_freq == 0) or (epoch == 1) or (epoch == args.epochs) or (is_best and epoch % 10 == 0):
                    star = " *" if is_best else "  "
                    print(
                        f"{epoch:6d} | {train_loss:11.6f} | {val_loss:9.6f} | {val_acc_wapa:14.2f}% | {val_acc_mape:14.2f}% | {val_r2:7.2f}% | {best_val_accuracy:8.2f}%{star}| {current_lr:.1e}"
                    )

    except KeyboardInterrupt:
        print("\n\n[INFO] Training interrupted by user. Saving artifacts and best weights...")

    elapsed = time.time() - start_time
    print("-" * 95)
    print(f"Training completed in {elapsed / 60.0:.2f} minutes.")
    print(f"Best Validation Accuracy achieved: {best_val_accuracy:.2f}% (Model saved to {config.BEST_MODEL_PATH.name})")

    # 4. Save history and generate convergence plot
    print("\n[4/4] Saving training history and generating convergence plots...")
    with open(config.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    plot_path = config.PLOTS_DIR / "training_history.png"
    plot_training_history(history, plot_path)
    print(f"Training history saved: {config.HISTORY_PATH}")
    print(f"Training curve plot saved: {plot_path}")
    print("=" * 78)


if __name__ == "__main__":
    from typing import Tuple
    main()
