-- 1. Tabela de Clientes
CREATE TABLE CUSTOMER (
    Customer_ID SERIAL PRIMARY KEY,
    Name VARCHAR(255) NOT NULL,
    Email VARCHAR(255) UNIQUE NOT NULL,
    Phone VARCHAR(20) NOT NULL,
    Address INTEGER NOT NULL -- Armazena o ID do Nó do grafo (OSMNX)
);

-- 2. Tabela de Restaurantes (Merchants)
CREATE TABLE MERCHANT (
    Merchant_ID SERIAL PRIMARY KEY,
    Name VARCHAR(255) NOT NULL,
    Type VARCHAR(100) NOT NULL, -- Ex: Italiana, Japonesa
    Address INTEGER NOT NULL -- Armazena o ID do Nó do grafo
);

-- 3. Tabela de Itens (Cardápio)
-- Regra: Um item está atrelado a exatamente 1 restaurante
CREATE TABLE ITEM (
    Item_ID SERIAL PRIMARY KEY,
    Name VARCHAR(255) NOT NULL,
    Merchant_ID INTEGER NOT NULL REFERENCES MERCHANT(Merchant_ID),
    Preparation_Time INTEGER NOT NULL, -- Tempo somado para definir transição de status
    Price DECIMAL(10, 2) NOT NULL
);

-- 4. Tabela de Entregadores (Couriers)
CREATE TABLE COURIER (
    Courier_ID SERIAL PRIMARY KEY,
    Name VARCHAR(255) NOT NULL,
    Vehicle_Type VARCHAR(50) NOT NULL, -- Ex: Moto, Bicicleta
    Location INTEGER NOT NULL, -- ID do Nó inicial/atual
    Availability BOOLEAN NOT NULL DEFAULT TRUE -- TRUE = AVAILABLE, FALSE = BUSY
);

-- 5. Tabela de Pedidos (Orders)
-- Regra: O Courier_ID é opcional (NULL) até o estado READY_FOR_PICKUP
CREATE TABLE ORDERS (
    Order_ID SERIAL PRIMARY KEY,
    Customer_ID INTEGER NOT NULL REFERENCES CUSTOMER (Customer_ID),
    Merchant_ID INTEGER NOT NULL REFERENCES MERCHANT(Merchant_ID),
    Courier_ID INTEGER REFERENCES COURIER(Courier_ID) NULL,
    Status VARCHAR(50) NOT NULL DEFAULT 'CONFIRMED',
    Order_Time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Tabela de Itens do Pedido (N:N entre Order e Item)
-- Regra: Um cliente pode pedir uma lista de itens de um merchant
CREATE TABLE ORDER_LIST (
    Order_ID INTEGER NOT NULL REFERENCES ORDERS(Order_ID) ON DELETE CASCADE,
    Item_ID INTEGER NOT NULL REFERENCES ITEM(Item_ID),
    Items_Quantity INTEGER NOT NULL CHECK (Items_Quantity > 0),
    Total_Price DECIMAL(10, 2) NOT NULL,
    PRIMARY KEY (Order_ID, Item_ID)
);

-- 7. Tabela de Histórico de Eventos (Para consulta do Administrador)
CREATE TABLE ORDER_EVENTS (
    Event_ID SERIAL PRIMARY KEY,
    Order_ID INTEGER NOT NULL REFERENCES ORDERS(Order_ID) ON DELETE CASCADE,
    Status_Change VARCHAR(50) NOT NULL,
    Change_Time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Índices para Performance em Alta Carga (200 req/s)
CREATE INDEX idx_courier_availability ON COURIER(Availability);
CREATE INDEX idx_order_status ON ORDERS(Status);
CREATE INDEX idx_events_order_time ON ORDER_EVENTS(Order_ID, Change_Time);