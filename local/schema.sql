-- Schema alinhado com os modelos SQLModel em services/api/models.py

DROP TABLE IF EXISTS order_item CASCADE;
DROP TABLE IF EXISTS order_event CASCADE;
DROP TABLE IF EXISTS "order" CASCADE;
DROP TABLE IF EXISTS item CASCADE;
DROP TABLE IF EXISTS merchant CASCADE;
DROP TABLE IF EXISTS courier CASCADE;
DROP TABLE IF EXISTS customer CASCADE;
DROP TABLE IF EXISTS "user" CASCADE;

CREATE TABLE IF NOT EXISTS "user" (
    id SERIAL PRIMARY KEY,
    role VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS customer (
    user_id INTEGER PRIMARY KEY REFERENCES "user"(id),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(50) NOT NULL,
    address BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS courier (
    user_id INTEGER PRIMARY KEY REFERENCES "user"(id),
    name VARCHAR(255) NOT NULL,
    location BIGINT NOT NULL,
    vehicle_type VARCHAR(50) NOT NULL,
    availability BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant (
    user_id INTEGER PRIMARY KEY REFERENCES "user"(id),
    name VARCHAR(255) NOT NULL,
    type VARCHAR(100) NOT NULL,
    address BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS item (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    preparation_time INTEGER NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    merchant_id INTEGER NOT NULL REFERENCES merchant(user_id)
);

CREATE TABLE IF NOT EXISTS "order" (
    id SERIAL PRIMARY KEY,
    merchant_id INTEGER NOT NULL REFERENCES merchant(user_id),
    customer_id INTEGER NOT NULL REFERENCES customer(user_id),
    courier_id INTEGER,
    status VARCHAR(50) NOT NULL,
    order_time TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS order_event (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES "order"(id) ON DELETE CASCADE,
    updated_status VARCHAR(50) NOT NULL,
    change_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_item (
    order_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    items_quantity INTEGER NOT NULL,
    total_price DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (order_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_courier_availability ON courier(availability);
CREATE INDEX IF NOT EXISTS idx_order_status ON "order"(status);
CREATE INDEX IF NOT EXISTS idx_order_event_order_time ON order_event(order_id, change_time);