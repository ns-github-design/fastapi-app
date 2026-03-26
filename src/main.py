from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/version")
def version():
    return {
        "version": os.getenv("GITHUB_SHA", "dev"),
        "environment": os.getenv("APP_ENV", "local")
    }
