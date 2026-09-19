"""Fold-aware terminal-observation training for the learned efficiency closure.

This module is deliberately small and conservative.  It trains only against
scalar observations that already exist in a caller-owned table.  A row carries
the parsed :class:`~ufdemo.config.RunConfig`, a material card, a scalar target,
and a ``condition_key``.  Rows sharing a key are kept in the same fold, so a
single physical condition cannot leak from training into validation.

The solver is an event-recursive black box, therefore this first trainer does
not pretend to back-propagate through ``solve``.  It fits one bounded constant
net-efficiency factor with a deterministic log-space grid/refinement search.
That factor is passed to ``solve`` on every forward run, so the fitted value
changes the recursive surface state rather than being applied to a final depth
after the fact.  No intermediate event labels, image pixels, or synthetic
rows are manufactured here.  A small MLP remains available in
``ufdemo.closures`` for future explicit event-supervised training.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .closures import ConstantEfficiencyClosure, TorchEfficiencyMLP, FEATURE_NAMES
from .config import RunConfig
from .materials import MaterialSpec
from .solver import RunResult, solve


_METRIC_ALIASES: dict[str, str] = {
    "mean_depth": "mean_depth_internal",
    "center_depth": "center_depth_internal",
    "max_depth": "max_depth_internal",
    "removal_volume": "removal_volume_internal",
}


@dataclass(frozen=True)
class ScalarObservation:
    """One measured scalar terminal observation.

    ``target`` must already be in the same internal unit as ``metric``.  The
    trainer intentionally does not infer units, convert labels, or fill missing
    values.  Use one row per observed terminal state; repeated rows are allowed
    only when they are genuinely repeated measurements and carry their own
    condition key/weight.
    """

    condition_key: str
    config: RunConfig
    material: MaterialSpec
    target: float
    metric: str = "mean_depth_internal"
    weight: float = 1.0

    def __post_init__(self) -> None:
        key = str(self.condition_key).strip()
        if not key:
            raise ValueError("condition_key must be a non-empty string")
        if not isinstance(self.config, RunConfig):
            raise TypeError("config must be a RunConfig")
        if not isinstance(self.material, MaterialSpec):
            raise TypeError("material must be a MaterialSpec")
        target = float(self.target)
        weight = float(self.weight)
        if not math.isfinite(target):
            raise ValueError("target must be finite; missing observations are not fabricated")
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("weight must be finite and positive")
        metric = _canonical_metric(self.metric)
        object.__setattr__(self, "condition_key", key)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "metric", metric)


@dataclass(frozen=True)
class ScalarObservationTable:
    """Immutable, validated scalar table accepted by the fold trainer."""

    rows: tuple[ScalarObservation, ...]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError("observation table is empty; provide measured scalar rows")
        if any(not isinstance(row, ScalarObservation) for row in self.rows):
            raise TypeError("rows must contain only ScalarObservation instances")
        metrics = {row.metric for row in self.rows}
        if len(metrics) != 1:
            raise ValueError(
                "one scalar observation table must use one metric; split tables before fitting "
                f"(got {sorted(metrics)})"
            )

    @classmethod
    def from_rows(cls, rows: Iterable[ScalarObservation | Mapping[str, Any]]) -> "ScalarObservationTable":
        parsed: list[ScalarObservation] = []
        for index, row in enumerate(rows):
            if isinstance(row, ScalarObservation):
                parsed.append(row)
                continue
            if not isinstance(row, Mapping):
                raise TypeError(f"row {index} must be ScalarObservation or mapping")
            missing = [name for name in ("condition_key", "config", "material", "target") if name not in row]
            if missing:
                raise ValueError(f"row {index} is missing required fields: {missing}")
            parsed.append(
                ScalarObservation(
                    condition_key=str(row["condition_key"]),
                    config=row["config"],
                    material=row["material"],
                    target=row["target"],
                    metric=str(row.get("metric", "mean_depth_internal")),
                    weight=row.get("weight", 1.0),
                )
            )
        return cls(rows=tuple(parsed))

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    @property
    def condition_keys(self) -> tuple[str, ...]:
        return tuple(sorted({row.condition_key for row in self.rows}))

    @property
    def metric(self) -> str:
        return self.rows[0].metric


def _canonical_metric(metric: str) -> str:
    metric = str(metric)
    return _METRIC_ALIASES.get(metric, metric)


def _validate_metric(metric: str) -> str:
    canonical = _canonical_metric(metric)
    allowed = {
        "mean_depth_internal",
        "center_depth_internal",
        "max_depth_internal",
        "removal_volume_internal",
    }
    if canonical not in allowed:
        raise ValueError(f"unsupported scalar solver metric {metric!r}; allowed={sorted(allowed)}")
    return canonical


def _extract_metric(result: RunResult, metric: str) -> float:
    if not result.ok:
        raise RuntimeError(f"solver failed for scalar observation: status={result.status!r}, errors={result.errors!r}")
    canonical = _validate_metric(metric)
    value = result.statistics.get(canonical)
    if value is None or not math.isfinite(float(value)):
        raise ValueError(f"solver metric {canonical!r} is unavailable/non-finite; no label was fabricated")
    return float(value)


def predict_scalar(row: ScalarObservation, efficiency: float) -> float:
    """Run the recursive solver and extract one scalar terminal observation."""

    closure = ConstantEfficiencyClosure(float(efficiency))
    result = solve(row.config, row.material, efficiency_closure=closure)
    return _extract_metric(result, row.metric)


@dataclass(frozen=True)
class GroupFold:
    """One deterministic group split."""

    index: int
    train_condition_keys: tuple[str, ...]
    test_condition_keys: tuple[str, ...]


def make_group_folds(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
    *,
    n_splits: int = 5,
    seed: int = 0,
) -> tuple[GroupFold, ...]:
    """Create deterministic condition-group folds without splitting a key."""

    table = (
        observations
        if isinstance(observations, ScalarObservationTable)
        else ScalarObservationTable.from_rows(observations)
    )
    keys = list(table.condition_keys)
    if n_splits < 2:
        raise ValueError("n_splits must be at least 2 for an honest held-out fold")
    if n_splits > len(keys):
        raise ValueError(f"n_splits={n_splits} exceeds unique condition keys={len(keys)}")
    if isinstance(seed, bool) or int(seed) != seed or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    rng = np.random.default_rng(int(seed))
    shuffled = np.asarray(keys, dtype=object)
    rng.shuffle(shuffled)
    chunks = np.array_split(shuffled, n_splits)
    all_keys = set(keys)
    folds: list[GroupFold] = []
    for index, chunk in enumerate(chunks):
        test = tuple(sorted(str(k) for k in chunk.tolist()))
        train = tuple(sorted(all_keys.difference(test)))
        if not test or not train:
            raise ValueError("group split produced an empty train/test partition")
        folds.append(GroupFold(index=index, train_condition_keys=train, test_condition_keys=test))
    return tuple(folds)


@dataclass(frozen=True)
class ConstantEfficiencyFit:
    """Black-box fit result for one training fold."""

    efficiency: float
    train_mse: float
    evaluations: int
    lower_bound: float
    upper_bound: float
    history: tuple[tuple[float, float], ...] = ()


def _weighted_mse(rows: Sequence[ScalarObservation], predictions: Sequence[float]) -> float:
    if len(rows) != len(predictions) or not rows:
        raise ValueError("cannot score an empty or mismatched observation set")
    weights = np.asarray([row.weight for row in rows], dtype=float)
    residual = np.asarray(predictions, dtype=float) - np.asarray([row.target for row in rows], dtype=float)
    return float(np.sum(weights * residual * residual) / np.sum(weights))


def fit_constant_efficiency(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
    *,
    bounds: tuple[float, float] = (0.1, 10.0),
    n_grid: int = 9,
    refinements: int = 3,
) -> ConstantEfficiencyFit:
    """Fit one bounded efficiency factor with deterministic black-box search.

    Search happens in ``log(efficiency)`` so equal multiplicative changes have
    equal resolution.  Every objective evaluation calls ``solve`` afresh with
    a ``ConstantEfficiencyClosure``; no terminal prediction is rescaled after
    solving.  The routine intentionally has no default regularization or
    imputation because both would silently invent information in a tiny table.
    """

    table = (
        observations
        if isinstance(observations, ScalarObservationTable)
        else ScalarObservationTable.from_rows(observations)
    )
    rows = table.rows
    if not rows:
        raise ValueError("cannot fit an empty observation table")
    lower, upper = (float(bounds[0]), float(bounds[1]))
    if not (math.isfinite(lower) and math.isfinite(upper) and 0.0 < lower < upper):
        raise ValueError("bounds must satisfy 0 < lower < upper and be finite")
    if isinstance(n_grid, bool) or int(n_grid) != n_grid or n_grid < 3:
        raise ValueError("n_grid must be an integer >= 3")
    if isinstance(refinements, bool) or int(refinements) != refinements or refinements < 0:
        raise ValueError("refinements must be a non-negative integer")

    lo, hi = math.log(lower), math.log(upper)
    history: list[tuple[float, float]] = []
    cache: dict[float, float] = {}

    def objective(log_eta: float) -> float:
        # Rounded cache keys avoid duplicate solves from adjacent grid stages.
        key = round(float(log_eta), 14)
        if key not in cache:
            eta = float(math.exp(key))
            preds = [predict_scalar(row, eta) for row in rows]
            cache[key] = _weighted_mse(rows, preds)
            history.append((eta, cache[key]))
        return cache[key]

    for _ in range(int(refinements) + 1):
        grid = np.linspace(lo, hi, int(n_grid))
        scores = np.asarray([objective(float(x)) for x in grid])
        best_index = int(np.argmin(scores))
        left = float(grid[max(0, best_index - 1)])
        right = float(grid[min(len(grid) - 1, best_index + 1)])
        if right <= left:
            break
        lo, hi = left, right

    best_log = min(cache, key=cache.get)
    best_eta = float(math.exp(best_log))
    return ConstantEfficiencyFit(
        efficiency=best_eta,
        train_mse=float(cache[best_log]),
        evaluations=len(cache),
        lower_bound=lower,
        upper_bound=upper,
        history=tuple(history),
    )


@dataclass(frozen=True)
class ScalarPrediction:
    condition_key: str
    target: float
    prediction: float
    residual: float
    metric: str


@dataclass(frozen=True)
class FoldResult:
    fold: GroupFold
    fit: ConstantEfficiencyFit
    train_predictions: tuple[ScalarPrediction, ...]
    test_predictions: tuple[ScalarPrediction, ...]

    @property
    def test_mse(self) -> float:
        if not self.test_predictions:
            return float("nan")
        return float(np.mean([p.residual * p.residual for p in self.test_predictions]))


@dataclass(frozen=True)
class FoldAwareTrainingResult:
    """Out-of-fold result; each prediction is produced without its own label."""

    folds: tuple[FoldResult, ...]
    oof_predictions: tuple[ScalarPrediction, ...]
    n_rows: int
    n_conditions: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def oof_mse(self) -> float:
        if not self.oof_predictions:
            return float("nan")
        return float(np.mean([p.residual * p.residual for p in self.oof_predictions]))

    @property
    def oof_rmse(self) -> float:
        value = self.oof_mse
        return float(math.sqrt(value)) if math.isfinite(value) else value


@dataclass(frozen=True)
class EventSupervisedTrainingResult:
    """Result for explicitly labelled event-level efficiency samples.

    This is intentionally separate from :func:`fit_fold_aware`: terminal
    height labels do not identify an event target.  Callers must provide
    ``eta`` labels from a paired event measurement or a separately justified
    reference calculation; this function never manufactures them.
    """

    model: TorchEfficiencyMLP
    n_samples: int
    feature_names: tuple[str, ...]
    final_loss: float
    history: Mapping[str, Sequence[float]]
    metadata: Mapping[str, Any] = field(default_factory=dict)


def fit_event_supervised_mlp(
    features: np.ndarray,
    target_efficiency: np.ndarray,
    *,
    hidden_dim: int = 16,
    n_hidden_layers: int = 2,
    r_max: float = float(np.log(2.0)),
    seed: int = 0,
    epochs: int = 300,
    learning_rate: float = 2e-3,
    weight_decay: float = 1e-4,
    checkpoint_path: str | None = None,
) -> EventSupervisedTrainingResult:
    """Fit the bounded event closure from explicit positive event labels.

    The fit is useful for P1-05 when paired event data become available.  It
    is deliberately impossible to call this API with terminal depths: the
    required target is the local positive efficiency itself, so a successful
    result cannot be misreported as end-to-end identification from a final
    rectangle depth.
    """

    x = np.asarray(features, dtype=float)
    y = np.asarray(target_efficiency, dtype=float).reshape(-1)
    if x.ndim != 2 or x.shape[1] != len(FEATURE_NAMES):
        raise ValueError(f"features must have shape (n, {len(FEATURE_NAMES)})")
    if x.shape[0] == 0 or y.size != x.shape[0]:
        raise ValueError("features and target_efficiency must have the same non-zero row count")
    if not np.all(np.isfinite(x)):
        raise ValueError("features must be finite")
    if not np.all(np.isfinite(y)) or np.any(y <= 0.0):
        raise ValueError("target_efficiency must be finite and positive")
    model = TorchEfficiencyMLP(
        hidden_dim=hidden_dim,
        n_hidden_layers=n_hidden_layers,
        r_max=r_max,
        seed=seed,
    )
    history = model.fit(
        x,
        y,
        epochs=epochs,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
    )
    if checkpoint_path:
        model.save_checkpoint(checkpoint_path)
    losses = list(history.get("loss_log_efficiency_mse", ()))
    return EventSupervisedTrainingResult(
        model=model,
        n_samples=int(x.shape[0]),
        feature_names=FEATURE_NAMES,
        final_loss=float(losses[-1]) if losses else float("nan"),
        history=history,
        metadata={
            "trainer": "bounded_event_supervised_mlp",
            "labels_fabricated": False,
            "supervision": "explicit_event_efficiency",
            "terminal_depth_labels_accepted": False,
            "checkpoint_path": checkpoint_path,
        },
    )


def _predict_rows(rows: Sequence[ScalarObservation], efficiency: float) -> tuple[ScalarPrediction, ...]:
    return tuple(
        ScalarPrediction(
            condition_key=row.condition_key,
            target=float(row.target),
            prediction=(pred := predict_scalar(row, efficiency)),
            residual=float(pred - row.target),
            metric=row.metric,
        )
        for row in rows
    )


def fit_fold_aware(
    observations: ScalarObservationTable | Iterable[ScalarObservation | Mapping[str, Any]],
    *,
    n_splits: int = 5,
    seed: int = 0,
    bounds: tuple[float, float] = (0.1, 10.0),
    n_grid: int = 9,
    refinements: int = 3,
) -> FoldAwareTrainingResult:
    """Fit and score a closure with condition-grouped out-of-fold evaluation.

    The test fold is never passed to ``fit_constant_efficiency``.  All rows
    are then predicted with the fold's fitted closure, and each input row is
    emitted exactly once in ``oof_predictions``.  This is terminal-observation
    supervision only; it does not claim that intermediate event states were
    measured or that the fitted scalar is a transferable material constant.
    """

    table = (
        observations
        if isinstance(observations, ScalarObservationTable)
        else ScalarObservationTable.from_rows(observations)
    )
    folds = make_group_folds(table, n_splits=n_splits, seed=seed)
    fold_results: list[FoldResult] = []
    oof: list[ScalarPrediction] = []
    for fold in folds:
        train_rows = tuple(row for row in table.rows if row.condition_key in fold.train_condition_keys)
        test_rows = tuple(row for row in table.rows if row.condition_key in fold.test_condition_keys)
        fit = fit_constant_efficiency(train_rows, bounds=bounds, n_grid=n_grid, refinements=refinements)
        train_predictions = _predict_rows(train_rows, fit.efficiency)
        test_predictions = _predict_rows(test_rows, fit.efficiency)
        oof.extend(test_predictions)
        fold_results.append(
            FoldResult(
                fold=fold,
                fit=fit,
                train_predictions=train_predictions,
                test_predictions=test_predictions,
            )
        )
    return FoldAwareTrainingResult(
        folds=tuple(fold_results),
        oof_predictions=tuple(oof),
        n_rows=len(table),
        n_conditions=len(table.condition_keys),
        metadata={
            "trainer": "constant_efficiency_black_box",
            "n_splits": int(n_splits),
            "seed": int(seed),
            "group_field": "condition_key",
            "metric_scope": "scalar terminal observation",
            "labels_fabricated": False,
            "intermediate_event_supervision": False,
        },
    )


__all__ = [
    "ScalarObservation",
    "ScalarObservationTable",
    "GroupFold",
    "make_group_folds",
    "ConstantEfficiencyFit",
    "fit_constant_efficiency",
    "ScalarPrediction",
    "FoldResult",
    "FoldAwareTrainingResult",
    "fit_fold_aware",
    "EventSupervisedTrainingResult",
    "fit_event_supervised_mlp",
    "predict_scalar",
]
