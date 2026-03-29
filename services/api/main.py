from fastapi import FastAPI, APIRouter, WebSocket, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from secrets import compare_digest
from psycopg2 import pool
from os import getenv
from pydantic import BaseModel
from typing import List


app = FastAPI()
security = HTTPBasic()


def auth(credentials: HTTPBasicCredentials = Depends(security)):
    username, password = getenv("ADMIN_USERNAME"), getenv("ADMIN_PASSWORD")

    username_is_correct = compare_digest(credentials.username, username)
    password_is_correct = compare_digest(credentials.password, password)

    if username_is_correct and password_is_correct:
        return credentials.username
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


admin_router = APIRouter(prefix="/admin", dependencies=[Depends(auth)])


# db_pool = pool.SimpleConnectionPool(
#     minconn=1,
#     maxconn=10,
#     host=getenv("DB_HOST"),
#     user="admin",
#     password=getenv("DB_password"),
#     dbname=getenv("DB_NAME")
# )


class User(BaseModel):
    name: str
    address: int


class Courier(BaseModel):
    name: str
    location: int


class Item(BaseModel):
    name: str
    preparation_time: int


class Merchant(BaseModel):
    name: str
    address: int
    items: List[Item]


class Order(BaseModel):
    merchant_id: int
    user_id: int
    item_ids: List[int]


@admin_router.post("/create_user")
def create_user(user: User):
    conn = db_pool.getconn()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user (name, address) VALUES (%s, %s)",
            (user.name, user.address)
        )
        conn.commit()
        cursor.close()
    finally:
        db_pool.putconn(conn)


@admin_router.post("/create_merchant")
def create_merchant(merchant: Merchant):
    ...


@admin_router.post("/create_courier")
def create_courier(courier: Courier):
    ...


@app.post("/create_order")
def create_order(order: Order):
    ...


@app.websocket("/order_status")
async def order_status(websocket: WebSocket):
    ...
    # await websocket.accept()
    # while True:
    #     data = await websocket.receive_text()
    #     await websocket.send_text(f"Message text was: {data}")


# Só para fins de teste
@app.get("/echo")
def echo(msg: str):
    return msg
