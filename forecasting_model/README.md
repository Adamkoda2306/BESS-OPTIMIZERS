# Deep Artificial Neural Network (ANN) Electricity Price Forecasting

This folder contains a production-grade PyTorch deep learning framework for day-ahead hourly electricity price forecasting. The model ingests **1 week of historical hourly prices** (168 hours or 144 hours) to forecast the **next 24 hours of hourly prices** across the Indian Energy Exchange (IEX Day-Ahead Market) datasets.

---

## 📌 Architecture Overview

This architecture implements a **Deep Residual Highway Artificial Neural Network (ANN)**:

```
[Input Window: 168 Hours / 144 Hours + Cyclical Features] (dim = 223)
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
   [Direct Recency Highway]      [Input Projection] -> 1024
   (Linear 24 -> 24)                     │
   (Direct day-ahead baseline)           ▼
               │              [Residual Block 1: 1024 -> 1024]
               │                         │
               │              [Residual Block 2: 1024 -> 512]
               │                         │
               │              [Residual Block 3: 512 -> 512]
               │                         │
               │              [Residual Block 4: 512 -> 256]
               │                         │
               │              [Residual Block 5: 256 -> 256]
               │                         │
               │              [Residual Block 6: 256 -> 128]
               │                         │
               │              [Residual Block 7: 128 -> 128]
               │                         │
               │              [Prediction Head: 128 -> 64 -> 24]
               │                         │
               └────────────┬────────────┘
                            ▼
           [Predicted 24-Hour Price Curve]
```


---

## 📁 File Structure

```
forecasting_model/
├── config.py             # Hyperparameters, window sizes, dataset paths, training settings
├── dataset.py            # DataLoader, sliding window generation (1-week -> 24h), feature engineering
├── model.py              # DeepResidualForecastANN PyTorch architecture
├── train.py              # Full 5000-epoch training loop, checkpointing, CosineAnnealingLR
├── test.py               # Comprehensive evaluation suite, benchmark metrics, CSV export
├── predict.py            # CLI inference tool for custom dates, recent data, or external CSVs
├── utils.py              # Accuracy metrics (WAPA, MAPE, R2, NRMSE), plotting tools, seed setup
├── __init__.py           # Package initializer
└── saved_models/         # Checkpoints, scaler metadata, plots, prediction results
    ├── best_forecasting_ann.pth
    ├── scaler_metadata.json
    ├── training_history.json
    ├── test_metrics.json
    ├── test_predictions.csv
    └── plots/
        ├── training_history.png
        └── test_forecast_comparison.png
```

---

## 🚀 Quickstart & Usage

### 1. Training the Model (5000 Epochs)

To start training the deep neural network for 5000 epochs:

```bash
# Run from repository root or inside forecasting_model
python forecasting_model/train.py --epochs 5000 --batch-size 128
```

Key training flags:
- `--epochs 5000`: Number of training iterations (default: 5000).
- `--batch-size 128`: Batch size for stochastic gradient descent.
- `--lr 0.001`: Initial learning rate, decayed with Cosine Annealing.
- `--device cpu`: Device (`cpu`, `mps`, or `cuda`).
- `--eval-freq 10`: Evaluate validation accuracy every 10 epochs.
- `--print-freq 50`: Log progress to console every 50 epochs.

*Note: The best model weights are automatically saved to `saved_models/best_forecasting_ann.pth` whenever validation accuracy reaches a new high.*

---

### 2. Evaluating on Test Split & Benchmarks

To evaluate the saved best model on held-out test data and run the April 2022 day-by-day benchmark:

```bash
python forecasting_model/test.py
```

Outputs computed:
- **Forecasting Accuracy (1 - WAPE) %**
- **Forecasting Accuracy (1 - MAPE) %**
- **Normalized RMSE Accuracy %**
- **R² Score (Coefficient of Determination) %**
- **Tolerance Accuracies (% within ±10%, ±15%, ±20% error)**
- **Directional Trend Accuracy %**
- Saves all 24-hour actual vs predicted values to `saved_models/test_predictions.csv`.
- Generates high-resolution actual vs forecast comparison plots in `saved_models/plots/`.

---

### 3. Forecasting Tomorrow / Specific Dates

#### Predict a specific historical day (e.g. April 7, 2022 using April 1-6 data):
```bash
python forecasting_model/predict.py --date 2022-04-07
```

#### Forecast the very next day beyond the dataset:
```bash
python forecasting_model/predict.py --latest
```

Outputs:
- A clean CLI table showing hourly forecasts in both **INR/MWh** and **INR/kWh**.
- Saved CSV table in `saved_models/forecast_<date>.csv`.
- High-resolution forecast curve plot in `saved_models/plots/forecast_<date>.png`.

---

## ⚙️ Configuration Reference (`config.py`)

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `PRICE_COLUMN` | `"price_inr_mwh"` | Target column (`price_inr_mwh` or `price_inr_kwh`) |
| `PAST_DAYS` | `7` | Past input window in days (7 days = 168h; 6 days = 144h) |
| `INPUT_HOURS` | `168` | Total hourly lookback window |
| `FORECAST_DAYS` | `1` | Future prediction window (1 day) |
| `FORECAST_HOURS` | `24` | Total hourly forecast output |
| `TRAIN_STEP_HOURS` | `1` | Hourly sliding stride for dense training (20,000+ samples) |
| `TEST_STEP_HOURS` | `24` | Daily stride for day-by-day evaluation |
| `HIDDEN_DIMS` | `[1024, 1024, 512, 512, 256, 256, 128, 128]` | 8-layer deep hidden dimensions |
| `EPOCHS` | `5000` | Full training epoch count |
| `ACTIVATION` | `"gelu"` | Hidden activation function |
| `LR_SCHEDULER` | `"cosine"` | CosineAnnealingLR with minimum lr `1e-6` |
