from fastapi import FastAPI, HTTPException
from sentence_transformers import SentenceTransformer
from models import EmbedRequest, EmbedResponse, RAGRequest, RAGResponse
import time

app = FastAPI(title="Vigil-AI Inference Service", version="1.0.0")

model = SentenceTransformer("all-MiniLM-L6-v2")


@app.get("/health")
def health():
    return {"status": "ok", "service": "inference_service"}


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest):
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    start = time.time()
    embedding = model.encode(request.text).tolist()
    elapsed = (time.time() - start) * 1000

    return EmbedResponse(
        embedding=embedding,
        model="all-MiniLM-L6-v2",
        processing_time_ms=round(elapsed, 2)
    )


@app.post("/rag", response_model=RAGResponse)
def rag(request: RAGRequest):
    return RAGResponse(
        query=request.query,
        answer="This is a stub response. RAG pipeline will be wired on Day 3.",
        sources=["doc_001", "doc_002", "doc_003"][: request.top_k],
        note="stub"
    )