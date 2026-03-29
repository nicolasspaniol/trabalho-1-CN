from fastapi import FastAPI, HTTPException
from shared.models.route_models import RouteRequest, RouteResponse

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    pass

def try_reserve_driver(driver_id):
    pass

def reconstruct_path(predecessors: dict, target: int):
    pass

@app.post("/calculate-route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    pass

@app.get("/health")
async def health():
    pass
