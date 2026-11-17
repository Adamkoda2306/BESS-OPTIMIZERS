"""
Configuration file for Price Forecasting ANN Model.
Defines dataset paths, window parameters, model architecture, and training hyperparameters.
"""

import os
from pathlib import Path
import torch

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR.parent / "datasets" / "converted"
SAVED_MODELS_DIR = BASE_DIR / "saved_models"
PLOTS_DIR = SAVED_MODELS_DIR / "plots"
PREDICTED_SAVED_MODELS_DIR = SAVED_MODELS_DIR / "predicted" 
PREDICTED_PLOTS_DIR = PREDICTED_SAVED_MODELS_DIR / "plots"

# Ensure directories exist
SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Data & Window Configuration
# ---------------------------------------------------------------------------
# Price column to predict: 'price_inr_mwh' or 'price_inr_kwh'
PRICE_COLUMN = "price_inr_mwh"

# Window settings:
# Previous 1 week input (default 7 days = 168 hours; or set to 6 days = 144 hours as in the 04-01 to 04-06 example)
PAST_DAYS = 7
INPUT_HOURS = PAST_DAYS * 24        # 168 hours (7 days) or 144 hours (6 days)

# Forecast target: Next 1 day (24 hourly prices)
FORECAST_DAYS = 1
FORECAST_HOURS = FORECAST_DAYS * 24  # 24 hours

# Stride settings:
# For training: step_hours=1 creates dense sliding windows (over 20,000 samples for high generalization)
TRAIN_STEP_HOURS = 1
# For testing: step_hours=24 slides day-by-day (Day 1..7 -> Day 8, Day 2..8 -> Day 9)
TEST_STEP_HOURS = 24

# Chronological split ratios: 80% Train, 10% Validation, 10% Test
SPLIT_RATIOS = (0.80, 0.10, 0.10)

# Feature engineering options
USE_FEATURE_ENGINEERING = True

# ---------------------------------------------------------------------------
# Neural Network Architecture Configuration
# ---------------------------------------------------------------------------
# Deep Artificial Neural Network with multiple hidden layers and residual connections
# 8 deep hidden layers providing high representational capacity
HIDDEN_DIMS = [1024, 1024, 512, 512, 256, 256, 128, 128]
DROPOUT = 0.05
ACTIVATION = "gelu"                 # 'gelu', 'leaky_relu', or 'relu'
USE_RESIDUAL = True                 # Skip connections between blocks
USE_DAILY_HIGHWAY = True            # Direct linear highway from previous day (ensures fast convergence & stability)

# ---------------------------------------------------------------------------
# Training Hyperparameters
# ---------------------------------------------------------------------------
# Train for 5000 epochs as requested
EPOCHS = 5000
BATCH_SIZE = 128
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-5
LOSS_FUNCTION = "smooth_l1"          # 'smooth_l1', 'huber', 'l1', or 'mse'
LR_SCHEDULER = "cosine"              # 'cosine', 'plateau', or 'none'
ETA_MIN = 1e-6                       # Minimum learning rate for cosine scheduler

# Early stopping (set EARLY_STOPPING=False to train for all 5000 epochs,
# but the best model based on validation accuracy/loss is always saved)
EARLY_STOPPING = False
PATIENCE = 500

# Random seed for reproducibility
SEED = 42

# Device selection: Auto-detects CUDA / MPS / CPU
if torch.cuda.is_available():
    DEVICE = "cuda"
elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

# ---------------------------------------------------------------------------
# Model Checkpoint & Artifact Paths
# ---------------------------------------------------------------------------
BEST_MODEL_PATH = SAVED_MODELS_DIR / "best_forecasting_ann.pth"
SCALER_PATH = SAVED_MODELS_DIR / "scaler_metadata.json"
HISTORY_PATH = SAVED_MODELS_DIR / "training_history.json"
TEST_METRICS_PATH = SAVED_MODELS_DIR / "test_metrics.json"
TEST_PREDICTIONS_PATH = SAVED_MODELS_DIR / "test_predictions.csv"
