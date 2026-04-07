from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Query, status, BackgroundTasks
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import create_engine, Session, select, delete
from secrets import compare_digest
from os import getenv
from typing import Annotated, Optional
import boto3
from boto3.resources.base import ServiceResource
import requests

# Dependências locais
import models as em
import rest


app = FastAPI()
security = HTTPBasic()

WORKER_URL = getenv("WORKER_URL")

COURIER_LOCATION_TABLE = "CourierLocation"
DELIVERY_ROUTE_TABLE = "DeliveryRoute"

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

NOT_FOUND = { 404: { "description": "Not found" } }


# HELPERS -------------------------------------

def assert_exists(obj, id, name):
    msg = f"{str.title(name)} with ID {id} not found"

    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def get_dynamo_item(table, order_id: int):
    return table \
        .get_item(Key={"Order_ID": order_id}) \
        .get("Item")
    

# DEPENDÊNCIAS -------------------------------------

def get_dynamodb():
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
    def f(session: SessionDep, credentials: HTTPBasicCredentials = Depends(security)):
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


# Armazenamento
SessionDep = Annotated[Session, Depends(get_session)]
DynamoDep = Annotated[ServiceResource, Depends(get_dynamodb)]

# "Autenticação"
AdminDep = Annotated[None, Depends(get_auth)]
UserDep = Annotated[em.User, Depends(get_user(em.User))]
CustomerDep = Annotated[em.Customer, Depends(get_user(em.Customer))]
MerchantDep = Annotated[em.Merchant, Depends(get_user(em.Merchant))]
CourierDep = Annotated[em.Courier, Depends(get_user(em.Courier))]

# Outros
Limit = Annotated[int, Query(ge=1, le=100)]


# OUTROS HANDLERS -------------------------------------

@app.exception_handler(IntegrityError)
async def integrity_exception_handler(_req: Request, _exc: IntegrityError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Database constraint violation"}
    )


@app.on_event("startup")
def ensure_table():
    existing = dynamodb.meta.client.list_tables()["TableNames"]

    if "CourierLocation" not in existing:
        dynamodb.create_table(
            TableName="CourierLocation",
            KeySchema=[
                {"AttributeName": "Order_ID", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "Order_ID", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    if "DeliveryRoute" not in existing:
        dynamodb.create_table(
            TableName="DeliveryRoute",
            KeySchema=[
                {"AttributeName": "Order_ID", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "Order_ID", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


# CUSTOMER ----------------------------------------------

customers = APIRouter(prefix="/customers", tags=["Customer"])


@customers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CustomerPublic)
def create_customer(customer: em.CustomerCreate, session: SessionDep, _auth: AdminDep):
    customer_db = em.Customer(
        **customer.model_dump(),
        user=em.User(role=em.UserRole.customer)
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


# MERCHANT ------------------------------------------------

merchants = APIRouter(prefix="/merchants", tags=["Merchant"])


@merchants.post("/", status_code=status.HTTP_201_CREATED, response_model=em.MerchantPublic)
def create_merchant(merchant: em.MerchantCreate, session: SessionDep, _auth: AdminDep):
    merchant_db = em.Merchant(
        **merchant.model_dump(exclude={"items"}),
        user=em.User(role=em.UserRole.merchant),
        items=[em.Item.model_validate(item) for item in merchant.items]
    )

    session.add(merchant_db)
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
    return rest.get_one(session, em.Merchant, merchant.user_id)


@merchants.put("/{id}", responses=NOT_FOUND)
def replace_merchant(id: int, merchant: em.MerchantCreate, session: SessionDep, _auth: AdminDep):
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

        db_merchant.items = [
            em.Item.model_validate(item)
            for item in items_data
        ]

    session.add(db_merchant)
    session.commit()
    session.refresh(db_merchant)


@merchants.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_merchant(id: int, session: SessionDep, _auth: AdminDep):
    session.exec(
        delete(em.Item).where(em.Item.merchant_id == id)
    )
    return rest.delete(session, em.Merchant, id)


# COURIER -------------------------------------------------

couriers = APIRouter(prefix="/couriers", tags=["Courier"])


@couriers.post("/", status_code=status.HTTP_201_CREATED, response_model=em.CourierPublic)
def create_courier(courier: em.CourierCreate, session: SessionDep, _auth: AdminDep):
    courier_db = em.Courier(
        **courier.model_dump(),
        user=em.User(role=em.UserRole.courier)
    )

    session.add(courier_db)
    session.commit()
    session.refresh(courier_db)

    return courier_db


@couriers.get("/", response_model=list[em.CourierPublic])
def get_all_couriers(session: SessionDep, _auth: AdminDep, offset: int = 0, limit: Limit = 100):
    return rest.get_all(session, em.Courier, offset, limit)


@couriers.get("/me", response_model=em.CourierPublic)
def get_own_courier(id: int, session: SessionDep, courier: CourierDep):
    return rest.get_one(session, em.Customer, courier.user_id)


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


# ORDER --------------------------------------------------

def look_for_courier(
    order: em.Order,
    customer_address: int,
    merchant_address: int,
    session: Session,
    dynamo: ServiceResource,
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

    # 4. Armazenar rota no DynamoDB (DeliveryRoute)

    route = em.DeliveryRoute(
        courier_id = courier_id,
        distance_to_merchant = int(data["distance_to_merchant"]),
        path_to_merchant = data["path_to_merchant"],
        distance_to_user = int(data["distance_to_user"]),
        path_to_user = data["path_to_user"],
    )

    dynamo.Table(DELIVERY_ROUTE_TABLE).put_item(
        Item={
            "Order_ID": order.id,
            **route.to_dynamo()
        }
    )

    session.add(order)
    session.commit()


orders = APIRouter(prefix="/orders", tags=["Order"])


@orders.get("/{order_id}", response_model=em.OrderPublicComplete)
def get_order(
    order_id: int,
    session: SessionDep,
    user: UserDep,
    dynamo: DynamoDep,
):
    order: em.Order = session.get(em.Order, order_id)
    assert_exists(order, order_id, "order")

    if user.id not in (order.customer_id, order.merchant_id, order.courier_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your order")

    order_public: em.OrderPublicComplete = em.OrderPublicComplete.model_validate(order, from_attributes=True)

    if order.status.idx() <= em.OrderStatus.ready_for_pickup.idx():
        return order_public

    # 1. Query latest route and courier location from DynamoDB
    delivery_routes = dynamo.Table(DELIVERY_ROUTE_TABLE)
    courier_locations = dynamo.Table(COURIER_LOCATION_TABLE)

    delivery_route = get_dynamo_item(delivery_routes, order.id)
    if delivery_route:
        order_public.delivery_route = em.DeliveryRoute.from_dynamo(delivery_route)

    courier_location = get_dynamo_item(courier_locations, order.id)
    if courier_location:
        order_public.courier_location = em.CourierLocation.from_dynamo(courier_location)

    return order_public
    

@orders.get("/{order_id}/events", response_model=list[em.OrderEvent])
def list_order_events(
    order_id: int,
    session: SessionDep,
    _auth: AdminDep,
):
    order = session.get(em.Order, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return order.events


@orders.post("/", status_code=status.HTTP_201_CREATED, response_model=em.OrderPublic)
def place_order(order: em.OrderCreate, session: SessionDep, customer: CustomerDep):
    merchant = session.get(em.Merchant, order.merchant_id)
    if not merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")

    order_db = em.Order(
        customer_id = customer.user_id,
        merchant_id = merchant.user_id,
        courier_id = None,
        status = em.OrderStatus.confirmed,
    )

    event = em.OrderEvent(updated_status=em.OrderStatus.confirmed)
    order_db.events.append(event)

    session.add(order_db)
    session.commit()
    session.refresh(order_db)

    return order_db


@orders.get("/", response_model=list[em.OrderPublic])
def get_all_orders(
    status: Optional[em.OrderStatus] = None,
    session: SessionDep = None,
    user: UserDep = None,
    offset: int = 0,
    limit: Limit = 100
):
    user_id_field = {
        em.UserRole.customer: em.Order.customer_id,
        em.UserRole.merchant: em.Order.merchant_id,
        em.UserRole.courier: em.Order.courier_id
    }[user.role]

    conditions = [user_id_field == user.id]

    if status is not None:
        conditions.append(em.Order.status == status)

    return session.exec(
        select(em.Order)
        .where(*conditions)
        .offset(offset)
        .limit(limit)
    ).all()


@orders.patch("/{order_id}")
def update_order(
    order_id: int,
    order: em.OrderUpdate,
    session: SessionDep,
    user: UserDep,
    bg_tasks: BackgroundTasks,
    dynamo: DynamoDep
):
    order_db = session.get(em.Order, order_id)

    assert_exists(order_db, order_id, "order")
   
    data = order.model_dump(exclude_unset=True)
    updated_status = data.pop("status", None)

    if updated_status is None:
        return order_db

    # Assert valid state transition
    if updated_status.idx() != order_db.status.idx() + 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid state transition"
        )

    # Assert user role is correct
    if updated_status.idx() <= em.OrderStatus.ready_for_pickup.idx():
        if user.id != order_db.merchant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not your order"
            )
    else:
        if user.id != order_db.courier_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not your order"
            )

    if updated_status == em.OrderStatus.ready_for_pickup:
        bg_tasks.add_task(
            look_for_courier, order_db, order_db.customer.address,
            order_db.merchant.address, session, dynamo
        )
    
    elif updated_status == em.OrderStatus.picked_up:
        dynamo.Table(COURIER_LOCATION_TABLE).put_item(
            Item={
                "Order_ID": order_id,
                "Location": order_db.merchant.address,
            }
        )

    order_db.status = updated_status
    
    event = em.OrderEvent(updated_status=updated_status)
    order_db.events.append(event)

    order_db.sqlmodel_update(data)

    session.add(order_db)
    session.commit()
    session.refresh(order_db)

    return order_db


app.include_router(customers)
app.include_router(merchants)
app.include_router(couriers)
app.include_router(orders)


# OUTRAS ROTAS --------------------------------------------------

@app.get("/health", tags=["Other"])
def health():
    return True
