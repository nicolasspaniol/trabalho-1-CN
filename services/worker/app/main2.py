import hashlib
import os
import socket
import time

from fastapi import FastAPI, Query


app = FastAPI(title="Routing Worker - Test Mode")


def _instance_id() -> str:
    # Em ECS/Fargate, hostname normalmente identifica a task/container.
    return os.getenv("HOSTNAME", socket.gethostname())


@app.get("/health")
async def health():
    return {"status": "ok", "instance": _instance_id()}


@app.get("/hello")
async def hello(name: str = "world"):
    return {
        "message": f"hello, {name}",
        "instance": _instance_id(),
        "service": "routing-worker",
    }


@app.get("/cpu-burn")
def cpu_burn(
    seconds: int = Query(15, ge=1, le=120),
    payload_kb: int = Query(64, ge=1, le=1024),
):
    # Endpoint para gerar carga de CPU e testar autoscaling por utilizacao.
    end_at = time.time() + seconds
    block = ("x" * (payload_kb * 1024)).encode("utf-8")
    digest = block
    iterations = 0

    while time.time() < end_at:
        digest = hashlib.sha256(digest).digest()
        iterations += 1

    return {
        "status": "done",
        "seconds": seconds,
        "payload_kb": payload_kb,
        "iterations": iterations,
        "instance": _instance_id(),
    }
