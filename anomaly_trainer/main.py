import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from sklearn.ensemble import IsolationForest

from models import (
    DatasetCaptureRequest,
    DatasetCaptureResponse,
    ModelTrainRequest,
    ModelTrainResponse,
)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", "/artifacts"))
DATASET_ROOT = ARTIFACTS_DIR / "datasets" / "anomaly"
MODEL_ROOT = ARTIFACTS_DIR / "models" / "anomaly"
REGISTRY_ROOT = ARTIFACTS_DIR / "registry" / "anomaly"
FEATURE_NAMES = [
    "latency_p95_ms",
    "error_rate_pct",
    "requests_per_second",
    "cpu_pct",
    "memory_mb",
]

app = FastAPI(title="Vigil-AI Anomaly Trainer", version="1.0.0")
Instrumentator().instrument(app).expose(app)


class PrometheusClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

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


def build_rows(series_by_metric: dict[str, dict[float, float]], step_seconds: int) -> list[dict[str, float | str]]:
    if not series_by_metric:
        return []

    bucketed_series: dict[str, dict[int, float]] = {}
    for metric_name, series in series_by_metric.items():
        bucketed_series[metric_name] = {
            int(round(timestamp / step_seconds) * step_seconds): value
            for timestamp, value in series.items()
        }

    reference_timestamps: set[int] = set()
    for series in bucketed_series.values():
        reference_timestamps.update(series.keys())

    if not reference_timestamps:
        return []

    common_timestamps = sorted(reference_timestamps)
    rows = []
    for timestamp in common_timestamps:
        row = {
            "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
        }
        for feature_name in FEATURE_NAMES:
            row[feature_name] = bucketed_series[feature_name].get(timestamp, 0.0)
        rows.append(row)
    return rows


def write_dataset_files(dataset_dir: Path, rows: list[dict[str, float | str]], manifest: dict) -> tuple[Path, Path, Path]:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    json_path = dataset_dir / "dataset.json"
    csv_path = dataset_dir / "dataset.csv"
    manifest_path = dataset_dir / "manifest.json"

    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", *FEATURE_NAMES])
        writer.writeheader()
        writer.writerows(rows)

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return json_path, csv_path, manifest_path


def load_latest_dataset_pointer(dataset_name: str) -> dict:
    latest_path = DATASET_ROOT / dataset_name / "latest.json"
    if not latest_path.exists():
        raise HTTPException(status_code=404, detail=f"No dataset registry found for {dataset_name}")
    return json.loads(latest_path.read_text(encoding="utf-8"))


def load_dataset_rows(dataset_name: str, dataset_version: str | None) -> tuple[str, list[dict]]:
    resolved_version = dataset_version
    if resolved_version is None:
        latest_pointer = load_latest_dataset_pointer(dataset_name)
        resolved_version = latest_pointer["dataset_version"]

    dataset_path = DATASET_ROOT / dataset_name / resolved_version / "dataset.json"
    if not dataset_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Dataset artifact not found for {dataset_name}:{resolved_version}",
        )

    rows = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not rows:
        raise HTTPException(status_code=400, detail="Dataset artifact exists but contains no rows")

    return resolved_version, rows


def rows_to_matrix(rows: list[dict]) -> np.ndarray:
    return np.array(
        [[float(row[feature_name]) for feature_name in FEATURE_NAMES] for row in rows],
        dtype=float,
    )


def write_model_artifacts(
    model_name: str,
    model_version: str,
    model_bundle: dict,
    manifest: dict,
) -> tuple[Path, Path, Path]:
    model_dir = MODEL_ROOT / model_name / model_version
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "model.joblib"
    manifest_path = model_dir / "manifest.json"
    registry_path = REGISTRY_ROOT / model_name / "latest.json"

    joblib.dump(model_bundle, model_path)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "model_name": model_name,
                "model_version": model_version,
                "model_path": str(model_path),
                "manifest_path": str(manifest_path),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    latest_model_pointer = MODEL_ROOT / model_name / "latest.json"
    latest_model_pointer.parent.mkdir(parents=True, exist_ok=True)
    latest_model_pointer.write_text(
        json.dumps(
            {
                "model_name": model_name,
                "model_version": model_version,
                "manifest_path": str(manifest_path),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return model_path, manifest_path, registry_path


@app.get("/health")
def health():
    return {"status": "ok", "service": "anomaly_trainer"}


@app.post("/datasets/capture", response_model=DatasetCaptureResponse)
async def capture_dataset(request: DatasetCaptureRequest):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=request.lookback_minutes)
    queries = metric_queries(window="1m")

    series_by_metric = {}
    for metric_name, query in queries.items():
        series_by_metric[metric_name] = await prometheus.query_range(
            query=query,
            start=start,
            end=end,
            step_seconds=request.step_seconds,
        )

    rows = build_rows(series_by_metric, request.step_seconds)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No aligned Prometheus samples were available for the requested time window",
        )

    dataset_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dataset_dir = DATASET_ROOT / request.dataset_name / dataset_version
    manifest = {
        "dataset_name": request.dataset_name,
        "dataset_version": dataset_version,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "lookback_minutes": request.lookback_minutes,
        "step_seconds": request.step_seconds,
        "row_count": len(rows),
        "feature_names": FEATURE_NAMES,
        "prometheus_url": PROMETHEUS_URL,
        "queries": queries,
    }
    json_path, csv_path, manifest_path = write_dataset_files(dataset_dir, rows, manifest)

    latest_path = DATASET_ROOT / request.dataset_name / "latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(
        json.dumps(
            {
                "dataset_name": request.dataset_name,
                "dataset_version": dataset_version,
                "manifest_path": str(manifest_path),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return DatasetCaptureResponse(
        status="ok",
        dataset_version=dataset_version,
        dataset_name=request.dataset_name,
        row_count=len(rows),
        feature_names=FEATURE_NAMES,
        json_path=str(json_path),
        csv_path=str(csv_path),
        manifest_path=str(manifest_path),
        note="dataset snapshot captured and versioned",
    )


@app.post("/models/train", response_model=ModelTrainResponse)
async def train_model(request: ModelTrainRequest):
    resolved_dataset_version, rows = load_dataset_rows(
        dataset_name=request.dataset_name,
        dataset_version=request.dataset_version,
    )
    feature_matrix = rows_to_matrix(rows)
    training_samples = len(feature_matrix)
    if training_samples < 10:
        raise HTTPException(
            status_code=400,
            detail=f"Need at least 10 samples to train a model, found {training_samples}",
        )

    contamination = request.contamination
    if contamination is None:
        contamination = min(0.2, max(0.05, 2.0 / training_samples))

    model = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=42,
    )
    model.fit(feature_matrix)
    training_scores = model.decision_function(feature_matrix)

    model_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_bundle = {
        "model": model,
        "training_scores": training_scores,
        "feature_names": FEATURE_NAMES,
        "dataset_name": request.dataset_name,
        "dataset_version": resolved_dataset_version,
        "model_name": request.model_name,
        "model_version": model_version,
        "contamination": contamination,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_samples": training_samples,
    }
    manifest = {
        "model_name": request.model_name,
        "model_version": model_version,
        "dataset_name": request.dataset_name,
        "dataset_version": resolved_dataset_version,
        "feature_names": FEATURE_NAMES,
        "contamination": contamination,
        "training_samples": training_samples,
        "trained_at": model_bundle["trained_at"],
    }
    model_path, manifest_path, registry_path = write_model_artifacts(
        model_name=request.model_name,
        model_version=model_version,
        model_bundle=model_bundle,
        manifest=manifest,
    )

    return ModelTrainResponse(
        status="ok",
        model_name=request.model_name,
        model_version=model_version,
        dataset_name=request.dataset_name,
        dataset_version=resolved_dataset_version,
        training_samples=training_samples,
        feature_names=FEATURE_NAMES,
        model_path=str(model_path),
        manifest_path=str(manifest_path),
        registry_path=str(registry_path),
        note="model trained, versioned, and promoted to latest registry pointer",
    )
