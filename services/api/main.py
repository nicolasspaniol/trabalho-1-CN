from os import getenv
from secrets import compare_digest
from typing import Annotated, Optional

import boto3
import requests
from boto3.resources.base import ServiceResource
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, delete, select

import models as em
import rest


app = FastAPI()
security = HTTPBasic()

WORKER_URL = getenv("WORKER_URL", "").strip()
LOCATION_URL = getenv("LOCATION_URL", "").strip()

COURIER_LOCATION_TABLE = "CourierLocation"
DELIVERY_ROUTE_TABLE = "DeliveryRoute"

ADMIN_USERNAME = getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = getenv("ADMIN_PASSWORD", "admin")

DATABASE_URL = (
    "postgresql+psycopg://"
    f"{getenv('DB_USERNAME', 'postgres')}:"
    f"{getenv('DB_PASSWORD', '')}@"
    f"{getenv('DB_HOST', 'localhost')}:"
    f"{getenv('DB_PORT', '5432')}/"
    f"{getenv('DB_NAME', 'postgres')}"
)
db_engine = create_engine(DATABASE_URL)

NOT_FOUND = {404: {"description": "Not found"}}


def get_dynamodb() -> ServiceResource:
    return boto3.resource(
        "dynamodb",
        region_name=getenv("DYNAMODB_REGION"),
        endpoint_url=getenv("DYNAMODB_URL"),
        aws_access_key_id=getenv("DYNAMODB_ACCESS_KEY_ID"),
        aws_secret_access_key=getenv("DYNAMODB_SECRET_ACCESS_KEY"),
    )


def get_session():
    with Session(db_engine) as session:
        yield session


def get_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    username_is_correct = compare_digest(credentials.username, ADMIN_USERNAME)
    password_is_correct = compare_digest(credentials.password, ADMIN_PASSWORD)
    if username_is_correct and password_is_correct:
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


def get_user(model):
    def dependency(session: SessionDep, credentials: HTTPBasicCredentials = Depends(security)):
        if credentials.username.isnumeric():
            user = session.get(model, int(credentials.username))
            if user:
                return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return dependency


def get_dynamo_item(table, order_id: int):
    return table.get_item(Key={"Order_ID": order_id}).get("Item")


def assert_exists(obj, obj_id: int, name: str) -> None:
    if not obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{name.title()} with ID {obj_id} not found",
        )


def add_order_event(order: em.Order, new_status: em.OrderStatus) -> None:
    order.events.append(em.OrderEvent(updated_status=new_status))


def order_status_transition(order: em.Order, old_status: em.OrderStatus, new_status: em.OrderStatus) -> None:
    if order.status != old_status:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state transition")
    order.status = new_status
    add_order_event(order, new_status)


def write_courier_location(dynamo: ServiceResource, order_id: int, location: int) -> None:
    if LOCATION_URL:
        try:
            requests.put(
                f"{LOCATION_URL.rstrip('/')}/couriers/me/location",
                params={"order_id": order_id},
                json={"location": location},
                timeout=5,
            ).raise_for_status()
            return
        except Exception:
            pass

    dynamo.Table(COURIER_LOCATION_TABLE).put_item(
        Item={
            "Order_ID": order_id,
            "Location": location,
        }
    )


SessionDep = Annotated[Session, Depends(get_session)]
DynamoDep = Annotated[ServiceResource, Depends(get_dynamodb)]

AdminDep = Annotated[str, Depends(get_auth)]
UserDep = Annotated[em.User, Depends(get_user(em.User))]
CustomerDep = Annotated[em.Customer, Depends(get_user(em.Customer))]
MerchantDep = Annotated[em.User, Depends(get_user(em.User))]
CourierDep = Annotated[em.Courier, Depends(get_user(em.Courier))]

Limit = Annotated[int, Query(ge=1, le=100)]


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(_req: Request, _exc: IntegrityError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Database constraint violation"},
    )


@app.on_event("startup")
def ensure_tables() -> None:
    # Nao derruba o processo por indisponibilidade temporaria do banco.
    try:
        SQLModel.metadata.create_all(db_engine)
    except Exception as exc:
        print(f"[startup] aviso: falha ao inicializar schema SQL: {exc}")

    # Nao derruba o processo por indisponibilidade temporaria do DynamoDB.
    try:
        dynamodb = get_dynamodb()
        existing = dynamodb.meta.client.list_tables()["TableNames"]

        if COURIER_LOCATION_TABLE not in existing:
            dynamodb.create_table(
                TableName=COURIER_LOCATION_TABLE,
                KeySchema=[{"AttributeName": "Order_ID", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "Order_ID", "AttributeType": "N"}],
                BillingMode="PAY_PER_REQUEST",
            )

        if DELIVERY_ROUTE_TABLE not in existing:
            dynamodb.create_table(
                TableName=DELIVERY_ROUTE_TABLE,
                KeySchema=[{"AttributeName": "Order_ID", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "Order_ID", "AttributeType": "N"}],
                BillingMode="PAY_PER_REQUEST",
            )
    except Exception as exc:
        print(f"[startup] aviso: falha ao inicializar DynamoDB: {exc}")


customers = APIRouter(prefix="/customers", tags=["Customer"])
merchants = APIRouter(prefix="/merchants", tags=["Merchant"])
couriers = APIRouter(prefix="/couriers", tags=["Courier"])
orders = APIRouter(prefix="/orders", tags=["Order"])


@customers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CustomerPublic)
def create_customer(customer: em.CustomerCreate, session: SessionDep, _auth: AdminDep):
    user = em.User(role=em.UserRole.customer)
    session.add(user)
    session.flush()
    customer_db = em.Customer(
        user_id=user.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        address=customer.address,
    )
    session.add(customer_db)
    session.commit()
    session.refresh(customer_db)
    return customer_db


@customers.get("/", response_model=list[em.CustomerPublic])
def get_all_customers(session: SessionDep, _auth: AdminDep, offset: int = 0, limit: Limit = 100):
    return rest.get_all(session, em.Customer, offset, limit)


@customers.get("/me", response_model=em.CustomerPublic)
def get_own_customer(customer: CustomerDep, session: SessionDep):
    return rest.get_one(session, em.Customer, customer.user_id)


@customers.get("/{id}", response_model=em.CustomerPublic, responses=NOT_FOUND)
def get_customer(id: int, session: SessionDep, _auth: AdminDep):
    return rest.get_one(session, em.Customer, id)


@customers.put("/{id}", responses=NOT_FOUND)
def replace_customer(id: int, customer: em.CustomerCreate, session: SessionDep, _auth: AdminDep):
    return rest.put(session, em.Customer, id, customer)


@customers.patch("/{id}", responses=NOT_FOUND)
def update_customer(id: int, customer: em.CustomerUpdate, session: SessionDep, _auth: AdminDep):
    return rest.patch(session, em.Customer, id, customer)


@customers.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_customer(id: int, session: SessionDep, _auth: AdminDep):
    return rest.delete(session, em.Customer, id)


@merchants.post("/", status_code=status.HTTP_201_CREATED, response_model=em.MerchantPublic)
def create_merchant(merchant: em.MerchantCreate, session: SessionDep, _auth: AdminDep):
    user = em.User(role=em.UserRole.merchant)
    session.add(user)
    session.flush()
    merchant_db = em.Merchant(
        user_id=user.id,
        name=merchant.name,
        type=merchant.type,
        address=merchant.address,
    )
    session.add(merchant_db)
    session.flush()
    for item in merchant.items:
        item_db = em.Item(
            name=item.name,
            preparation_time=item.preparation_time,
            price=item.price,
            merchant_id=merchant_db.user_id,
        )
        session.add(item_db)
    session.commit()
    session.refresh(merchant_db)
    return merchant_db


@merchants.get("/", response_model=list[em.MerchantPublic])
def get_all_merchants(session: SessionDep, _auth: AdminDep, offset: int = 0, limit: Limit = 100):
    return rest.get_all(session, em.Merchant, offset, limit)


@merchants.get("/{id}", response_model=em.MerchantPublic, responses=NOT_FOUND)
def get_merchant(id: int, session: SessionDep, _auth: AdminDep):
    return rest.get_one(session, em.Merchant, id)


@merchants.get("/me", response_model=em.MerchantPublic)
def get_own_merchant(merchant: MerchantDep, session: SessionDep):
    return rest.get_one(session, em.Merchant, merchant.id)


@merchants.put("/{id}", responses=NOT_FOUND)
def replace_merchant(id: int, merchant: em.MerchantCreate, session: SessionDep, _auth: AdminDep):
    db_merchant = session.get(em.Merchant, id)
    if not db_merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    db_merchant.sqlmodel_update(merchant.model_dump(exclude={"items"}))
    for item in db_merchant.items:
        session.delete(item)
    db_merchant.items = [em.Item.model_validate(item) for item in merchant.items]

    session.add(db_merchant)
    session.commit()
    session.refresh(db_merchant)


@merchants.patch("/{id}", responses=NOT_FOUND)
def update_merchant(id: int, merchant: em.MerchantUpdate, session: SessionDep, _auth: AdminDep):
    db_merchant = session.get(em.Merchant, id)
    if not db_merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    data = merchant.model_dump(exclude_unset=True)
    items_data = data.pop("items", None)
    db_merchant.sqlmodel_update(data)

    if items_data is not None:
        for item in db_merchant.items:
            session.delete(item)
        db_merchant.items = [em.Item.model_validate(item) for item in items_data]

    session.add(db_merchant)
    session.commit()
    session.refresh(db_merchant)


@merchants.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_merchant(id: int, session: SessionDep, _auth: AdminDep):
    session.exec(delete(em.Item).where(em.Item.merchant_id == id))
    return rest.delete(session, em.Merchant, id)


@couriers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CourierPublic)
def create_courier(courier: em.CourierCreate, session: SessionDep, _auth: AdminDep):
    user = em.User(role=em.UserRole.courier)
    session.add(user)
    session.flush()
    courier_db = em.Courier(
        user_id=user.id,
        name=courier.name,
        location=courier.location,
        vehicle_type=courier.vehicle_type,
        availability=courier.availability,
    )
    session.add(courier_db)
    session.commit()
    session.refresh(courier_db)
    return courier_db


@couriers.get("/", response_model=list[em.CourierPublic])
def get_all_couriers(session: SessionDep, _auth: AdminDep, offset: int = 0, limit: Limit = 100):
    return rest.get_all(session, em.Courier, offset, limit)


@couriers.get("/me", response_model=em.CourierPublic)
def get_own_courier(session: SessionDep, courier: CourierDep):
    return rest.get_one(session, em.Courier, courier.user_id)


@couriers.get("/{id}", response_model=em.CourierPublic, responses=NOT_FOUND)
def get_courier(id: int, session: SessionDep, _auth: AdminDep):
    return rest.get_one(session, em.Courier, id)


@couriers.put("/{id}", responses=NOT_FOUND)
def replace_courier(id: int, courier: em.CourierCreate, session: SessionDep, _auth: AdminDep):
    return rest.put(session, em.Courier, id, courier)


@couriers.patch("/{id}", responses=NOT_FOUND)
def update_courier(id: int, courier: em.CourierUpdate, session: SessionDep, _auth: AdminDep):
    return rest.patch(session, em.Courier, id, courier)


@couriers.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_courier(id: int, session: SessionDep, _auth: AdminDep):
    return rest.delete(session, em.Courier, id)


@couriers.get("/me/order", response_model=em.OrderPublicComplete, responses=NOT_FOUND, tags=["Order"])
def get_assigned_order(session: SessionDep, courier: CourierDep, dynamo: DynamoDep):
    order = session.exec(
        select(em.Order).where(
            em.Order.courier_id == courier.user_id,
            em.Order.status.in_([em.OrderStatus.ready_for_pickup, em.OrderStatus.picked_up, em.OrderStatus.in_transit]),
        )
    ).first()

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active order")

    order_public = em.OrderPublicComplete.model_validate(order, from_attributes=True)

    delivery_route = get_dynamo_item(dynamo.Table(DELIVERY_ROUTE_TABLE), order.id)
    if delivery_route:
        order_public.delivery_route = em.DeliveryRoute.from_dynamo(delivery_route)

    courier_location = get_dynamo_item(dynamo.Table(COURIER_LOCATION_TABLE), order.id)
    if courier_location:
        order_public.courier_location = em.CourierLocation.from_dynamo(courier_location)

    return order_public


@couriers.put("/me/location")
def update_courier_location(location: int, session: SessionDep, courier: CourierDep, dynamo: DynamoDep):
    order = session.exec(
        select(em.Order).where(
            em.Order.courier_id == courier.user_id,
            em.Order.status == em.OrderStatus.in_transit,
        )
    ).first()

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active order in transit")

    write_courier_location(dynamo, order.id, location)
    return {"status": "updated"}


def look_for_courier(order_id: int, customer_address: int, merchant_address: int):
    if not WORKER_URL:
        return

    response = requests.post(
        f"{WORKER_URL.rstrip('/')}/calculate-route",
        json={"merchant_node": merchant_address, "user_node": customer_address},
        timeout=10,
    )
    if response.status_code != 200:
        return

    data = response.json()
    courier_id = int(data["courier_id"])

    route = em.DeliveryRoute(
        courier_id=courier_id,
        distance_to_merchant=int(data["distance_to_merchant"]),
        path_to_merchant=[int(node) for node in data["path_to_merchant"]],
        distance_to_user=int(data["distance_to_user"]),
        path_to_user=[int(node) for node in data["path_to_user"]],
    )

    try:
        dynamo = get_dynamodb()
        dynamo.Table(DELIVERY_ROUTE_TABLE).put_item(Item={"Order_ID": order_id, **route.to_dynamo()})
    except Exception:
        pass

    with Session(db_engine) as session:
        order = session.get(em.Order, order_id)
        if not order:
            return

        order.courier_id = courier_id
        courier = session.get(em.Courier, courier_id)
        if courier:
            courier.availability = False
            session.add(courier)

        session.add(order)
        session.commit()


@orders.get("/{order_id}", response_model=em.OrderPublicComplete)
def get_order(order_id: int, session: SessionDep, user: UserDep, dynamo: DynamoDep):
    order: em.Order = session.get(em.Order, order_id)
    assert_exists(order, order_id, "order")

    if user.id not in (order.customer_id, order.merchant_id, order.courier_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_public = em.OrderPublicComplete.model_validate(order, from_attributes=True)

    if order.status.idx() > em.OrderStatus.ready_for_pickup.idx():
        delivery_route = get_dynamo_item(dynamo.Table(DELIVERY_ROUTE_TABLE), order.id)
        if delivery_route:
            order_public.delivery_route = em.DeliveryRoute.from_dynamo(delivery_route)

        courier_location = get_dynamo_item(dynamo.Table(COURIER_LOCATION_TABLE), order.id)
        if courier_location:
            order_public.courier_location = em.CourierLocation.from_dynamo(courier_location)

    return order_public


@orders.get("/{order_id}/events", response_model=list[em.OrderEvent])
def list_order_events(order_id: int, session: SessionDep, _auth: AdminDep):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order.events


@orders.post("/", status_code=status.HTTP_201_CREATED, response_model=em.OrderPublic)
def place_order(order: em.OrderCreate, session: SessionDep, customer: CustomerDep):
    merchant = session.get(em.Merchant, order.merchant_id)
    if not merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")

    order_db = em.Order(
        merchant_id=order.merchant_id,
        customer_id=customer.user_id,
        courier_id=None,
        status=em.OrderStatus.confirmed,
    )
    add_order_event(order_db, em.OrderStatus.confirmed)

    session.add(order_db)
    session.commit()
    session.refresh(order_db)
    return order_db


@orders.get("/", response_model=list[em.OrderPublic])
def list_orders(
    session: SessionDep,
    user: UserDep,
    status: Optional[em.OrderStatus] = None,
    offset: int = 0,
    limit: Limit = 100,
):
    user_id_field = {
        em.UserRole.customer: em.Order.customer_id,
        em.UserRole.merchant: em.Order.merchant_id,
        em.UserRole.courier: em.Order.courier_id,
    }[user.role]

    conditions = [user_id_field == user.id]
    if status is not None:
        conditions.append(em.Order.status == status)

    return session.exec(select(em.Order).where(*conditions).offset(offset).limit(limit)).all()


@orders.patch("/{order_id}")
def update_order(order_id: int, order: em.OrderUpdate, session: SessionDep, user: UserDep, bg_tasks: BackgroundTasks, dynamo: DynamoDep):
    order_db = session.get(em.Order, order_id)
    assert_exists(order_db, order_id, "order")

    updated_status = order.model_dump(exclude_unset=True).get("status")
    if updated_status is None:
        return order_db

    if updated_status.idx() != order_db.status.idx() + 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state transition")

    if updated_status.idx() <= em.OrderStatus.ready_for_pickup.idx():
        if user.id != order_db.merchant_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")
    elif user.id != order_db.courier_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    if updated_status == em.OrderStatus.ready_for_pickup:
        bg_tasks.add_task(
            look_for_courier,
            order_db.id,
            order_db.customer.address,
            order_db.merchant.address,
        )
    elif updated_status == em.OrderStatus.picked_up:
        write_courier_location(dynamo, order_id, order_db.merchant.address)

    order_db.status = updated_status
    add_order_event(order_db, updated_status)

    session.add(order_db)
    session.commit()
    session.refresh(order_db)
    return order_db


@orders.post("/{order_id}/accept")
def accept_order(order_id: int, session: SessionDep, merchant: MerchantDep):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.merchant_id != merchant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(order, em.OrderStatus.confirmed, em.OrderStatus.preparing)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@orders.post("/{order_id}/ready")
def announce_order_is_ready(order_id: int, session: SessionDep, merchant: MerchantDep, bg_tasks: BackgroundTasks):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.merchant_id != merchant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    merchant_db = session.get(em.Merchant, merchant.id)
    if not merchant_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")

    order_status_transition(order, em.OrderStatus.preparing, em.OrderStatus.ready_for_pickup)
    session.add(order)
    session.commit()
    session.refresh(order)

    customer = session.get(em.Customer, order.customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    bg_tasks.add_task(look_for_courier, order.id, customer.address, merchant_db.address)
    return order


@orders.post("/{order_id}/picked_up")
def announce_order_picked_up(order_id: int, session: SessionDep, courier: CourierDep, bg_tasks: BackgroundTasks):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.courier_id != courier.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(order, em.OrderStatus.ready_for_pickup, em.OrderStatus.picked_up)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@orders.post("/{order_id}/in_transit")
def announce_order_in_transit(order_id: int, session: SessionDep, courier: CourierDep, dynamo: DynamoDep, bg_tasks: BackgroundTasks):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.courier_id != courier.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    merchant = session.get(em.Merchant, order.merchant_id)
    write_courier_location(dynamo, order.id, merchant.address)

    order_status_transition(order, em.OrderStatus.picked_up, em.OrderStatus.in_transit)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@orders.post("/{order_id}/delivered")
def announce_order_delivered(order_id: int, session: SessionDep, courier: CourierDep, bg_tasks: BackgroundTasks):
    order = session.get(em.Order, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.courier_id != courier.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_status_transition(order, em.OrderStatus.in_transit, em.OrderStatus.delivered)

    courier_db = session.get(em.Courier, courier.user_id)
    if courier_db:
        courier_db.availability = True
        session.add(courier_db)

    session.add(order)
    session.commit()
    session.refresh(order)
    return order


app.include_router(customers)
app.include_router(merchants)
app.include_router(couriers)
app.include_router(orders)


@app.get("/health", tags=["Other"])
def health():
    return True
