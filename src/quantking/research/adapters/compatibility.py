"""LightGBM Stage 0 compatibility adapter."""

from __future__ import annotations

from pathlib import Path

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)


def probe_lightgbm(root: Path) -> ProbeResult:
    """Fit and predict a deterministic tiny synthetic regression model."""
    return capture_probe("lightgbm", lambda: _run_lightgbm(root))


def _run_lightgbm(_root: Path) -> ProbePayload:
    import lightgbm
    import numpy as np

    features = np.asarray(
        [
            [0.0, 0.0],
            [0.0, 1.0],
            [1.0, 0.0],
            [1.0, 1.0],
            [2.0, 0.0],
            [2.0, 1.0],
            [3.0, 0.0],
            [3.0, 1.0],
        ],
        dtype=np.float64,
    )
    labels = np.asarray([0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0], dtype=np.float64)
    dataset = lightgbm.Dataset(features, label=labels, free_raw_data=False)
    model = lightgbm.train(
        {
            "objective": "regression",
            "verbosity": -1,
            "deterministic": True,
            "force_col_wise": True,
            "seed": 7,
            "num_threads": 1,
            "num_leaves": 3,
            "min_data_in_leaf": 1,
            "feature_fraction": 1.0,
            "bagging_fraction": 1.0,
        },
        dataset,
        num_boost_round=4,
    )
    first = np.asarray(model.predict(features), dtype=np.float64)
    second = np.asarray(model.predict(features), dtype=np.float64)
    if first.shape != (8,) or not np.isfinite(first).all() or not np.array_equal(first, second):
        raise CompatibilityProbeError(
            ProbeErrorCode.EXECUTION_FAILED,
            "LightGBM deterministic fit/predict evidence is invalid",
        )
    return ProbePayload(
        str(lightgbm.__version__),
        ("synthetic_dataset_created", "deterministic_model_fitted", "prediction_verified"),
    )
