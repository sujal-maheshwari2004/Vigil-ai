from pydantic import BaseModel


class QueryRequest(BaseModel):
    text: str
    top_k: int = 3


class QueryResponse(BaseModel):
    query: str
    answer: str
    sources: list[str]
    embedding: list[float]
    note: str