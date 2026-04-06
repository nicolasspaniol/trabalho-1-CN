alias curl="sleep .1; curl -s"

# Criar cliente
curl -X 'POST' \
  'http://127.0.0.1:8000/customers/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "nome",
  "email": "customer@email",
  "phone": "019239238",
  "address": 10
}' -u admin:123 | jq

# Criar restaurante
curl -X 'POST' \
  'http://127.0.0.1:8000/merchants/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "los pollos hermanos",
  "type": "mexicana",
  "address": 20,
  "items": [
    {
      "name": "taco",
      "preparation_time": 10,
      "price": 14
    }
  ]
}' -u admin:123 | jq

# Criar entregador
curl -X 'POST' \
  'http://127.0.0.1:8000/couriers/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "jame",
  "location": 50,
  "vehicle_type": "carro",
  "availability": true
}' -u admin:123 | jq

# Fazer pedido (CLIENTE)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "merchant_id": 1,
  "item_ids": [1]
}' -u 1: | jq

# Pedido aceito (RESTAURANTE)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/1/accept' \
  -H 'accept: application/json' \
  -u 1: | jq

# Pedido pronto (RESTAURANTE)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/1/ready' \
  -H 'accept: application/json' \
  -u 1: | jq

# Ver pedido atribuido (ENTREGADOR)
curl -X 'GET' \
  'http://127.0.0.1:8000/couriers/me/order' \
  -H 'accept: application/json' \
  -u 1: | jq

# Pedido recolhido (ENTREGADOR)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/1/picked_up' \
  -H 'accept: application/json' \
  -u 1: | jq

# Pedido em trânsito (ENTREGADOR)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/1/in_transit' \
  -H 'accept: application/json' \
  -u 1: | jq

# Atualiza posição (ENTREGADOR)
curl -X 'PUT' \
  'http://127.0.0.1:8000/couriers/me/location?location=23' \
  -H 'accept: application/json' \
  -u 1: | jq

# Atualiza posição (ENTREGADOR)
curl -X 'PUT' \
  'http://127.0.0.1:8000/couriers/me/location?location=31' \
  -H 'accept: application/json' \
  -u 1: | jq

# Consulta entrega (CLIENTE)
curl -X 'GET' \
  'http://127.0.0.1:8000/orders/1' \
  -H 'accept: application/json' \
  -u 1: | jq

# Atualiza posição (ENTREGADOR)
curl -X 'PUT' \
  'http://127.0.0.1:8000/couriers/me/location?location=8' \
  -H 'accept: application/json' \
  -u 1: | jq

# Pedido entregue (ENTREGADOR)
curl -X 'POST' \
  'http://127.0.0.1:8000/orders/1/delivered' \
  -H 'accept: application/json' \
  -u 1: | jq
