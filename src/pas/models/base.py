"""The interface every forecaster implements, plus save/load."""

from __future__ import annotations

import abc
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pas import __version__
from pas.io import write_text


class Forecaster(abc.ABC):
    """Predicts requests per second for the next `horizon` minutes.

    Subclasses must be picklable. `fit` receives a training series (see `pas.timeseries`);
    `predict` receives the full history available at forecast time and must only use it.
    """

    name: str = "base"
    #: quantile the model was trained for (None = mean forecast)
    trained_quantile: float | None = None

    def fit(self, y: pd.Series) -> Forecaster:
        return self

    @abc.abstractmethod
    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        """Return an array of `horizon` non-negative forecasts."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "trained_quantile": self.trained_quantile}

    # Persistence -----------------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump(self, fh)
        meta = {**self.describe(), "pas_version": __version__}
        write_text(path.with_suffix(".json"), json.dumps(meta, indent=2, default=str))
        return path


def load_model(path: str | Path) -> Forecaster:
    """Load a model saved by `Forecaster.save`. Only load files you produced yourself."""
    with Path(path).open("rb") as fh:
        model = pickle.load(fh)  # noqa: S301 - artifacts are built by this project
    if not isinstance(model, Forecaster):
        raise TypeError(f"{path} does not contain a pas Forecaster")
    return model


def clip_non_negative(values: np.ndarray) -> np.ndarray:
    return np.clip(np.nan_to_num(np.asarray(values, dtype=float), nan=0.0), 0.0, None)
