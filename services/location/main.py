from fastapi import FastAPI, HTTPException, Depends
from os import getenv
import boto3
from boto3.resources.base import ServiceResource
from botocore.exceptions import ClientError
from sqlmodel import SQLModel


app = FastAPI()

COURIER_LOCATION_TABLE = "CourierLocation"

dynamodb = boto3.resource(
    "dynamodb",
    region_name=getenv("DYNAMODB_REGION"),
    endpoint_url=getenv("DYNAMODB_URL"),
    aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
    aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
)

NOT_FOUND = { 404: { "description": "Not found" } }


def get_dynamodb():
    return boto3.resource(
        "dynamodb",
        region_name=getenv("DYNAMODB_REGION"),
        endpoint_url=getenv("DYNAMODB_URL"),
        aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
        aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
    )


class Location(SQLModel):
    location: int


@app.put("/")
def update_courier_location(
    order_id: int,
    location: Location,
    dynamo: ServiceResource = Depends(get_dynamodb),
):
    dynamo.Table(COURIER_LOCATION_TABLE).put_item(
        Item={
            "Order_ID": order_id,
            "Location": location.location,
        }
    )


    return {"status": "updated"}


@app.get("/health")
def health():
    return True


@app.get("/debug")
def ddb(dynamo: ServiceResource = Depends(get_dynamodb)):
    items = dynamo.Table(COURIER_LOCATION_TABLE).scan()
    for i in items.get("Items", []):
        print(i)
    return True

