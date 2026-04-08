from pydantic import BaseModel, Field


class ScoreRequest(BaseModel):
    model_name: str = Field(default="isolation-forest", min_length=3, max_length=64)


class FeatureSnapshot(BaseModel):
    timestamp: float
    latency_p95_ms: float
    error_rate_pct: float
    requests_per_second: float
    cpu_pct: float
    memory_mb: float


class ScoreResponse(BaseModel):
    status: str
    model_trained: bool
    training_samples: int
    model_name: str | None = None
    model_version: str | None = None
    dataset_name: str | None = None
    dataset_version: str | None = None
    anomaly_probability: float
    anomaly_score: float
    anomaly_detected: bool
    threshold: float
    current_features: FeatureSnapshot | None = None
    note: str
