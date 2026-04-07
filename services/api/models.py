# Baseado em https://fastapi.tiangolo.com/tutorial/sql-databases/

from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime
from enum import Enum
from typing import Optional, List


class UserRole(str, Enum):
    customer = "customer"
    merchant = "merchant"
    courier  = "courier"

class User(SQLModel, table=True):
    # Explicitar o nome da tabela é necessário pq USER é uma palavra reservada
    # no SQL, o q significa que os comandos do SQLModel vão colocar aspas ao
    # redor do nome da classe (Order) e a consulta deixará de ser case-insensitive
    __tablename__ = "user"

    id: int = Field(primary_key=True)
    role: UserRole

    merchant: "Merchant" = Relationship(back_populates="user")
    customer: "Customer" = Relationship(back_populates="user")
    courier: "Courier" = Relationship(back_populates="user")


# CUSTOMER -------------------------------------------

class CustomerBase(SQLModel):
    name: str
    email: str
    phone: str
    address: int

class Customer(CustomerBase, table=True):
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    user: User = Relationship(back_populates="customer")
    orders: List["Order"] = Relationship(back_populates="customer")

class CustomerCreate(CustomerBase):
    ...

class CustomerPublic(CustomerBase):
    user_id: int

class CustomerUpdate(CustomerBase):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[int] = None


# COURIER -------------------------------------------

class CourierBase(SQLModel):
    name: str
    location: int
    vehicle_type: str
    availability: bool

class Courier(CourierBase, table=True):
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    user: User = Relationship(back_populates="courier")

class CourierCreate(CourierBase):
    ...

class CourierPublic(CourierBase):
    user_id: int

class CourierUpdate(CourierBase):
    name: Optional[str] = None
    location: Optional[int] = None
    vehicle_type: Optional[str] = None
    availability: Optional[bool] = None


# MERCHANT/ITEM ---------------------------------------

class MerchantBase(SQLModel):
    name: str
    type: str
    address: int

class Merchant(MerchantBase, table=True):
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    user: User = Relationship(back_populates="merchant")
    items: List["Item"] = Relationship(back_populates="merchant")
    orders: List["Order"] = Relationship(back_populates="merchant")

class ItemBase(SQLModel):
    name: str
    preparation_time: int
    price: float

class Item(ItemBase, table=True):
    id: int = Field(default=None, primary_key=True)
    merchant_id: int = Field(default=None, foreign_key="merchant.user_id")
    merchant: Merchant = Relationship(back_populates="items")

class ItemPublic(ItemBase):
    id: int

class ItemCreate(ItemBase):
    ...

class MerchantCreate(MerchantBase):
    items: List[ItemCreate]

class MerchantPublic(MerchantBase):
    user_id: int
    items: List[ItemPublic]

class MerchantUpdate(MerchantBase):
    name: Optional[str] = None
    type: Optional[str] = None
    address: Optional[int] = None
    items: List[Optional[ItemCreate]] = None


# DYNAMODB TYPES ------------------------------------------

class DeliveryRoute(SQLModel):
    courier_id: int
    distance_to_merchant: int
    path_to_merchant: List[int]
    distance_to_user: int
    path_to_user: List[int]

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

class CourierLocation(SQLModel):
    location: int

    @classmethod
    def from_dynamo(cls, item: dict):
        return cls(location=int(item["Location"]))

    def to_dynamo(self) -> dict:
        return { "Location": self.location }


# ORDER --------------------------------------------------

class OrderStatus(Enum):
    confirmed        = "confirmed"
    preparing        = "preparing"
    ready_for_pickup = "ready_for_pickup"
    picked_up        = "picked_up"
    in_transit       = "in_transit"
    delivered        = "delivered"

    def idx(self):
        order = list(OrderStatus)
        return order.index(self)

class OrderItem(SQLModel):
    __tablename__ = "order_item"

    order_id: int = Field(primary_key=True)
    item_id: int = Field(primary_key=True)
    items_quantity: int
    total_price: float

class OrderBase(SQLModel):
    ...

class Order(OrderBase, table=True):
    # Ver classe User ^
    __tablename__ = "order"

    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: int = Field(foreign_key="merchant.user_id")
    customer_id: int = Field(foreign_key="customer.user_id")
    courier_id: Optional[int]
    status: OrderStatus
    order_time: datetime = Field(default_factory=datetime.now)
    events: List["OrderEvent"] = Relationship(back_populates="order")

    merchant: Merchant = Relationship(back_populates="orders")
    customer: Customer = Relationship(back_populates="orders")

class OrderCreate(OrderBase):
    merchant_id: int
    item_ids: List[int]

class OrderEvent(SQLModel, table=True):
    __tablename__ = "order_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id")
    updated_status: OrderStatus
    change_time: datetime = Field(default_factory=datetime.now)
    order: Order = Relationship(back_populates="events")

class OrderPublic(OrderBase):
    id: int
    merchant_id: int
    customer_id: int
    courier_id: Optional[int]
    status: str
    order_time: datetime = Field(default_factory=datetime.now)

class OrderPublicComplete(OrderPublic):
    delivery_route: Optional[DeliveryRoute] = None
    courier_location: Optional[CourierLocation] = None

class OrderUpdate(OrderBase):
    status: Optional[OrderStatus] = None
