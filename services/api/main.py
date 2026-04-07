from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Query, status, BackgroundTasks
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import SQLModel, create_engine, Session, select, delete
from secrets import compare_digest
from os import getenv
from typing import Annotated
from pydantic import BaseModel
from datetime import datetime
import shared.entity_models as em
import boto3
import requests


app = FastAPI()
security = HTTPBasic()

WORKER_URL = getenv("WORKER_URL")

COURIER_LOCATION_TABLE_NAME = "CourierLocation"
DELIVERY_ROUTE_TABLE_NAME = "DeliveryRoute"

ADMIN_USERNAME = getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = getenv("ADMIN_PASSWORD")

DATABASE_URL = (
    f"postgresql+psycopg://"
    f"{getenv("DB_USERNAME")}:"
    f"{getenv("DB_PASSWORD")}@"
    f"{getenv("DB_HOST")}:"
    f"{getenv("DB_PORT")}/"
    f"{getenv("DB_NAME")}"
)
db_engine = create_engine(DATABASE_URL)

dynamodb = boto3.resource(
    "dynamodb",
    region_name=getenv("DYNAMODB_REGION"),
    endpoint_url=getenv("DYNAMODB_URL"),
    aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
    aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
)


def get_dynamodb():
    return boto3.resource(
        "dynamodb",
        region_name=getenv("DYNAMODB_REGION"),
        endpoint_url=getenv("DYNAMODB_URL"),
        aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
        aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
    )


def get_table(table_name: str):
    def f(dynamodb=Depends(get_dynamodb)):
        return dynamodb.Table(table_name)

    return f


def get_session():
    with Session(db_engine) as session:
        yield session


def get_auth(credentials: HTTPBasicCredentials = Depends(security)):
    username_is_correct = compare_digest(credentials.username, ADMIN_USERNAME)
    password_is_correct = compare_digest(credentials.password, ADMIN_PASSWORD)

    if username_is_correct and password_is_correct:
        return credentials.username
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def get_user(model):
    def f(session = Depends(get_session), credentials: HTTPBasicCredentials = Depends(security)):
        if str.isnumeric(credentials.username):
            user = session.get(model, int(credentials.username))

            if user:
                return user

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return f


SessionDep = Annotated[Session, Depends(get_session)]
AuthDep = Annotated[None, Depends(get_auth)]
CustomerDep = Annotated[None, Depends(get_user(em.Customer))]
MerchantDep = Annotated[None, Depends(get_user(em.Merchant))]
CourierDep = Annotated[None, Depends(get_user(em.Courier))]
Limit = Annotated[int, Query(ge=1, le=100)]
CourierLocationDep = Annotated[None, Depends(get_table(COURIER_LOCATION_TABLE_NAME))]
DeliveryRouteDep = Annotated[None, Depends(get_table(DELIVERY_ROUTE_TABLE_NAME))]


class ErrorResponse(BaseModel):
    detail: str


NOT_FOUND = {
    404: { "description": "Not found" }
}


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(_req: Request, _exc: IntegrityError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Database constraint violation"}
    )


@app.on_event("startup")
def ensure_table():
    # Mantem o banco alinhado com os modelos atuais, mesmo quando schema.sql estiver desatualizado.
    SQLModel.metadata.create_all(db_engine)

    existing = dynamodb.meta.client.list_tables()["TableNames"]

    if "CourierLocation" not in existing:
        dynamodb.create_table(
            TableName="CourierLocation",
            KeySchema=[
                {"AttributeName": "Order_ID", "KeyType": "HASH"},
                {"AttributeName": "Timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "Order_ID", "AttributeType": "N"},
                {"AttributeName": "Timestamp", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    if "DeliveryRoute" not in existing:
        dynamodb.create_table(
            TableName="DeliveryRoute",
            KeySchema=[
                {"AttributeName": "Order_ID", "KeyType": "HASH"},
                {"AttributeName": "Timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "Order_ID", "AttributeType": "N"},
                {"AttributeName": "Timestamp", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


class REST:
    @staticmethod
    def post(session, model, obj):
        obj_db = model.model_validate(obj)
        session.add(obj_db)
        session.commit()
        session.refresh(obj_db)
        return obj_db

    @staticmethod
    def get_all(session, model, offset, limit):
        objs = session.exec(select(model).offset(offset).limit(limit)).all()
        return objs

    @staticmethod
    def get_one(session, model, id):
        obj = session.get(model, id)
        if not obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
        return obj

    @staticmethod
    def put(session, model, id, obj):
        obj_db = session.get(model, id)
        if not obj_db:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
        obj_db.sqlmodel_update(obj.model_dump())
        session.add(obj_db)
        session.commit()
        session.refresh(obj_db)
        return

    @staticmethod
    def patch(session, model, id, obj):
        obj_db = session.get(model, id)
        if not obj_db:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
        obj_db.sqlmodel_update(obj.model_dump(exclude_unset=True))
        session.add(obj_db)
        session.commit()
        session.refresh(obj_db)
        return

    @staticmethod
    def delete(session, model, id):
        obj = session.get(model, id)
        if not obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
        session.delete(obj)
        session.commit()
        return
        

# Customer ----------------------------------------------

customers = APIRouter(prefix="/customers", tags=["Customer"])


@customers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CustomerPublic)
def create_customer(customer: em.CustomerCreate, session: SessionDep, _auth: AuthDep):
    return REST.post(session, em.Customer, customer)

    
@customers.get("/", response_model=list[em.CustomerPublic])
def get_all_customers(session: SessionDep, _auth: AuthDep, offset: int = 0, limit: Limit = 100):
    return REST.get_all(session, em.Customer, offset, limit)


@customers.get("/me", response_model=em.CustomerPublic)
def get_own_customer(customer: CustomerDep, session: SessionDep):
    return REST.get_one(session, em.Customer, customer.id)


@customers.get("/{id}", response_model=em.CustomerPublic, responses=NOT_FOUND)
def get_customer(id: int, session: SessionDep, _auth: AuthDep):
    return REST.get_one(session, em.Customer, id)


@customers.put("/{id}", responses=NOT_FOUND)
def replace_customer(id: int, customer: em.CustomerCreate, session: SessionDep, _auth: AuthDep):
    return REST.put(session, em.Customer, id, customer)


@customers.patch("/{id}", responses=NOT_FOUND)
def update_customer(id: int, customer: em.CustomerUpdate, session: SessionDep, _auth: AuthDep):
    return REST.patch(session, em.Customer, id, customer)


@customers.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_customer(id: int, session: SessionDep, _auth: AuthDep):
    return REST.delete(session, em.Customer, id)


# Merchant ------------------------------------------------

merchants = APIRouter(prefix="/merchants", tags=["Merchant"])


@merchants.post("/", status_code=status.HTTP_201_CREATED, response_model=em.MerchantPublic)
def create_merchant(merchant: em.MerchantCreate, session: SessionDep, _auth: AuthDep):
    merchant.items = [
        em.Item.model_validate(item) for item in merchant.items
    ]
    return REST.post(session, em.Merchant, merchant)

    
@merchants.get("/", response_model=list[em.MerchantPublic])
def get_all_merchants(session: SessionDep, _auth: AuthDep, offset: int = 0, limit: Limit = 100):
    return REST.get_all(session, em.Merchant, offset, limit)


@merchants.get("/{id}", response_model=em.MerchantPublic, responses=NOT_FOUND)
def get_merchant(id: int, session: SessionDep, _auth: AuthDep):
    return REST.get_one(session, em.Merchant, id)


@merchants.get("/me", response_model=em.MerchantPublic)
def get_own_merchant(merchant: MerchantDep, session: SessionDep):
    return REST.get_one(session, em.Merchant, merchant.id)


@merchants.put("/{id}", responses=NOT_FOUND)
def replace_merchant(id: int, merchant: em.MerchantCreate, session: SessionDep, _auth: AuthDep):
    db_merchant = session.get(em.Merchant, id)
    if not db_merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    db_merchant.sqlmodel_update(
        merchant.model_dump(exclude={"items"})
    )

    for item in db_merchant.items:
        session.delete(item)

    db_merchant.items = [
        em.Item.model_validate(item) for item in merchant.items
    ]

    session.add(db_merchant)
    session.commit()
    session.refresh(db_merchant)


@merchants.patch("/{id}", responses=NOT_FOUND)
def update_merchant(id: int, merchant: em.MerchantUpdate, session: SessionDep, _auth: AuthDep):
    db_merchant = session.get(em.Merchant, id)
    if not db_merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    data = merchant.model_dump(exclude_unset=True)
    items_data = data.pop("items", None)

    db_merchant.sqlmodel_update(data)

    if items_data is not None:
        for item in db_merchant.items:
            session.delete(item)

        db_merchant.items = [
            em.Item.model_validate(item)
            for item in items_data
        ]

    session.add(db_merchant)
    session.commit()
    session.refresh(db_merchant)


@merchants.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_merchant(id: int, session: SessionDep, _auth: AuthDep):
    session.exec(
        delete(em.Item).where(em.Item.merchant_id == id)
    )
    return REST.delete(session, em.Merchant, id)


# Courier -------------------------------------------------

couriers = APIRouter(prefix="/couriers", tags=["Courier"])


@couriers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CourierPublic)
def create_courier(courier: em.CourierCreate, session: SessionDep, _auth: AuthDep):
    return REST.post(session, em.Courier, courier)

    
@couriers.get("/", response_model=list[em.CourierPublic])
def get_all_couriers(session: SessionDep, _auth: AuthDep, offset: int = 0, limit: Limit = 100):
    return REST.get_all(session, em.Courier, offset, limit)


@couriers.get("/{id}", response_model=em.CourierPublic, responses=NOT_FOUND)
def get_courier(id: int, session: SessionDep, _auth: AuthDep):
    return REST.get_one(session, em.Courier, id)


@couriers.put("/{id}", responses=NOT_FOUND)
def replace_courier(id: int, courier: em.CourierCreate, session: SessionDep, _auth: AuthDep):
    return REST.put(session, em.Courier, id, courier)


@couriers.patch("/{id}", responses=NOT_FOUND)
def update_courier(id: int, courier: em.CourierUpdate, session: SessionDep, _auth: AuthDep):
    return REST.patch(session, em.Courier, id, courier)


@couriers.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_courier(id: int, session: SessionDep, _auth: AuthDep):
    return REST.delete(session, em.Courier, id)


# Order --------------------------------------------------

def add_order_event(session: Session, order: em.Order, new_status: str) -> None:
    event = em.OrderEvent(order_id=order.id, status_change=new_status)

    session.add(event)
    session.add(order)
    session.commit()


def look_for_courier(
    order: em.Order,
    customer_address: int,
    merchant_address: int,
    session: Session,
    delivery_routes,
):
    # 1. Encontrar entregador e rota
    payload = {
        "merchant_node": merchant_address,
        "user_node": customer_address,
    }

    response = requests.post(f"{WORKER_URL}/calculate-route", json=payload)

    if response.status_code != 200:
        return

    data = response.json()

    # 2. Extrair dados da resposta
    courier_id = data["courier_id"]

    # 3. Atribuir entregador no BD
    order.courier_id = courier_id

    courier = session.get(em.Courier, courier_id)
    if courier:
        courier.availability = False
        session.add(courier)

    session.add(order)
    session.commit()

    # 4. Armazenar rota no DynamoDB (DeliveryRoute)
    timestamp = int(order.order_time.timestamp())

    route = em.DeliveryRoute(
        courier_id = courier_id,
        distance_to_merchant = int(data["distance_to_merchant"]),
        path_to_merchant = data["path_to_merchant"],
        distance_to_user = int(data["distance_to_user"]),
        path_to_user = data["path_to_user"],
    )

    delivery_routes.put_item(
        Item={
            "Order_ID": order.id,
            "Timestamp": timestamp,
            **route.to_dynamo()
        }
    )


customer_orders = APIRouter(prefix="/me/orders", tags=["Order"], dependencies=[Depends(get_user(em.Customer))])
merchant_orders = APIRouter(prefix="/me/orders", tags=["Order"], dependencies=[Depends(get_user(em.Merchant))])


@customer_orders.get("/", response_model=list[em.OrderPublic])
def get_all_placed_orders(
    session: SessionDep,
    customer: CustomerDep,
    courier_location: CourierLocationDep,
    delivery_routes: DeliveryRouteDep,
    offset: int = 0,
    limit: Limit = 100
):
    orders = session.exec(
        select(em.Order)
            .where(em.Order.customer_id == customer.id)
            .offset(offset)
            .limit(limit)
    ).all()

    return orders


@merchant_orders.get("/", response_model=list[em.OrderPublic])
def get_all_incoming_orders(status: str, session: SessionDep, merchant: MerchantDep, offset: int = 0, limit: Limit = 100):
    orders = session.exec(
        select(em.Order)
            .where(
                em.Order.merchant_id == merchant.id,
                em.Order.status == status
            )
            .offset(offset)
            .limit(limit)
    ).all()

    return orders


@couriers.get("/me/order", response_model=em.OrderPublic | None, tags=["Order"], responses=NOT_FOUND)
def get_assigned_order(
    session: SessionDep,
    courier: CourierDep,
    delivery_routes: DeliveryRouteDep,
):
    order = session.exec(
        select(em.Order).where(
            em.Order.status == "READY_FOR_PICKUP",
            em.Order.courier_id == courier.id
        )
    ).first()

    if not order:
        raise HTTPException(status_code=404, detail="No active order")

    # 1. Query latest route from DynamoDB
    item = delivery_routes.get_item(
        Key={
            "Order_ID": order.id,
            "Timestamp": int(order.order_time.timestamp())
        }
    ).get("Item")

    order_public = em.OrderPublic.model_validate(order, from_attributes=True)

    if item:
        order_public.delivery_route = em.DeliveryRoute.from_dynamo(item)

    return order_public


@couriers.put("/me/location", responses=NOT_FOUND)
def update_courier_location(location: int, session: SessionDep, courier: CourierDep, courier_location: CourierLocationDep):
    order = session.exec(select(em.Order).where(em.Order.courier_id == courier.id)).first()

    if not order:
        raise HTTPException(status_code=404, detail="No active order")

    if order.status != "IN_TRANSIT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order must be in transit")

    timestamp = int(order.order_time.timestamp())

    courier_location.put_item(
        Item={
            "Order_ID": order.id,
            "Timestamp": timestamp,
            "Location": location,
        }
    )

    return {"status": "ok", "timestamp": timestamp}


orders = APIRouter(prefix="/orders", tags=["Order"])


@orders.get("/{order_id}", response_model=em.OrderPublic)
def get_order(
    order_id: int,
    session: SessionDep,
    customer: CustomerDep,
    delivery_routes: DeliveryRouteDep,
    courier_locations: CourierLocationDep
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.customer_id != customer.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_public = em.OrderPublic.model_validate(order, from_attributes=True)

    # 1. Query latest route from DynamoDB
    item_delivery_route = delivery_routes.get_item(
        Key={
            "Order_ID": order.id,
            "Timestamp": int(order.order_time.timestamp())
        }
    ).get("Item")

    if item_delivery_route:
        order_public.delivery_route = em.DeliveryRoute.from_dynamo(item_delivery_route)

    item_courier_location = courier_locations.get_item(
        Key={
            "Order_ID": order.id,
            "Timestamp": int(order.order_time.timestamp())
        }
    ).get("Item")

    if item_courier_location:
        order_public.courier_location = int(item_courier_location["Location"])

    return order_public
    

@orders.get("/{order_id}/events", response_model=list[em.OrderEvent])
def list_order_events(
    order_id: int,
    session: SessionDep,
    _auth: AuthDep,
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return order.events


@orders.post("/", status_code=status.HTTP_201_CREATED, response_model=em.OrderPublic)
def place_order(order: em.OrderCreate, session: SessionDep, customer: CustomerDep, bg_tasks: BackgroundTasks):
    merchant = session.get(em.Merchant, order.merchant_id)
    if not merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")

    order_db = em.Order(
        customer_id = customer.id,
        merchant_id = merchant.id,
        courier_id = None,
        status = "CONFIRMED",
        order_time = datetime.now(),
    )

    session.add(order_db)
    session.commit()
    session.refresh(order_db)

    add_order_event(session, order_db, "CONFIRMED")

    return order_db


@orders.post("/{order_id}/accept")
def accept_order(order_id: int, session: SessionDep, merchant: MerchantDep):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.merchant_id != merchant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    if order.status != "CONFIRMED":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state transition")

    order.status = "PREPARING"

    add_order_event(session, order, "PREPARING")

    session.add(order)
    session.commit()
    session.refresh(order)

    return order


def order_status_transition(session, order: em.Order, old_status: str, new_status: str):
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.status != old_status:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state transition")

    order.status = new_status

    add_order_event(session, order, new_status)


@orders.post("/{order_id}/ready")
def announce_order_is_ready(
    order_id: int,
    session: SessionDep,
    merchant: MerchantDep,
    bg_tasks: BackgroundTasks,
    table: DeliveryRouteDep
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.merchant_id != merchant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(session, order, "PREPARING", "READY_FOR_PICKUP")
    session.add(order)
    session.commit()
    session.refresh(order)

    customer = session.get(em.Customer, order.customer_id)

    bg_tasks.add_task(look_for_courier, order, customer.address, merchant.address, session, table)

    return order


@orders.post("/{order_id}/picked_up")
def announce_order_picked_up(
    order_id: int,
    session: SessionDep,
    courier: CourierDep,
    bg_tasks: BackgroundTasks
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.courier_id != courier.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(session, order, "READY_FOR_PICKUP", "PICKED_UP")
    session.add(order)
    session.commit()
    session.refresh(order)

    return order


@orders.post("/{order_id}/in_transit")
def announce_order_in_transit(
    order_id: int,
    session: SessionDep,
    courier: CourierDep,
    courier_location: CourierLocationDep,
    bg_tasks: BackgroundTasks
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.courier_id != courier.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    merchant = session.get(em.Merchant, order.merchant_id)

    courier_location.put_item(
        Item={
            "Order_ID": order.id,
            "Timestamp": int(order.order_time.timestamp()),
            "Location": merchant.address,
        }
    )
    

    order_status_transition(session, order, "PICKED_UP", "IN_TRANSIT")
    session.add(order)
    session.commit()
    session.refresh(order)

    return order


@orders.post("/{order_id}/delivered")
def announce_order_delivered(
    order_id: int,
    session: SessionDep,
    courier: CourierDep,
    bg_tasks: BackgroundTasks
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.courier_id != courier.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(session, order, "IN_TRANSIT", "DELIVERED")
    session.add(order)
    session.commit()
    session.refresh(order)

    return order


# Adding routes to app
customers.include_router(customer_orders)
merchants.include_router(merchant_orders)
app.include_router(customers)
app.include_router(merchants)
app.include_router(couriers)
app.include_router(orders)


# Other --------------------------------------------------

@app.get("/health", tags=["Other"])
def health():
    return True
