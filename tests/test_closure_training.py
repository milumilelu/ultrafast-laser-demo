from __future__ import annotations

import numpy as np
import pytest

from conftest import ROOT, load_example, load_material_card, make_config
from ufdemo.closure_training import (
    ScalarObservation,
    ScalarObservationTable,
    fit_constant_efficiency,
    fit_fold_aware,
    make_group_folds,
    predict_scalar,
    fit_event_supervised_mlp,
)
from ufdemo.closure_evaluation import evaluate_folded_baselines


def _row_config(label: str):
    raw = load_example("analytic_single_pulse.json")
    raw["label"] = label
    # Keep the test solver small.  The test exercises fold/closure semantics,
    # not the spatial convergence of the analytic fixture.
    raw["grid"]["nx"] = 21
    raw["grid"]["ny"] = 21
    raw["grid"]["dx_m"] = 1.0e-6
    raw["grid"]["dy_m"] = 1.0e-6
    raw["path"]["segments"][0]["end_s"] = 1.0e-3
    raw["solver"]["mode"] = "reference"
    return make_config(raw)


def _table(n_conditions: int = 3) -> ScalarObservationTable:
    material = load_material_card(ROOT / "tests/fixtures/analytic_fixture.json")
    rows = []
    for i in range(n_conditions):
        cfg = _row_config(f"condition-{i}")
        row = ScalarObservation(
            condition_key=f"condition-{i}",
            config=cfg,
            material=material,
            target=0.0,
        )
        target = predict_scalar(row, 0.5)
        rows.append(
            ScalarObservation(
                condition_key=row.condition_key,
                config=cfg,
                material=material,
                target=target,
            )
        )
    return ScalarObservationTable(tuple(rows))


def test_group_folds_keep_condition_keys_together() -> None:
    table = _table()
    folds = make_group_folds(table, n_splits=3, seed=7)
    assert len(folds) == 3
    seen = []
    for fold in folds:
        assert set(fold.train_condition_keys).isdisjoint(fold.test_condition_keys)
        seen.extend(fold.test_condition_keys)
    assert sorted(seen) == sorted(table.condition_keys)


def test_table_rejects_missing_or_nonfinite_labels() -> None:
    with pytest.raises(ValueError, match="missing required fields"):
        ScalarObservationTable.from_rows([{"condition_key": "x"}])
    table = _table(1)
    row = table.rows[0]
    with pytest.raises(ValueError, match="finite"):
        ScalarObservation(
            condition_key=row.condition_key,
            config=row.config,
            material=row.material,
            target=np.nan,
        )


def test_constant_fit_changes_recursive_solver_and_uses_only_train_rows() -> None:
    table = _table()
    fit = fit_constant_efficiency(table, bounds=(0.25, 1.0), n_grid=3, refinements=1)
    assert 0.25 <= fit.efficiency <= 1.0
    assert fit.evaluations >= 3
    assert fit.train_mse < 1.0e-20

    result = fit_fold_aware(table, n_splits=3, seed=3, bounds=(0.25, 1.0), n_grid=3, refinements=1)
    assert result.n_rows == 3
    assert result.n_conditions == 3
    assert len(result.oof_predictions) == len(table)
    assert result.metadata["labels_fabricated"] is False
    assert result.metadata["intermediate_event_supervision"] is False
    # Every test prediction comes from a fit whose train/test key sets are
    # disjoint.  This guards against condition-level leakage.
    for fold_result in result.folds:
        assert set(fold_result.fold.train_condition_keys).isdisjoint(fold_result.fold.test_condition_keys)
        assert all(p.condition_key in fold_result.fold.test_condition_keys for p in fold_result.test_predictions)


def test_event_supervised_mlp_requires_explicit_efficiency_labels() -> None:
    features = np.zeros((6, 8), dtype=float)
    target = np.linspace(0.8, 1.2, 6)
    fit = fit_event_supervised_mlp(features, target, hidden_dim=4, n_hidden_layers=1, epochs=3)
    assert fit.n_samples == 6
    assert fit.metadata["labels_fabricated"] is False
    assert fit.metadata["terminal_depth_labels_accepted"] is False
    assert np.all(np.isfinite(fit.model(features)))


def test_p1_06_folded_baseline_evaluator_uses_same_rows() -> None:
    table = _table()
    m0, m1 = evaluate_folded_baselines(table, n_splits=3, seed=3, n_grid=3, refinements=1)
    assert m0.model == "M0_mechanistic"
    assert m1.model == "M1_constant_recursive_closure"
    assert len(m0.predictions) == len(m1.predictions) == len(table)
    assert m1.metadata["labels_fabricated"] is False
