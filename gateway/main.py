import os
from fastapi import FastAPI, HTTPException
from models import QueryRequest, QueryResponse
import httpx
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Vigil-AI Gateway", version="1.0.0")

INFERENCE_URL = os.getenv("INFERENCE_URL", "http://localhost:8001")

Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway"}


@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    async with httpx.AsyncClient(timeout=30.0) as client:

        try:
            embed_response = await client.post(
                f"{INFERENCE_URL}/embed",
                json={"text": request.text}
            )
            embed_response.raise_for_status()
            embedding = embed_response.json()["embedding"]
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Inference service error: {str(e)}")

        try:
            rag_response = await client.post(
                f"{INFERENCE_URL}/rag",
                json={"query": request.text, "top_k": request.top_k}
            )
            rag_response.raise_for_status()
            rag_data = rag_response.json()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"RAG service error: {str(e)}")

    return QueryResponse(
        query=request.text,
        answer=rag_data["answer"],
        sources=rag_data["sources"],
        embedding=embedding,
        note=rag_data["note"]
    )
