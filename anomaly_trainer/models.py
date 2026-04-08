from pydantic import BaseModel, Field


class DatasetCaptureRequest(BaseModel):
    lookback_minutes: int = Field(default=15, ge=5, le=240)
    step_seconds: int = Field(default=30, ge=10, le=300)
    dataset_name: str = Field(default="anomaly-metrics", min_length=3, max_length=64)


class DatasetCaptureResponse(BaseModel):
    status: str
    dataset_version: str
    dataset_name: str
    row_count: int
    feature_names: list[str]
    json_path: str
    csv_path: str
    manifest_path: str
    note: str


class ModelTrainRequest(BaseModel):
    dataset_name: str = Field(default="anomaly-metrics", min_length=3, max_length=64)
    dataset_version: str | None = None
    model_name: str = Field(default="isolation-forest", min_length=3, max_length=64)
    contamination: float | None = Field(default=None, ge=0.001, le=0.5)


class ModelTrainResponse(BaseModel):
    status: str
    model_name: str
    model_version: str
    dataset_name: str
    dataset_version: str
    training_samples: int
    feature_names: list[str]
    model_path: str
    manifest_path: str
    registry_path: str
    note: str


class RetrainRunRequest(BaseModel):
    dataset_name: str = Field(default="anomaly-metrics", min_length=3, max_length=64)
    model_name: str = Field(default="isolation-forest", min_length=3, max_length=64)
    lookback_minutes: int = Field(default=15, ge=5, le=240)
    step_seconds: int = Field(default=30, ge=10, le=300)
    contamination: float | None = Field(default=None, ge=0.001, le=0.5)


class RetrainStatusResponse(BaseModel):
    enabled: bool
    interval_minutes: int
    last_run_at: str | None = None
    last_status: str | None = None
    last_note: str | None = None
    last_dataset_version: str | None = None
    last_model_version: str | None = None
    policy_path: str
    note: str


class DriftCheckRequest(BaseModel):
    dataset_name: str = Field(default="anomaly-metrics", min_length=3, max_length=64)
    model_name: str = Field(default="isolation-forest", min_length=3, max_length=64)
    lookback_minutes: int = Field(default=15, ge=5, le=240)
    step_seconds: int = Field(default=30, ge=10, le=300)
    threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    trigger_retrain: bool = True
    contamination: float | None = Field(default=None, ge=0.001, le=0.5)


class DriftFeatureDelta(BaseModel):
    feature_name: str
    baseline_mean: float
    current_mean: float
    relative_change: float
    normalized_drift: float


class DriftCheckResponse(BaseModel):
    status: str
    drift_detected: bool
    drift_score: float
    threshold: float
    trigger_retrain: bool
    model_name: str
    model_version: str | None = None
    dataset_name: str
    baseline_dataset_version: str | None = None
    current_dataset_version: str | None = None
    triggered_retraining: bool
    retrain_note: str | None = None
    feature_deltas: list[DriftFeatureDelta]
