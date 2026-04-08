import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from models import FeatureSnapshot, ScoreRequest, ScoreResponse

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
ANOMALY_THRESHOLD = float(os.getenv("ANOMALY_THRESHOLD", "0.8"))
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", "/artifacts"))
REGISTRY_ROOT = ARTIFACTS_DIR / "registry" / "anomaly"

app = FastAPI(title="Vigil-AI Anomaly Detector", version="1.0.0")
Instrumentator().instrument(app).expose(app)

ANOMALY_SCORE_GAUGE = Gauge(
    "vigil_anomaly_score",
    "Latest normalized anomaly score from the isolation forest detector",
)
ANOMALY_PROBABILITY_GAUGE = Gauge(
    "vigil_anomaly_probability",
    "Latest anomaly probability derived from the isolation forest detector",
)
MODEL_TRAINED_GAUGE = Gauge(
    "vigil_anomaly_model_trained",
    "Whether the anomaly detector currently has a trained model",
)
TRAINING_SAMPLES_GAUGE = Gauge(
    "vigil_anomaly_training_samples",
    "Number of samples used to train the current anomaly detector model",
)


@dataclass
class DetectorState:
    model: object | None = None
    training_scores: np.ndarray | None = None
    training_samples: int = 0
    model_name: str | None = None
    model_version: str | None = None
    dataset_name: str | None = None
    dataset_version: str | None = None


state = DetectorState()


class PrometheusClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def query(self, query: str) -> float:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": query},
            )
            response.raise_for_status()
            payload = response.json()

        if payload["status"] != "success":
            raise HTTPException(status_code=502, detail="Prometheus instant query failed")

        result = payload["data"]["result"]
        if not result:
            return 0.0

        return float(result[0]["value"][1])

    async def query_range(self, query: str, start: datetime, end: datetime, step_seconds: int) -> dict[float, float]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "step": step_seconds,
                },
            )
            response.raise_for_status()
            payload = response.json()

        if payload["status"] != "success":
            raise HTTPException(status_code=502, detail="Prometheus range query failed")

        result = payload["data"]["result"]
        if not result:
            return {}

        values = {}
        for timestamp, value in result[0]["values"]:
            values[float(timestamp)] = float(value)
        return values


prometheus = PrometheusClient(PROMETHEUS_URL)


def metric_queries(window: str) -> dict[str, str]:
    return {
        "latency_p95_ms": (
            'histogram_quantile(0.95, sum(rate(http_request_duration_highr_seconds_bucket{job=~"gateway|inference_service",handler!="/metrics"}['
            + window
            + '])) by (le)) * 1000'
        ),
        "error_rate_pct": (
            '(sum(rate(http_requests_total{job=~"gateway|inference_service",status=~"5..",handler!="/metrics"}['
            + window
            + '])) / clamp_min(sum(rate(http_requests_total{job=~"gateway|inference_service",handler!="/metrics"}['
            + window
            + '])), 0.0001)) * 100'
        ),
        "requests_per_second": (
            'sum(rate(http_requests_total{job=~"gateway|inference_service",handler!="/metrics"}[' + window + "]))"
        ),
        "cpu_pct": (
            'sum(rate(process_cpu_seconds_total{job=~"gateway|inference_service"}[' + window + "])) * 100"
        ),
        "memory_mb": (
            'sum(avg_over_time(process_resident_memory_bytes{job=~"gateway|inference_service"}['
            + window
            + '])) / 1024 / 1024'
        ),
    }


def normalize_probability(score: float, training_scores: np.ndarray) -> float:
    if training_scores.size == 0:
        return 0.0

    min_score = float(np.min(training_scores))
    max_score = float(np.max(training_scores))
    if np.isclose(min_score, max_score):
        return 0.0

    normalized = (max_score - score) / (max_score - min_score)
    return float(np.clip(normalized, 0.0, 1.0))


async def collect_current_snapshot() -> FeatureSnapshot:
    queries = metric_queries(window="1m")
    values = {}
    for metric_name, query in queries.items():
        values[metric_name] = await prometheus.query(query)

    return FeatureSnapshot(
        timestamp=datetime.now(timezone.utc).timestamp(),
        latency_p95_ms=values["latency_p95_ms"],
        error_rate_pct=values["error_rate_pct"],
        requests_per_second=values["requests_per_second"],
        cpu_pct=values["cpu_pct"],
        memory_mb=values["memory_mb"],
    )


def load_model_bundle(model_name: str) -> tuple[bool, str]:
    registry_path = REGISTRY_ROOT / model_name / "latest.json"
    if not registry_path.exists():
        state.model = None
        state.training_scores = None
        state.training_samples = 0
        state.model_name = None
        state.model_version = None
        state.dataset_name = None
        state.dataset_version = None
        MODEL_TRAINED_GAUGE.set(0)
        TRAINING_SAMPLES_GAUGE.set(0)
        return False, f"no registered model found for {model_name}"

    pointer = json_load(registry_path)
    model_path = Path(pointer["model_path"])
    if not model_path.exists():
        state.model = None
        state.training_scores = None
        state.training_samples = 0
        state.model_name = None
        state.model_version = None
        state.dataset_name = None
        state.dataset_version = None
        MODEL_TRAINED_GAUGE.set(0)
        TRAINING_SAMPLES_GAUGE.set(0)
        return False, f"registered model artifact missing at {model_path}"

    bundle = joblib.load(model_path)
    state.model = bundle["model"]
    state.training_scores = np.asarray(bundle["training_scores"])
    state.training_samples = int(bundle["training_samples"])
    state.model_name = bundle["model_name"]
    state.model_version = bundle["model_version"]
    state.dataset_name = bundle["dataset_name"]
    state.dataset_version = bundle["dataset_version"]
    MODEL_TRAINED_GAUGE.set(1)
    TRAINING_SAMPLES_GAUGE.set(state.training_samples)
    return True, f"loaded model {state.model_version} from registry"


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "anomaly_detector"}


@app.post("/score", response_model=ScoreResponse)
async def score(request: ScoreRequest):
    trained, note = load_model_bundle(model_name=request.model_name)

    if not trained or state.model is None or state.training_scores is None:
        ANOMALY_SCORE_GAUGE.set(0)
        ANOMALY_PROBABILITY_GAUGE.set(0)
        return ScoreResponse(
            status="model_unavailable",
            model_trained=False,
            training_samples=state.training_samples,
            model_name=request.model_name,
            anomaly_probability=0.0,
            anomaly_score=0.0,
            anomaly_detected=False,
            threshold=ANOMALY_THRESHOLD,
            note=note,
        )

    current = await collect_current_snapshot()
    feature_vector = np.array(
        [[
            current.latency_p95_ms,
            current.error_rate_pct,
            current.requests_per_second,
            current.cpu_pct,
            current.memory_mb,
        ]],
        dtype=float,
    )

    anomaly_score = float(state.model.decision_function(feature_vector)[0])
    anomaly_probability = normalize_probability(anomaly_score, state.training_scores)
    anomaly_detected = bool(state.model.predict(feature_vector)[0] == -1 or anomaly_probability >= ANOMALY_THRESHOLD)

    ANOMALY_SCORE_GAUGE.set(anomaly_score)
    ANOMALY_PROBABILITY_GAUGE.set(anomaly_probability)
    MODEL_TRAINED_GAUGE.set(1)
    TRAINING_SAMPLES_GAUGE.set(state.training_samples)

    return ScoreResponse(
        status="ok",
        model_trained=True,
        training_samples=state.training_samples,
        model_name=state.model_name,
        model_version=state.model_version,
        dataset_name=state.dataset_name,
        dataset_version=state.dataset_version,
        anomaly_probability=round(anomaly_probability, 4),
        anomaly_score=round(anomaly_score, 4),
        anomaly_detected=anomaly_detected,
        threshold=ANOMALY_THRESHOLD,
        current_features=current,
        note=note,
    )
