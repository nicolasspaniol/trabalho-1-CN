"""Módulo para simular a frota de entregadores em tempo real nas ruas"""

import asyncio
import aiohttp


async def update_order_status(
    session: aiohttp.ClientSession, api_url: str, order_id: str, new_status: str
):
    """Atualiza o estado do pedido na API"""
    payload = {"status": new_status}
    await session.patch(f"{api_url}/orders/{order_id}/status", json=payload)


async def simulate_courier_route(
    session: aiohttp.ClientSession,
    api_url: str,
    order_id: str,
    courier_id: str,
    route_nodes: list,
):
    """
    Transita o pedido pelos estados finais e envia a telemetria do GPS (DynamoDB)
    """
    # 1. Entregador aceita e retira o pedido
    await update_order_status(session, api_url, order_id, "PICKED_UP")
    await update_order_status(session, api_url, order_id, "IN_TRANSIT")

    # 2. Simulação física de deslocamento
    for node in route_nodes:
        location_payload = {
            "courier_id": courier_id,
            "order_id": order_id,
            "lat": node["lat"],
            "lon": node["lon"],
        }

        # Envia coordenada atual para rastreamento em tempo real
        await session.post(f"{api_url}/locations", json=location_payload)

        # Reportar a cada 100ms
        await asyncio.sleep(0.1)

    # 3. Finaliza a entrega
    await update_order_status(session, api_url, order_id, "DELIVERED")
    print(f"Pedido {order_id} entregue com sucesso pelo entregador {courier_id}!")


async def delivery_worker(api_url: str):
    """
    Fica monitorando a API em busca de pedidos que já saíram da cozinha
    e já tiveram a rota calculada pelo Worker (Dijkstra)
    """
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # busca pedidos prontos para retirada
                async with session.get(
                    f"{api_url}/orders?status=READY_FOR_PICKUP"
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
                            order["route"],
                        )
                    )
            except Exception as e:
                print(f"Erro ao buscar pedidos (A API caiu?): {e}")

            # polling a cada 2 segundos para não sobrecarregar a API atoa
            await asyncio.sleep(2)


if __name__ == "__main__":
    API_BASE_URL = "http://localhost:8000"  # TODO: Substituir pela URL do ALB
    print("Iniciando a central de despachos dos entregadores...")
    asyncio.run(delivery_worker(API_BASE_URL))
