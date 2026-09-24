from fastapi import APIRouter, FastAPI

api_router = APIRouter(prefix="/api")

app = FastAPI(title="Backend")


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@api_router.get("/runs")
async def list_runs() -> dict[str, list[object] | None]:
    return {"items": [], "nextCursor": None}


app.include_router(api_router)
