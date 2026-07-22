"""Rolling operational metrics for health and stats endpoints."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any


MAX_SAMPLES = 512
HEALTH_PROBE_TTL_S = 30.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 2)
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[int(index)], 2)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


@dataclass
class RollingSeries:
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES))
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def add(self, value_ms: float) -> None:
        numeric = float(value_ms)
        self.samples.append(numeric)
        self.count += 1
        self.total_ms += numeric
        self.max_ms = max(self.max_ms, numeric)

    def snapshot(self) -> dict[str, Any]:
        values = list(self.samples)
        return {
            "count": self.count,
            "avg_ms": round(self.total_ms / self.count, 2) if self.count else 0.0,
            "mean_ms": round(self.total_ms / self.count, 2) if self.count else 0.0,
            "median_ms": _percentile(values, 0.5),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
            "max_ms": round(self.max_ms, 2),
            "sample_size": len(values),
        }


@dataclass
class RequestMetric:
    latency: RollingSeries = field(default_factory=RollingSeries)
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    status_classes: Counter[str] = field(default_factory=Counter)

    def record(self, status_code: int, duration_ms: float) -> None:
        self.latency.add(duration_ms)
        status_class = f"{status_code // 100}xx"
        self.status_classes[status_class] += 1
        if status_code >= 500:
            self.error_count += 1
        elif status_code >= 400:
            self.error_count += 1
        else:
            self.success_count += 1
        if status_code == 504:
            self.timeout_count += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.latency.snapshot(),
            "success_count": self.success_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "status_classes": dict(self.status_classes),
        }


@dataclass
class OutcomeMetric:
    latency: RollingSeries = field(default_factory=RollingSeries)
    outcome_counts: Counter[str] = field(default_factory=Counter)

    def record(self, duration_ms: float, outcome: str) -> None:
        self.latency.add(duration_ms)
        self.outcome_counts[outcome] += 1

    def snapshot(self) -> dict[str, Any]:
        data = self.latency.snapshot()
        data["outcomes"] = dict(self.outcome_counts)
        data["success_count"] = self.outcome_counts.get("success", 0)
        data["error_count"] = sum(
            count
            for name, count in self.outcome_counts.items()
            if name not in {"success", "timeout"}
        )
        data["timeout_count"] = self.outcome_counts.get("timeout", 0)
        return data


class MetricsStore:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self.endpoint_metrics: dict[str, RequestMetric] = {}
        self.inference_metrics: dict[str, OutcomeMetric] = {}
        self.vector_metrics: dict[str, OutcomeMetric] = {}
        self.agent_metrics: dict[str, OutcomeMetric] = {}
        self.health_cache: dict[str, dict[str, Any]] = {}

    def record_request(self, method: str, path: str, status_code: int, duration_ms: float) -> None:
        key = f"{method.upper()} {path}"
        with self._lock:
            metric = self.endpoint_metrics.setdefault(key, RequestMetric())
            metric.record(status_code, duration_ms)

    def record_inference(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        duration_ms: float,
        outcome: str,
    ) -> None:
        key = f"{operation}:{provider}:{model}"
        with self._lock:
            metric = self.inference_metrics.setdefault(key, OutcomeMetric())
            metric.record(duration_ms, outcome)

    def record_vector(self, *, operation: str, duration_ms: float, outcome: str) -> None:
        with self._lock:
            metric = self.vector_metrics.setdefault(operation, OutcomeMetric())
            metric.record(duration_ms, outcome)

    def record_agent_run(self, *, kind: str, duration_ms: float, outcome: str) -> None:
        with self._lock:
            metric = self.agent_metrics.setdefault(kind, OutcomeMetric())
            metric.record(duration_ms, outcome)

    def update_health_probe(self, name: str, payload: dict[str, Any]) -> None:
        probe = {
            **payload,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checked_at_epoch": time.time(),
        }
        with self._lock:
            self.health_cache[name] = probe

    def get_health_probe(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            probe = self.health_cache.get(name)
            if not probe:
                return None
            age = time.time() - float(probe.get("checked_at_epoch", 0))
            if age > HEALTH_PROBE_TTL_S:
                return None
            return dict(probe)

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "window": {
                    "retained_samples_per_series": MAX_SAMPLES,
                    "uptime_s": round(time.time() - self.started_at, 2),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
                },
                "service": {
                    "endpoint_count": len(self.endpoint_metrics),
                    "inference_series": len(self.inference_metrics),
                    "vector_series": len(self.vector_metrics),
                    "agent_series": len(self.agent_metrics),
                },
                "endpoints": {
                    key: metric.snapshot()
                    for key, metric in sorted(self.endpoint_metrics.items())
                },
                "ollama": {
                    key: metric.snapshot()
                    for key, metric in sorted(self.inference_metrics.items())
                },
                "database": {
                    key: metric.snapshot()
                    for key, metric in sorted(self.vector_metrics.items())
                    if key.startswith("supabase_")
                },
                "vector": {
                    key: metric.snapshot()
                    for key, metric in sorted(self.vector_metrics.items())
                },
                "agent_runs": {
                    key: metric.snapshot()
                    for key, metric in sorted(self.agent_metrics.items())
                },
            }


metrics = MetricsStore()
