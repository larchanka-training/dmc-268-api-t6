from fastapi import FastAPI

app = FastAPI(title="Portal API")


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
