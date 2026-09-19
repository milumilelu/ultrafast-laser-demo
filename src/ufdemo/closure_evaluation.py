"""Leakage-safe scalar baselines for P1-06.

The evaluator compares closures through the same recursive solver and the same
observation metric.  It reports model predictions and errors but does not
silently fit a neural model to terminal labels.  Grouped out-of-fold results
are used whenever a fitted constant closure is requested.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .closure_training import (
    ScalarObservation,
    ScalarObservationTable,
    ScalarPrediction,
    fit_fold_aware,
    predict_scalar,
)
from .closures import EfficiencyClosure


@dataclass(frozen=True)
class ModelEvaluation:
    model: str
    predictions: tuple[ScalarPrediction, ...]
    metadata: Mapping[str, Any]

    @property
    def mae(self) -> float:
        return float(np.mean([abs(p.residual) for p in self.predictions])) if self.predictions else float("nan")

    @property
    def rmse(self) -> float:
        return math.sqrt(float(np.mean([p.residual * p.residual for p in self.predictions]))) if self.predictions else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "n": len(self.predictions),
            "mae": self.mae,
            "rmse": self.rmse,
            "metadata": dict(self.metadata),
            "predictions": [
                {
                    "condition_key": p.condition_key,
                    "target": p.target,
                    "prediction": p.prediction,
                    "residual": p.residual,
                    "metric": p.metric,
                }
                for p in self.predictions
            ],
        }


def _table(observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]]) -> ScalarObservationTable:
    return observations if isinstance(observations, ScalarObservationTable) else ScalarObservationTable.from_rows(observations)


def evaluate_mechanistic(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
) -> ModelEvaluation:
    """M0: recursive solver with no learned closure."""

    table = _table(observations)
    predictions = tuple(
        ScalarPrediction(
            condition_key=row.condition_key,
            target=float(row.target),
            prediction=(pred := predict_scalar(row, 1.0)),
            residual=float(pred - row.target),
            metric=row.metric,
        )
        for row in table
    )
    return ModelEvaluation("M0_mechanistic", predictions, {"closure": None, "labels_fabricated": False})


def evaluate_closure(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
    closure: EfficiencyClosure,
    *,
    model_name: str = "M4_event_closure",
    training_metadata: Mapping[str, Any] | None = None,
) -> ModelEvaluation:
    """Evaluate a pre-trained event closure on terminal observations.

    The caller supplies the split/training provenance.  This function never
    fits the closure using the rows it evaluates.
    """

    table = _table(observations)
    from .solver import solve

    predictions: list[ScalarPrediction] = []
    for row in table:
        result = solve(row.config, row.material, efficiency_closure=closure)
        if not result.ok:
            raise RuntimeError(f"closure evaluation failed for {row.condition_key}: {result.errors}")
        value = result.statistics.get(row.metric)
        if value is None or not math.isfinite(float(value)):
            raise ValueError(f"metric {row.metric!r} unavailable for {row.condition_key}")
        predictions.append(
            ScalarPrediction(
                condition_key=row.condition_key,
                target=float(row.target),
                prediction=float(value),
                residual=float(value - row.target),
                metric=row.metric,
            )
        )
    meta = {"closure": closure.metadata(), "labels_fabricated": False, "evaluation_rows_used_for_fit": False}
    if training_metadata:
        meta.update(dict(training_metadata))
    return ModelEvaluation(model_name, tuple(predictions), meta)


def evaluate_folded_baselines(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
    *,
    n_splits: int = 5,
    seed: int = 0,
    bounds: tuple[float, float] = (0.1, 10.0),
    n_grid: int = 9,
    refinements: int = 3,
) -> tuple[ModelEvaluation, ModelEvaluation]:
    """Return leakage-safe M0 and M1 OOF evaluations on identical folds."""

    table = _table(observations)
    m0 = evaluate_mechanistic(table)
    fitted = fit_fold_aware(
        table,
        n_splits=n_splits,
        seed=seed,
        bounds=bounds,
        n_grid=n_grid,
        refinements=refinements,
    )
    m1 = ModelEvaluation(
        "M1_constant_recursive_closure",
        fitted.oof_predictions,
        {
            **dict(fitted.metadata),
            "oof_rmse": fitted.oof_rmse,
            "oof_mse": fitted.oof_mse,
            "labels_fabricated": False,
        },
    )
    return m0, m1


__all__ = ["ModelEvaluation", "evaluate_mechanistic", "evaluate_closure", "evaluate_folded_baselines"]
