from fastapi import FastAPI
from pydantic import BaseModel
from typing import List

app = FastAPI()


# ---- Models ----

class RouteRequest(BaseModel):
    merchant_node: int
    user_node: int


class RouteResponse(BaseModel):
    courier_id: int  # changed to int
    distance_to_merchant: float
    path_to_merchant: List[int]
    distance_to_user: float
    path_to_user: List[int]


# ---- Endpoints ----

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/calculate-route", response_model=RouteResponse)
def calculate_route(_: RouteRequest):
    return RouteResponse(
        courier_id=3,
        distance_to_merchant=1.23,
        path_to_merchant=[1, 2, 3],
        distance_to_user=4.56,
        path_to_user=[3, 4, 5],
    )
