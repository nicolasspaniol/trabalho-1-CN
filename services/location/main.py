from fastapi import Depends, FastAPI, HTTPException, status
from os import getenv
import boto3
from boto3.resources.base import ServiceResource
from botocore.config import Config
from sqlmodel import SQLModel


app = FastAPI()

COURIER_LOCATION_TABLE = "CourierLocation"

def get_dynamodb():
    connect_timeout = float(getenv("DYNAMODB_CONNECT_TIMEOUT", "2"))
    read_timeout = float(getenv("DYNAMODB_READ_TIMEOUT", "2"))
    max_attempts = int(getenv("DYNAMODB_MAX_ATTEMPTS", "2"))
    region = (
        getenv("DYNAMODB_REGION")
        or getenv("AWS_REGION")
        or getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return boto3.resource(
        "dynamodb",
        region_name=region,
        endpoint_url=getenv("DYNAMODB_URL"),
        aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
        aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
        config=Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": "standard"},
        ),
    )


class Location(SQLModel):
    location: int


@app.put("/couriers/me/location")
def update_courier_location(
    order_id: int,
    location: Location,
    dynamo: ServiceResource = Depends(get_dynamodb),
):
    try:
        dynamo.Table(COURIER_LOCATION_TABLE).put_item(
            Item={
                "Order_ID": order_id,
                "Location": location.location,
            }
        )
    except Exception as exc:
        # Preferível retornar 503 do que deixar a conexão pendurada (e gerar 502 no ALB).
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return {"status": "updated"}


@app.get("/health")
def health():
    return True


@app.get("/debug")
def ddb(dynamo: ServiceResource = Depends(get_dynamodb)):
    try:
        items = dynamo.Table(COURIER_LOCATION_TABLE).scan(Limit=50)
        for i in items.get("Items", []):
            print(i)
        return True
    except Exception:
        return False
