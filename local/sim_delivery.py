"""Módulo para simular a frota de entregadores em tempo real nas ruas"""

import asyncio
import aiohttp
import argparse
import os

# TODO(grupo-api): confirmar endpoints e schema de pedidos/telemetria no OpenAPI final.
# Atualmente este script assume /orders e /locations.


async def update_order_status(
    session: aiohttp.ClientSession, api_url: str, order_id: str, new_status: str
):
    """Atualiza o estado do pedido na API"""
    payload = {"status": new_status}
    await session.patch(f"{api_url.rstrip('/')}/orders/{order_id}/status", json=payload)


async def simulate_courier_route(
    session: aiohttp.ClientSession,
    api_url: str,
    order_id: str,
    courier_id: str,
    route_to_merchant: list,
    route_to_client: list,
):
    """Transita o pedido pelos estados através do trajeto duplo."""

    # 1. Deslocamento do entregador até o restaurante
    for node in route_to_merchant:
        location_payload = {
            "courier_id": courier_id,
            "order_id": order_id,
            "lat": node["lat"],
            "lon": node["lon"],
        }
        await session.post(f"{api_url}/locations", json=location_payload)
        await asyncio.sleep(0.1)  # Telemetria a cada 100ms

    # 2. Entregador retira o pedido no restaurante
    await update_order_status(session, api_url, order_id, "PICKED_UP")
    await update_order_status(session, api_url, order_id, "IN_TRANSIT")

    # 3. Deslocamento do restaurante até o cliente
    for node in route_to_client:
        location_payload = {
            "courier_id": courier_id,
            "order_id": order_id,
            "lat": node["lat"],
            "lon": node["lon"],
        }
        await session.post(f"{api_url}/locations", json=location_payload)
        await asyncio.sleep(0.1)

    # 4. Finaliza a entrega
    await update_order_status(session, api_url, order_id, "DELIVERED")
    print(f"Pedido {order_id} entregue com sucesso pelo entregador {courier_id}!")


async def delivery_worker(api_url: str, username: str | None = None, password: str | None = None):
    """
    Fica monitorando a API em busca de pedidos que já saíram da cozinha
    e já tiveram a rota calculada pelo Worker (Dijkstra)
    """
    auth = None
    username = username or os.getenv("API_USERNAME")
    password = password or os.getenv("API_PASSWORD")
    if username and password:
        auth = aiohttp.BasicAuth(username, password)

    async with aiohttp.ClientSession(auth=auth) as session:
        while True:
            try:
                # busca pedidos prontos para retirada
                async with session.get(
                    f"{api_url.rstrip('/')}/orders?status=READY_FOR_PICKUP"
                ) as response:
                    ready_orders = await response.json()

                # para cada pedido pronto, cria uma task independente para o entregador fazer a rota
                for order in ready_orders:
                    # API deve retornar a rota calculada pelo Worker como uma lista de coordenadas
                    asyncio.create_task(
                        simulate_courier_route(
                            session,
                            api_url,
                            order["order_id"],
                            order["courier_id"],
                            order["route_to_merchant"],
                            order["route_to_client"],
                        )
                    )
            except Exception:
                pass  # Silencia erros de timeout passageiros

            # polling a cada 2 segundos para não sobrecarregar a API atoa
            await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Central de Entregadores DijkFood")
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument("--username", default=None, help="Usuario Basic Auth da API")
    parser.add_argument("--password", default=None, help="Senha Basic Auth da API")
    args = parser.parse_args()

    print(f"Iniciando a central de despachos conectada em: {args.api_url}")
    asyncio.run(delivery_worker(args.api_url, username=args.username, password=args.password))
