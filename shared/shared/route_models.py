from pydantic import BaseModel
from typing import List

class RouteRequest(BaseModel):
    restaurant_node: int
    client_node: int
    available_drivers: List[int]

class RouteResponse(BaseModel):
    driver_id: str
    distance_to_restaurant: float
    path_to_restaurant: List[int]
    distance_to_client: float
    path_to_client: List[int]
    status: str = "CONFIRMED"