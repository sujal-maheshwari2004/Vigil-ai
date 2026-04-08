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
