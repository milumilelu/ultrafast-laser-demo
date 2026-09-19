"""Constrained learned efficiency closures for the event solver.

The event solver already owns the path, beam geometry, response semantics and
surface commit.  This module supplies only the optional unknown-process factor
``eta_phi``.  It is applied to a *positive event-depth increment* before the
single surface commit, so its effect propagates into later defocus and history.

Torch is optional.  The reference solver accepts any callable implementing the
small protocol below; the baseline remains dependency-free and unchanged when
no closure is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np


class EfficiencyClosure(Protocol):
    """Callable contract for a state-dependent efficiency correction."""

    def __call__(self, features: np.ndarray) -> np.ndarray:
        """Return one finite positive efficiency per flattened feature row."""

    def metadata(self) -> dict[str, Any]:
        """Return auditable model metadata."""


FEATURE_NAMES: tuple[str, ...] = (
    "log_fluence_over_threshold",
    "depth_over_rayleigh",
    "radial_over_window_spot_radius",
    "exposure_count_log1p",
    "illumination_count_log1p",
    "pulse_duration_over_ref",
    "pass_index_log1p",
    "event_dt_over_ref",
)


def build_features(
    *,
    fluence: Any,
    threshold: Any,
    depth_m: Any,
    exposure_count: Any,
    illumination_count: Any,
    r2_m2: Any,
    spot_radius_m: float,
    rayleigh_range_m: float | None,
    pulse_duration_s: float | None,
    pass_index: int,
    event_dt_s: float | None,
    pulse_duration_ref_s: float = 1e-12,
    event_dt_ref_s: float = 1e-6,
) -> np.ndarray:
    """Construct dimensionless, causal closure features.

    Only the event-start state and the current pulse are used.  Planned final
    pass count, terminal depth, or any future event is intentionally absent.
    ``spot_radius_m`` supplied by the current `FluencePatch` is the window
    scale (the maximum local radius), so this feature is a window-normalized
    radial proxy rather than a per-cell optical radius.
    ``exposure_count`` and ``illumination_count`` remain separate because the
    former counts positive-removal events while the latter counts beam-window
    illumination events in the existing surface protocol.
    """

    F, T = np.broadcast_arrays(np.asarray(fluence, dtype=float), np.asarray(threshold, dtype=float))
    shape = F.shape
    tiny = np.finfo(float).tiny
    z_r = None if rayleigh_range_m is None else max(float(rayleigh_range_m), tiny)
    w = max(float(spot_radius_m), tiny)
    depth = np.broadcast_to(np.asarray(depth_m, dtype=float), shape)
    exposure = np.broadcast_to(np.asarray(exposure_count, dtype=float), shape)
    illumination = np.broadcast_to(np.asarray(illumination_count, dtype=float), shape)
    r2 = np.broadcast_to(np.asarray(r2_m2, dtype=float), shape)
    pulse_ratio = (
        float(pulse_duration_s) / max(float(pulse_duration_ref_s), tiny)
        if pulse_duration_s is not None
        else 0.0
    )
    dt_ratio = float(event_dt_s) / max(float(event_dt_ref_s), tiny) if event_dt_s is not None else 0.0
    cols = (
        np.log(np.maximum(F / np.maximum(T, tiny), tiny)),
        np.zeros(shape, dtype=float) if z_r is None else depth / z_r,
        np.sqrt(np.maximum(r2, 0.0)) / w,
        np.log1p(np.maximum(exposure, 0.0)),
        np.log1p(np.maximum(illumination, 0.0)),
        np.full(shape, pulse_ratio, dtype=float),
        np.full(shape, np.log1p(max(int(pass_index), 0)), dtype=float),
        np.full(shape, dt_ratio, dtype=float),
    )
    features = np.stack(cols, axis=-1).reshape(-1, len(FEATURE_NAMES))
    if not np.all(np.isfinite(features)):
        raise ValueError("closure features contain NaN/Inf")
    return features


@dataclass(frozen=True)
class ConstantEfficiencyClosure:
    """Deterministic baseline closure; 1.0 exactly recovers the physics solver."""

    efficiency: float = 1.0

    def __post_init__(self) -> None:
        if not np.isfinite(self.efficiency) or self.efficiency <= 0.0:
            raise ValueError("efficiency must be finite and positive")

    def __call__(self, features: np.ndarray) -> np.ndarray:
        return np.full((np.asarray(features).shape[0],), float(self.efficiency), dtype=float)

    def metadata(self) -> dict[str, Any]:
        return {
            "kind": "constant_efficiency",
            "efficiency": float(self.efficiency),
            "feature_names": list(FEATURE_NAMES),
        }


class TorchEfficiencyMLP:
    """Small MLP with a bounded log-efficiency output.

    The final layer starts at zero, so an untrained instance gives eta=1 and
    exactly preserves the mechanistic baseline.  The bound is
    ``exp(-r_max) <= eta <= exp(r_max)``.  Training is deliberately exposed for
    explicit per-event efficiency targets only; terminal depth labels must be
    handled by a separate fold-aware closed-loop training pipeline.
    """

    def __init__(
        self,
        *,
        hidden_dim: int = 16,
        n_hidden_layers: int = 2,
        r_max: float = float(np.log(2.0)),
        seed: int = 0,
        device: str = "cpu",
    ) -> None:
        if hidden_dim <= 0 or n_hidden_layers <= 0 or not np.isfinite(r_max) or r_max <= 0.0:
            raise ValueError("hidden_dim, n_hidden_layers and r_max must be positive finite values")
        if r_max > float(np.log(10.0)):
            raise ValueError("r_max is capped at log(10), giving a maximum eta range of [0.1, 10]")
        try:
            import torch
            from torch import nn
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ImportError("TorchEfficiencyMLP requires optional dependency 'torch'.") from exc
        torch.manual_seed(int(seed))
        layers: list[Any] = []
        in_dim = len(FEATURE_NAMES)
        for _ in range(int(n_hidden_layers)):
            layers += [nn.Linear(in_dim, int(hidden_dim)), nn.Tanh()]
            in_dim = int(hidden_dim)
        layers.append(nn.Linear(in_dim, 1))
        self._torch = torch
        self._network = nn.Sequential(*layers).to(device)
        nn.init.zeros_(self._network[-1].weight)
        nn.init.zeros_(self._network[-1].bias)
        self.r_max = float(r_max)
        self.device = str(device)

    def __call__(self, features: np.ndarray) -> np.ndarray:
        x = self._torch.as_tensor(np.asarray(features, dtype=np.float32), device=self.device)
        self._network.eval()
        with self._torch.no_grad():
            raw = self._network(x).reshape(-1)
            eta = self._torch.exp(self.r_max * self._torch.tanh(raw))
        return eta.detach().cpu().numpy().astype(float, copy=False)

    def metadata(self) -> dict[str, Any]:
        return {
            "kind": "bounded_efficiency_mlp",
            "r_max": self.r_max,
            "eta_bounds": [float(np.exp(-self.r_max)), float(np.exp(self.r_max))],
            "feature_names": list(FEATURE_NAMES),
            "hidden_dim": int(self._network[0].out_features),
            "device": self.device,
        }

    def save_checkpoint(self, path: str) -> None:
        """Persist architecture, bound and weights for one deployable closure."""
        payload = {
            "schema": "ufdemo.efficiency_closure/1",
            "hidden_dim": int(self._network[0].out_features),
            "n_hidden_layers": sum(1 for layer in self._network if layer.__class__.__name__ == "Linear") - 1,
            "r_max": self.r_max,
            "feature_names": list(FEATURE_NAMES),
            "state_dict": self._network.state_dict(),
        }
        self._torch.save(payload, str(path))

    @classmethod
    def from_checkpoint(cls, path: str, *, device: str = "cpu") -> "TorchEfficiencyMLP":
        """Load a fixed checkpoint without retraining or inspecting final fields."""
        import pathlib

        torch = __import__("torch")
        payload = torch.load(pathlib.Path(path), map_location=device, weights_only=False)
        if not isinstance(payload, dict) or payload.get("schema") != "ufdemo.efficiency_closure/1":
            raise ValueError("unsupported efficiency closure checkpoint schema")
        if tuple(payload.get("feature_names") or ()) != FEATURE_NAMES:
            raise ValueError("checkpoint feature_names do not match the solver closure contract")
        obj = cls(
            hidden_dim=int(payload["hidden_dim"]),
            n_hidden_layers=int(payload["n_hidden_layers"]),
            r_max=float(payload["r_max"]),
            device=device,
        )
        obj._network.load_state_dict(payload["state_dict"])
        obj._network.eval()
        obj._checkpoint_path = str(path)
        return obj

    def fit(
        self,
        features: np.ndarray,
        target_efficiency: np.ndarray,
        *,
        epochs: int = 300,
        learning_rate: float = 2e-3,
        weight_decay: float = 1e-4,
    ) -> dict[str, list[float]]:
        """Fit explicit positive efficiency targets in log space."""

        x_np = np.asarray(features, dtype=np.float32)
        y_np = np.asarray(target_efficiency, dtype=np.float32).reshape(-1)
        if x_np.ndim != 2 or x_np.shape[1] != len(FEATURE_NAMES):
            raise ValueError(f"features must have shape (n, {len(FEATURE_NAMES)})")
        if y_np.size != x_np.shape[0] or np.any(~np.isfinite(y_np)) or np.any(y_np <= 0.0):
            raise ValueError("target_efficiency must be finite and positive")
        eta_low, eta_high = float(np.exp(-self.r_max)), float(np.exp(self.r_max))
        if np.any(y_np < eta_low) or np.any(y_np > eta_high):
            raise ValueError(
                "target_efficiency is outside the configured closure bounds "
                f"[{eta_low:g}, {eta_high:g}]; increase r_max explicitly if justified"
            )
        if epochs <= 0 or learning_rate <= 0.0:
            raise ValueError("epochs and learning_rate must be positive")
        x = self._torch.as_tensor(x_np, device=self.device)
        log_y = self._torch.log(self._torch.as_tensor(y_np, device=self.device))
        opt = self._torch.optim.AdamW(self._network.parameters(), lr=float(learning_rate), weight_decay=float(weight_decay))
        losses: list[float] = []
        self._network.train()
        for _ in range(int(epochs)):
            opt.zero_grad(set_to_none=True)
            raw = self._network(x).reshape(-1)
            pred = self.r_max * self._torch.tanh(raw)
            loss = self._torch.mean((pred - log_y) ** 2)
            loss.backward()
            opt.step()
            losses.append(float(loss.detach().cpu().item()))
        return {"loss_log_efficiency_mse": losses}


def load_efficiency_closure(path: str, *, device: str = "cpu") -> TorchEfficiencyMLP:
    """Public deployment loader used by CLI/UI adapters."""
    return TorchEfficiencyMLP.from_checkpoint(path, device=device)


def apply_closure(
    increment: Any,
    features: np.ndarray,
    closure: EfficiencyClosure | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Multiply a mechanistic increment by a validated positive eta."""

    values = np.asarray(increment, dtype=float)
    if closure is None:
        return values, np.ones(values.size, dtype=float)
    eta = np.asarray(closure(features), dtype=float).reshape(-1)
    if eta.size != values.size:
        raise ValueError("efficiency closure output size does not match increment")
    if np.any(~np.isfinite(eta)) or np.any(eta <= 0.0):
        raise ValueError("efficiency closure must return finite positive eta")
    scaled = values * eta.reshape(values.shape)
    return scaled, eta
