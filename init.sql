CREATE TYPE user_role AS ENUM (
    'customer',
    'merchant',
    'courier'
);

CREATE TYPE order_status AS ENUM (
    'confirmed',
    'preparing',
    'ready_for_pickup',
    'picked_up',
    'in_transit',
    'delivered'
);

-- Tabela de Usuários
CREATE TABLE "user" (
    id SERIAL PRIMARY KEY,
    role user_role NOT NULL
);

-- Tabela de Clientes
CREATE TABLE customer (
    user_id INTEGER PRIMARY KEY REFERENCES "user" (id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) NOT NULL,
    address INTEGER NOT NULL -- Armazena o ID do Nó do grafo (OSMNX)
);

-- Tabela de Restaurantes (Merchants)
CREATE TABLE merchant (
    user_id INTEGER PRIMARY KEY REFERENCES "user" (id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(100) NOT NULL, -- Ex: Italiana, Japonesa
    address INTEGER NOT NULL -- Armazena o ID do Nó do grafo
);

-- Tabela de Entregadores (Couriers)
CREATE TABLE courier (
    user_id INTEGER PRIMARY KEY REFERENCES "user" (id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    vehicle_type VARCHAR(50) NOT NULL, -- Ex: Moto, Bicicleta
    location INTEGER NOT NULL, -- ID do Nó inicial/atual
    availability BOOLEAN -- TRUE = AVAILABLE, FALSE = BUSY
);

-- Tabela de Itens (Cardápio)
-- Regra: Um item está atrelado a exatamente 1 restaurante
CREATE TABLE item (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    merchant_id INTEGER NOT NULL REFERENCES merchant (user_id),
    preparation_time INTEGER NOT NULL, -- Tempo somado para definir transição de status
    price DECIMAL(10, 2) NOT NULL
);

-- Tabela de Pedidos (Orders)
-- Regra: O Courier_ID é opcional (NULL) até o estado READY_FOR_PICKUP
CREATE TABLE "order" (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customer (user_id),
    merchant_id INTEGER NOT NULL REFERENCES merchant (user_id),
    courier_id INTEGER REFERENCES courier (user_id) NULL,
    status order_status NOT NULL,
    order_time TIMESTAMP
);

-- Tabela de Itens do Pedido (N:N entre Order e Item)
-- Regra: Um cliente pode pedir uma lista de itens de um merchant
CREATE TABLE order_item (
    order_id INTEGER NOT NULL REFERENCES "order" (id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES item (id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    total_price DECIMAL(10, 2) NOT NULL,
    PRIMARY KEY (order_id, item_id)
);

-- Tabela de Histórico de Eventos (Para consulta do Administrador)
CREATE TABLE order_event (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES "order" (id) ON DELETE CASCADE,
    updated_status order_status NOT NULL,
    change_time TIMESTAMP
);

-- Índices para Performance em Alta Carga (200 req/s)
CREATE INDEX idx_courier_availability ON courier (availability);
CREATE INDEX idx_order_status ON "order" (status);
CREATE INDEX idx_events_order_time ON order_event (order_id, change_time);
