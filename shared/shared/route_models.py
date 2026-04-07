from pydantic import BaseModel
from typing import List

class RouteRequest(BaseModel):
    merchant_node: int
    user_node: int

class RouteResponse(BaseModel):
    courier_id: int
    distance_to_merchant: float
    path_to_merchant: List[int]
    distance_to_user: float
    path_to_user: List[int]