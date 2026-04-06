# Baseado em https://fastapi.tiangolo.com/tutorial/sql-databases/

from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime

# CUSTOMER -------------------------------------------

class CustomerBase(SQLModel):
    name: str
    email: str
    phone: str
    address: int

class Customer(CustomerBase, table=True):
    id: int | None = Field(default=None, primary_key=True)

class CustomerCreate(CustomerBase):
    ...

class CustomerPublic(CustomerBase):
    id: int

class CustomerUpdate(CustomerBase):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: int | None = None


# COURIER -------------------------------------------

class CourierBase(SQLModel):
    name: str
    location: int
    vehicle_type: str
    availability: bool

class Courier(CourierBase, table=True):
    id: int | None = Field(default=None, primary_key=True)

class CourierCreate(CourierBase):
    ...

class CourierPublic(CourierBase):
    id: int

class CourierUpdate(CourierBase):
    name: str | None = None
    location: int | None = None
    vehicle_type: str | None = None
    availability: bool | None = None


# MERCHANT -------------------------------------------

class MerchantBase(SQLModel):
    name: str
    type: str
    address: int

class Merchant(MerchantBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    items: list["Item"] = Relationship(back_populates="merchant")

class ItemBase(SQLModel):
    name: str
    preparation_time: int
    price: float

class Item(ItemBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    merchant_id: int | None = Field(default=None, foreign_key="merchant.id")
    merchant: Merchant | None = Relationship(back_populates="items")

class ItemPublic(ItemBase):
    id: int

class ItemCreate(ItemBase):
    ...

class MerchantCreate(MerchantBase):
    items: list[ItemCreate]

class MerchantPublic(MerchantBase):
    id: int
    items: list[ItemPublic]

class MerchantUpdate(MerchantBase):
    name: str | None = None
    type: str | None = None
    address: int | None = None
    items: list[ItemCreate] | None = None


# ORDER -------------------------------------------

class OrderList(SQLModel):
    order_id: int = Field(primary_key=True)
    item_id: int = Field(primary_key=True)
    items_quantity: int
    total_price: float

class OrderBase(SQLModel):
    merchant_id: int

class Order(OrderBase, table=True):
    # Explicitar o nome da tabela é necessário pq ORDER é uma palavra reservada
    # no SQL, o q significa que os comandos do SQLModel vão colocar aspas ao
    # redor do nome da classe (Order) e a consulta deixará de ser case-insensitive
    __tablename__ = "ORDER"

    id: int | None = Field(default=None, primary_key=True)
    customer_id: int
    courier_id: int | None
    status: str
    order_time: datetime
    events: list["OrderEvent"] = Relationship(back_populates="order")

class OrderCreate(OrderBase):
    item_ids: list[int]

class OrderEvent(SQLModel, table=True):
    __tablename__ = "order_events"

    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="ORDER.id")
    status_change: str
    change_time: datetime = Field(default_factory=datetime.now)
    order: Order | None = Relationship(back_populates="events")

class DeliveryRoute(SQLModel):
    courier_id: int
    distance_to_merchant: int
    path_to_merchant: list[int]
    distance_to_user: int
    path_to_user: list[int]

    @classmethod
    def from_dynamo(cls, item: dict):
        return cls(
            courier_id=int(item["Courier_ID"]),
            distance_to_merchant=float(item["Distance_To_Merchant"]),
            path_to_merchant=item["Path_To_Merchant"],
            distance_to_user=float(item["Distance_To_User"]),
            path_to_user=item["Path_To_User"],
        )

    def to_dynamo(self) -> dict:
        return {
            "Courier_ID": self.courier_id,
            "Distance_To_Merchant": self.distance_to_merchant,
            "Path_To_Merchant": self.path_to_merchant,
            "Distance_To_User": self.distance_to_user,
            "Path_To_User": self.path_to_user,
        }

class OrderPublic(OrderBase):
    id: int
    customer_id: int
    courier_id: int | None
    status: str
    order_time: datetime
    delivery_route: DeliveryRoute | None = None
    courier_location: int | None = None
