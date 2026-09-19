
from fastapi import FastAPI

app = FastAPI(title="后端服务")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
