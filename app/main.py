
from fastapi import FastAPI

from app.routes_recall import router as recall_router

app = FastAPI(title="食品品牌宣传合规台")
app.include_router(recall_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
