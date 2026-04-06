from pydantic import BaseModel


class EmbedRequest(BaseModel):
    text: str


class EmbedResponse(BaseModel):
    embedding: list[float]
    model: str
    processing_time_ms: float


class RAGRequest(BaseModel):
    query: str
    top_k: int = 3


class RAGResponse(BaseModel):
    query: str
    answer: str
    sources: list[str]
    note: str