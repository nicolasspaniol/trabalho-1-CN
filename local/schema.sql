-- Schema alinhado com os modelos SQLModel atuais em shared/shared/entity_models.py

-- Limpa estruturas antigas/legadas para evitar conflito de colunas (ex.: courier_id vs id).
-- Para o contexto de simulacao, este reset do schema e intencional.
DROP TABLE IF EXISTS order_events CASCADE;
DROP TABLE IF EXISTS order_list CASCADE;
DROP TABLE IF EXISTS "ORDER" CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS item CASCADE;
DROP TABLE IF EXISTS merchant CASCADE;
DROP TABLE IF EXISTS courier CASCADE;
DROP TABLE IF EXISTS customer CASCADE;

-- 1) customer
CREATE TABLE IF NOT EXISTS customer (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(50) NOT NULL,
    address BIGINT NOT NULL
);

-- 2) courier
CREATE TABLE IF NOT EXISTS courier (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    location BIGINT NOT NULL,
    vehicle_type VARCHAR(50) NOT NULL,
    availability BOOLEAN NOT NULL
);

-- 3) merchant
CREATE TABLE IF NOT EXISTS merchant (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(100) NOT NULL,
    address BIGINT NOT NULL
);

-- 4) item
CREATE TABLE IF NOT EXISTS item (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    preparation_time INTEGER NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    merchant_id INTEGER REFERENCES merchant(id)
);

-- 5) "ORDER" (nome explicito no modelo por causa da palavra reservada)
CREATE TABLE IF NOT EXISTS "ORDER" (
    id SERIAL PRIMARY KEY,
    merchant_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    courier_id INTEGER,
    status VARCHAR(50) NOT NULL,
    order_time TIMESTAMP NOT NULL,
    CONSTRAINT fk_order_customer FOREIGN KEY (customer_id) REFERENCES customer(id),
    CONSTRAINT fk_order_merchant FOREIGN KEY (merchant_id) REFERENCES merchant(id),
    CONSTRAINT fk_order_courier FOREIGN KEY (courier_id) REFERENCES courier(id)
);

-- 6) order_events
CREATE TABLE IF NOT EXISTS order_events (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES "ORDER"(id) ON DELETE CASCADE,
    status_change VARCHAR(50) NOT NULL,
    change_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 7) order_list (estrutura de apoio N:N, ainda que nao usada diretamente na API atual)
CREATE TABLE IF NOT EXISTS order_list (
    order_id INTEGER NOT NULL REFERENCES "ORDER"(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES item(id),
    items_quantity INTEGER NOT NULL CHECK (items_quantity > 0),
    total_price DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (order_id, item_id)
);

-- Índices para consultas frequentes
CREATE INDEX IF NOT EXISTS idx_courier_availability ON courier(availability);
CREATE INDEX IF NOT EXISTS idx_order_status ON "ORDER"(status);
CREATE INDEX IF NOT EXISTS idx_events_order_time ON order_events(order_id, change_time);