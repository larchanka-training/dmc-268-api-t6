from fastapi import FastAPI

app = FastAPI(title="Backend")


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
