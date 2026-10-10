"""Deep-learning forecaster: LSTM with direct multi-horizon output.

Input: the last `window` minutes of [standardised log1p(rps), sin(day), cos(day)].
Output: the next `horizon` minutes at once (direct strategy, no recursive error build-up).
With `quantile` set, the model is trained with pinball loss and predicts that quantile directly.
Because log1p is monotonic, a quantile in log space maps to the same quantile in RPS.

Requires the optional dependency: pip install -e ".[lstm]"
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from pas.models.base import Forecaster, clip_non_negative
from pas.timeseries import MINUTES_PER_DAY, fill_gaps, minute_of_day

try:
    import torch
    from torch import nn
except ImportError:  # pragma: no cover - exercised only without the extra installed
    torch = None
    nn = None


def _require_torch() -> None:
    if torch is None:
        raise ImportError('the LSTM model needs PyTorch: pip install -e ".[lstm]"')


if nn is not None:

    class _Net(nn.Module):
        def __init__(self, n_features: int, hidden: int, layers: int, dropout: float,
                     horizon: int) -> None:
            super().__init__()
            self.lstm = nn.LSTM(n_features, hidden, num_layers=layers, batch_first=True,
                                dropout=dropout if layers > 1 else 0.0)
            self.head = nn.Linear(hidden, horizon)

        def forward(self, x):  # x: (batch, window, features)
            out, _ = self.lstm(x)
            return self.head(out[:, -1, :])


class LstmForecaster(Forecaster):
    name = "lstm"

    def __init__(self, window: int = 60, horizon: int = 10, hidden: int = 64, layers: int = 2,
                 dropout: float = 0.2, epochs: int = 30, batch_size: int = 256,
                 lr: float = 1e-3, patience: int = 5, quantile: float | None = None,
                 seed: int = 42) -> None:
        _require_torch()
        self.window, self.horizon = window, horizon
        self.hidden, self.layers, self.dropout = hidden, layers, dropout
        self.epochs, self.batch_size, self.lr, self.patience = epochs, batch_size, lr, patience
        self.trained_quantile = quantile
        self.seed = seed
        self.mu = 0.0
        self.sd = 1.0
        self.net = None
        self.history_: list[dict[str, float]] = []
        if quantile is not None:
            self.name = f"lstm-p{round(quantile * 100)}"

    # Feature engineering ---------------------------------------------------------------
    def _features(self, y: pd.Series) -> np.ndarray:
        z = (np.log1p(fill_gaps(y).clip(lower=0).to_numpy()) - self.mu) / self.sd
        t = minute_of_day(y.index) / MINUTES_PER_DAY
        return np.stack([z, np.sin(2 * np.pi * t), np.cos(2 * np.pi * t)], axis=1)

    def _windows(self, y: pd.Series) -> tuple[np.ndarray, np.ndarray]:
        x = self._features(y).astype(np.float32)
        n = len(x) - self.window - self.horizon + 1
        if n <= 0:
            raise ValueError(f"series too short: need > {self.window + self.horizon} minutes")
        idx = np.arange(self.window)[None, :] + np.arange(n)[:, None]
        inputs = x[idx]
        tidx = np.arange(self.horizon)[None, :] + np.arange(n)[:, None] + self.window
        targets = x[tidx, 0]
        return inputs, targets

    def _loss(self, pred, target):
        if self.trained_quantile is None:
            return torch.mean((pred - target) ** 2)
        q = self.trained_quantile
        diff = target - pred
        return torch.mean(torch.maximum(q * diff, (q - 1) * diff))

    # Training --------------------------------------------------------------------------
    def fit(self, y: pd.Series, val: pd.Series | None = None) -> LstmForecaster:
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        logs = np.log1p(fill_gaps(y).clip(lower=0).to_numpy())
        self.mu, self.sd = float(logs.mean()), float(logs.std() or 1.0)

        x_tr, y_tr = self._windows(y)
        if val is not None and len(val) > self.window + self.horizon:
            x_va, y_va = self._windows(val)
        else:  # hold out the last 10% of training windows
            cut = int(len(x_tr) * 0.9)
            x_tr, x_va, y_tr, y_va = x_tr[:cut], x_tr[cut:], y_tr[:cut], y_tr[cut:]

        net = _Net(3, self.hidden, self.layers, self.dropout, self.horizon)
        opt = torch.optim.Adam(net.parameters(), lr=self.lr)
        xt, yt = torch.from_numpy(x_tr), torch.from_numpy(y_tr)
        xv, yv = torch.from_numpy(x_va), torch.from_numpy(y_va)
        gen = torch.Generator().manual_seed(self.seed)

        best, best_state, bad = float("inf"), None, 0
        self.history_ = []
        for epoch in range(self.epochs):
            net.train()
            perm = torch.randperm(len(xt), generator=gen)
            total = 0.0
            for i in range(0, len(xt), self.batch_size):
                b = perm[i:i + self.batch_size]
                opt.zero_grad()
                loss = self._loss(net(xt[b]), yt[b])
                loss.backward()
                nn.utils.clip_grad_norm_(net.parameters(), 1.0)
                opt.step()
                total += loss.item() * len(b)
            net.eval()
            with torch.no_grad():
                val_loss = float(self._loss(net(xv), yv))
            self.history_.append({"epoch": epoch + 1, "train_loss": total / len(xt),
                                  "val_loss": val_loss})
            if val_loss < best - 1e-6:
                best, best_state, bad = val_loss, copy.deepcopy(net.state_dict()), 0
            else:
                bad += 1
                if bad >= self.patience:
                    break
        if best_state is not None:
            net.load_state_dict(best_state)
        net.eval()
        self.net = net
        return self

    # Inference -------------------------------------------------------------------------
    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        if self.net is None:
            raise ValueError("model is not fitted; run `pas train lstm` first")
        if horizon > self.horizon:
            raise ValueError(f"model was trained for horizon <= {self.horizon}")
        if quantile is not None and quantile != self.trained_quantile:
            raise ValueError("an LSTM predicts only the quantile it was trained for "
                             f"({self.trained_quantile}); train another model for {quantile}")
        h = history.iloc[-self.window:]
        if len(h) < self.window:  # pad the start with the earliest value
            pad_index = pd.date_range(end=h.index[0] - pd.Timedelta(minutes=1),
                                      periods=self.window - len(h), freq="min")
            first = float(fill_gaps(h).iloc[0]) if len(h) else 0.0
            h = pd.concat([pd.Series(first, index=pad_index), h])
        x = torch.from_numpy(self._features(h).astype(np.float32))[None]
        with torch.no_grad():
            z = self.net(x)[0, :horizon].numpy()
        return clip_non_negative(np.expm1(z * self.sd + self.mu))

    def describe(self) -> dict:
        return {**super().describe(), "window": self.window, "horizon": self.horizon,
                "hidden": self.hidden, "layers": self.layers, "dropout": self.dropout,
                "epochs_run": len(self.history_), "seed": self.seed}
