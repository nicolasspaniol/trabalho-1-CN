url="http://127.0.0.1:8000"
url_loc="http://127.0.0.1:8050"

req() {
  echo "----------------" >&2
  sleep .5
  echo "$3" >&2
  res=$(curl -s -H 'Content-Type: application/json' "$@")
  echo "$res" | jq -C >&2
  echo "$res"
}

get() {
  echo "$1" | jq -r "$2"
}

# Criar cliente (ADMIN)
email="customer$RANDOM@email"
res=$(req -X POST "$url/customers/" \
  -d "{\"name\":\"nome\",\"email\":\"$email\",\"phone\":\"019239238\",\"address\":10}" \
  -u admin:123)

customer_id=$(get "$res" '.user_id')
echo "ID: $customer_id"

# Criar restaurante (ADMIN)
res=$(req -X POST "$url/merchants/" \
  -d '{"name":"los pollos hermanos","type":"mexicana","address":20,"items":[{"name":"taco","preparation_time":10,"price":14}]}' \
  -u admin:123)

merchant_id=$(get "$res" '.user_id')
echo "ID: $merchant_id"

# Criar entregador (ADMIN)
res=$(req -X POST "$url/couriers/" \
  -d '{"name":"jame","location":50,"vehicle_type":"carro","availability":true}' \
  -u admin:123)

courier_id=$(get "$res" '.user_id')
echo "ID: $courier_id"

# Criar pedido (CLIENTE)
res=$(req -X POST "$url/orders/" \
  -d "{\"merchant_id\": $merchant_id, \"item_ids\": [1]}" \
  -u "$customer_id":)

order_id=$(get "$res" '.id')

# Pedido aceito (RESTAURANTE)
req -X PATCH "$url/orders/$order_id" \
  -d '{"status":"preparing"}' \
  -u "$merchant_id":

# Pedido pronto (RESTAURANTE)
req -X PATCH "$url/orders/$order_id" \
  -d '{"status":"ready_for_pickup"}' \
  -u "$merchant_id":

# Ver todos os pedidos feitos (CLIENTE)
req -X GET "$url/orders/" \
  -u "$customer_id":

# Ver pedido atribuido (ENTREGADOR)
req -X GET "$url/orders/?status=ready_for_pickup" \
  -u "$courier_id":

# Pedido recolhido (ENTREGADOR)
req -X PATCH "$url/orders/$order_id" \
  -d '{"status":"picked_up"}' \
  -u "$courier_id":

# Pedido em trânsito (ENTREGADOR)
req -X PATCH "$url/orders/$order_id" \
  -d '{"status":"in_transit"}' \
  -u "$courier_id":

# Atualiza posição (ENTREGADOR)
req -X PUT "$url_loc/couriers/me/location?order_id=$order_id" \
  -d '{"location": 23}' \
  -u "$courier_id":

req -X PUT "$url_loc/couriers/me/location?order_id=$order_id" \
  -d '{"location": 28}' \
  -u "$courier_id":

# Consultar entrega (CLIENTE)
req -X GET "$url/orders/$order_id" \
  -u "$customer_id":

# Atualiza posição (ENTREGADOR)
req -X PUT "$url_loc/couriers/me/location?order_id=$order_id" \
  -d '{"location": 43}' \
  -u "$courier_id":

# Pedido entregue (ENTREGADOR)
req -X PATCH "$url/orders/$order_id" \
  -d '{"status":"delivered"}' \
  -u "$courier_id":

# Ver todos os pedidos feitos (CLIENTE)
req -X GET "$url/orders/" \
  -u "$customer_id":
