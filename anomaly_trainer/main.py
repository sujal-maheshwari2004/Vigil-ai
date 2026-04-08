import asyncio
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from sklearn.ensemble import IsolationForest

from models import (
    DatasetCaptureRequest,
    DatasetCaptureResponse,
    DriftCheckRequest,
    DriftCheckResponse,
    DriftFeatureDelta,
    ModelTrainRequest,
    ModelTrainResponse,
    RetrainRunRequest,
    RetrainStatusResponse,
)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", "/artifacts"))
DATASET_ROOT = ARTIFACTS_DIR / "datasets" / "anomaly"
MODEL_ROOT = ARTIFACTS_DIR / "models" / "anomaly"
REGISTRY_ROOT = ARTIFACTS_DIR / "registry" / "anomaly"
RETRAIN_POLICY_ENABLED = os.getenv("RETRAIN_POLICY_ENABLED", "true").lower() == "true"
RETRAIN_INTERVAL_MINUTES = int(os.getenv("RETRAIN_INTERVAL_MINUTES", "60"))
DEFAULT_DATASET_NAME = os.getenv("RETRAIN_DATASET_NAME", "anomaly-metrics")
DEFAULT_MODEL_NAME = os.getenv("RETRAIN_MODEL_NAME", "isolation-forest")
DEFAULT_LOOKBACK_MINUTES = int(os.getenv("RETRAIN_LOOKBACK_MINUTES", "15"))
DEFAULT_STEP_SECONDS = int(os.getenv("RETRAIN_STEP_SECONDS", "30"))
DRIFT_POLICY_ENABLED = os.getenv("DRIFT_POLICY_ENABLED", "true").lower() == "true"
DRIFT_INTERVAL_MINUTES = int(os.getenv("DRIFT_INTERVAL_MINUTES", "15"))
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.35"))
FEATURE_NAMES = [
    "latency_p95_ms",
    "error_rate_pct",
    "requests_per_second",
    "cpu_pct",
    "memory_mb",
]

app = FastAPI(title="Vigil-AI Anomaly Trainer", version="1.0.0")
Instrumentator().instrument(app).expose(app)

RETRAIN_LAST_SUCCESS_GAUGE = Gauge(
    "vigil_retrain_last_success_timestamp",
    "Unix timestamp of the last successful anomaly retraining run",
)
RETRAIN_LAST_STATUS_GAUGE = Gauge(
    "vigil_retrain_last_status",
    "1 when the last retraining run succeeded, 0 otherwise",
)
RETRAIN_RUN_COUNT_GAUGE = Gauge(
    "vigil_retrain_run_count",
    "Count of retraining runs recorded by the trainer service",
)
DRIFT_SCORE_GAUGE = Gauge(
    "vigil_drift_score",
    "Latest normalized drift score computed by the trainer service",
)
DRIFT_DETECTED_GAUGE = Gauge(
    "vigil_drift_detected",
    "1 when the latest drift check detected drift, 0 otherwise",
)


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


def load_latest_model_pointer(model_name: str) -> dict:
    latest_path = REGISTRY_ROOT / model_name / "latest.json"
    if not latest_path.exists():
        raise HTTPException(status_code=404, detail=f"No model registry found for {model_name}")
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


def compute_feature_means(rows: list[dict]) -> dict[str, float]:
    matrix = rows_to_matrix(rows)
    means = np.mean(matrix, axis=0)
    return {feature_name: float(means[index]) for index, feature_name in enumerate(FEATURE_NAMES)}


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


def retrain_policy_path() -> Path:
    return REGISTRY_ROOT / "retrain-policy.json"


def load_retrain_policy() -> dict:
    path = retrain_policy_path()
    if not path.exists():
        return {
            "enabled": RETRAIN_POLICY_ENABLED,
            "interval_minutes": RETRAIN_INTERVAL_MINUTES,
            "last_run_at": None,
            "last_status": None,
            "last_note": "policy initialized but no retraining has run yet",
            "last_dataset_version": None,
            "last_model_version": None,
            "last_trigger": None,
            "last_drift_score": None,
            "last_drift_detected": None,
            "run_count": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_retrain_policy(payload: dict) -> Path:
    path = retrain_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


async def capture_dataset_internal(request: DatasetCaptureRequest) -> DatasetCaptureResponse:
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


async def train_model_internal(request: ModelTrainRequest) -> ModelTrainResponse:
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


async def run_retraining_cycle(request: RetrainRunRequest, trigger: str) -> dict:
    policy = load_retrain_policy()
    run_started_at = datetime.now(timezone.utc).isoformat()

    try:
        dataset_response = await capture_dataset_internal(
            DatasetCaptureRequest(
                lookback_minutes=request.lookback_minutes,
                step_seconds=request.step_seconds,
                dataset_name=request.dataset_name,
            )
        )
        model_response = await train_model_internal(
            ModelTrainRequest(
                dataset_name=request.dataset_name,
                dataset_version=dataset_response.dataset_version,
                model_name=request.model_name,
                contamination=request.contamination,
            )
        )
        updated_policy = {
            **policy,
            "enabled": RETRAIN_POLICY_ENABLED,
            "interval_minutes": RETRAIN_INTERVAL_MINUTES,
            "last_run_at": run_started_at,
            "last_status": "success",
            "last_note": f"{trigger} retraining completed successfully",
            "last_dataset_version": dataset_response.dataset_version,
            "last_model_version": model_response.model_version,
            "last_trigger": trigger,
            "run_count": int(policy.get("run_count", 0)) + 1,
        }
        save_retrain_policy(updated_policy)
        RETRAIN_LAST_SUCCESS_GAUGE.set(datetime.now(timezone.utc).timestamp())
        RETRAIN_LAST_STATUS_GAUGE.set(1)
        RETRAIN_RUN_COUNT_GAUGE.set(updated_policy["run_count"])
        return updated_policy
    except HTTPException as exc:
        updated_policy = {
            **policy,
            "enabled": RETRAIN_POLICY_ENABLED,
            "interval_minutes": RETRAIN_INTERVAL_MINUTES,
            "last_run_at": run_started_at,
            "last_status": "failed",
            "last_note": f"{trigger} retraining failed: {exc.detail}",
            "last_trigger": trigger,
            "run_count": int(policy.get("run_count", 0)) + 1,
        }
        save_retrain_policy(updated_policy)
        RETRAIN_LAST_STATUS_GAUGE.set(0)
        RETRAIN_RUN_COUNT_GAUGE.set(updated_policy["run_count"])
        raise


async def evaluate_drift(request: DriftCheckRequest) -> DriftCheckResponse:
    model_pointer = load_latest_model_pointer(request.model_name)
    model_manifest_path = Path(model_pointer["manifest_path"])
    model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    baseline_dataset_version, baseline_rows = load_dataset_rows(
        dataset_name=model_manifest["dataset_name"],
        dataset_version=model_manifest["dataset_version"],
    )
    current_capture = await capture_dataset_internal(
        DatasetCaptureRequest(
            dataset_name=request.dataset_name,
            lookback_minutes=request.lookback_minutes,
            step_seconds=request.step_seconds,
        )
    )
    current_dataset_version, current_rows = load_dataset_rows(
        dataset_name=request.dataset_name,
        dataset_version=current_capture.dataset_version,
    )

    baseline_means = compute_feature_means(baseline_rows)
    current_means = compute_feature_means(current_rows)
    feature_scales = {
        "latency_p95_ms": 10.0,
        "error_rate_pct": 1.0,
        "requests_per_second": 0.1,
        "cpu_pct": 1.0,
        "memory_mb": 16.0,
    }

    feature_deltas: list[DriftFeatureDelta] = []
    normalized_scores = []
    for feature_name in FEATURE_NAMES:
        baseline_mean = baseline_means[feature_name]
        current_mean = current_means[feature_name]
        relative_change = abs(current_mean - baseline_mean) / max(abs(baseline_mean), feature_scales[feature_name])
        normalized_drift = float(min(relative_change / 2.0, 1.0))
        normalized_scores.append(normalized_drift)
        feature_deltas.append(
            DriftFeatureDelta(
                feature_name=feature_name,
                baseline_mean=round(baseline_mean, 4),
                current_mean=round(current_mean, 4),
                relative_change=round(relative_change, 4),
                normalized_drift=round(normalized_drift, 4),
            )
        )

    drift_score = float(np.mean(normalized_scores)) if normalized_scores else 0.0
    drift_detected = drift_score >= request.threshold
    DRIFT_SCORE_GAUGE.set(drift_score)
    DRIFT_DETECTED_GAUGE.set(1 if drift_detected else 0)

    retrain_note = None
    triggered_retraining = False
    if drift_detected and request.trigger_retrain:
        retrain_policy = await run_retraining_cycle(
            RetrainRunRequest(
                dataset_name=request.dataset_name,
                model_name=request.model_name,
                lookback_minutes=request.lookback_minutes,
                step_seconds=request.step_seconds,
                contamination=request.contamination,
            ),
            trigger="drift_triggered",
        )
        retrain_policy["last_drift_score"] = round(drift_score, 4)
        retrain_policy["last_drift_detected"] = drift_detected
        save_retrain_policy(retrain_policy)
        retrain_note = retrain_policy["last_note"]
        triggered_retraining = True
    else:
        policy = load_retrain_policy()
        policy["last_drift_score"] = round(drift_score, 4)
        policy["last_drift_detected"] = drift_detected
        save_retrain_policy(policy)

    return DriftCheckResponse(
        status="ok",
        drift_detected=drift_detected,
        drift_score=round(drift_score, 4),
        threshold=request.threshold,
        trigger_retrain=request.trigger_retrain,
        model_name=request.model_name,
        model_version=model_manifest["model_version"],
        dataset_name=request.dataset_name,
        baseline_dataset_version=baseline_dataset_version,
        current_dataset_version=current_dataset_version,
        triggered_retraining=triggered_retraining,
        retrain_note=retrain_note,
        feature_deltas=feature_deltas,
    )


async def retraining_loop():
    while True:
        if RETRAIN_POLICY_ENABLED:
            try:
                await run_retraining_cycle(
                    RetrainRunRequest(
                        dataset_name=DEFAULT_DATASET_NAME,
                        model_name=DEFAULT_MODEL_NAME,
                        lookback_minutes=DEFAULT_LOOKBACK_MINUTES,
                        step_seconds=DEFAULT_STEP_SECONDS,
                    ),
                    trigger="scheduled",
                )
            except HTTPException:
                pass
        await asyncio.sleep(RETRAIN_INTERVAL_MINUTES * 60)


async def drift_loop():
    while True:
        if DRIFT_POLICY_ENABLED:
            try:
                await evaluate_drift(
                    DriftCheckRequest(
                        dataset_name=DEFAULT_DATASET_NAME,
                        model_name=DEFAULT_MODEL_NAME,
                        lookback_minutes=DEFAULT_LOOKBACK_MINUTES,
                        step_seconds=DEFAULT_STEP_SECONDS,
                        threshold=DRIFT_THRESHOLD,
                        trigger_retrain=True,
                    )
                )
            except HTTPException:
                pass
        await asyncio.sleep(DRIFT_INTERVAL_MINUTES * 60)


@app.on_event("startup")
async def startup_event():
    policy = load_retrain_policy()
    save_retrain_policy(policy)
    RETRAIN_RUN_COUNT_GAUGE.set(int(policy.get("run_count", 0)))
    RETRAIN_LAST_STATUS_GAUGE.set(1 if policy.get("last_status") == "success" else 0)
    DRIFT_SCORE_GAUGE.set(float(policy.get("last_drift_score") or 0))
    DRIFT_DETECTED_GAUGE.set(1 if policy.get("last_drift_detected") else 0)
    if RETRAIN_POLICY_ENABLED:
        app.state.retraining_task = asyncio.create_task(retraining_loop())
    if DRIFT_POLICY_ENABLED:
        app.state.drift_task = asyncio.create_task(drift_loop())


@app.on_event("shutdown")
async def shutdown_event():
    task = getattr(app.state, "retraining_task", None)
    if task:
        task.cancel()
    drift_task = getattr(app.state, "drift_task", None)
    if drift_task:
        drift_task.cancel()


@app.get("/health")
def health():
    return {"status": "ok", "service": "anomaly_trainer"}


@app.post("/datasets/capture", response_model=DatasetCaptureResponse)
async def capture_dataset(request: DatasetCaptureRequest):
    return await capture_dataset_internal(request)


@app.post("/models/train", response_model=ModelTrainResponse)
async def train_model(request: ModelTrainRequest):
    return await train_model_internal(request)


@app.post("/retrain/run", response_model=RetrainStatusResponse)
async def retrain_run(request: RetrainRunRequest):
    policy = await run_retraining_cycle(request, trigger="manual")
    path = retrain_policy_path()
    return RetrainStatusResponse(
        enabled=bool(policy["enabled"]),
        interval_minutes=int(policy["interval_minutes"]),
        last_run_at=policy.get("last_run_at"),
        last_status=policy.get("last_status"),
        last_note=policy.get("last_note"),
        last_dataset_version=policy.get("last_dataset_version"),
        last_model_version=policy.get("last_model_version"),
        policy_path=str(path),
        note="manual retraining cycle completed",
    )


@app.get("/retrain/status", response_model=RetrainStatusResponse)
async def retrain_status():
    policy = load_retrain_policy()
    path = retrain_policy_path()
    return RetrainStatusResponse(
        enabled=bool(policy["enabled"]),
        interval_minutes=int(policy["interval_minutes"]),
        last_run_at=policy.get("last_run_at"),
        last_status=policy.get("last_status"),
        last_note=policy.get("last_note"),
        last_dataset_version=policy.get("last_dataset_version"),
        last_model_version=policy.get("last_model_version"),
        policy_path=str(path),
        note="retraining policy status loaded",
    )


@app.post("/retrain/drift-check", response_model=DriftCheckResponse)
async def retrain_drift_check(request: DriftCheckRequest):
    return await evaluate_drift(request)
