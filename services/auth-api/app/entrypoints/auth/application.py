from fastapi import FastAPI

app = FastAPI(title="Auth")


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
