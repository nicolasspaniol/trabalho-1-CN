"""Módulo para simular o tráfego de clientes gerando pedidos na plataforma"""

import asyncio
import aiohttp
import time
import random
import numpy as np


async def place_order(
    session: aiohttp.ClientSession, api_url: str, user_id: str, merchant_id: str
):
    """
    Gera e envia um pedido individual e
    Mede a latência exata da requisição para o relatório de desempenho
    """
    # Poisson para o número de itens (maioria pede poucos, alguns pedem muitos)
    num_items = 1 + np.random.poisson(1.5)
    items_mock = [f"item_{i}" for i in range(num_items)]

    payload = {
        "user_id": user_id,
        "merchant_id": merchant_id,
        "items": items_mock,
    }

    start_time = time.perf_counter()
    # Envia o pedido e aguarda a resposta para medir a latência real
    try:
        # aguarda resposta e retorna a latência da requisição
        async with session.post(f"{api_url}/orders", json=payload) as response:
            await response.json()
            latency = time.perf_counter() - start_time
            return latency
    except Exception as e:
        print(f"Erro na requisição: {e}")
        return None


async def run_load_test(
    api_url: str, rps: int, duration: int, users: list, merchants: list
):
    """
    Executa o teste de carga compensando o tempo de processamento
    para manter o throughput alvo (10, 50 ou 200 req/s).
    """
    latencies = []

    # Remove limite local de conexões simultâneas do aiohttp
    connector = aiohttp.TCPConnector(limit=0)

    async with aiohttp.ClientSession(connector=connector) as session:
        print(f"Iniciando teste de carga: {rps} RPS por {duration} segundos...")

        for _ in range(duration):
            loop_start = time.perf_counter()

            # Dispara n (rps) requisições concorrentes neste segundo
            tasks = [
                place_order(
                    session, api_url, random.choice(users), random.choice(merchants)
                )
                for _ in range(rps)
            ]

            results = await asyncio.gather(*tasks)
            latencies.extend([lat for lat in results if lat is not None])

            # Compensa o tempo gasto no disparo para fechar exatamente 1.0 segundo
            elapsed = time.perf_counter() - loop_start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    # relatório de desempenho
    if latencies:
        p95 = np.percentile(latencies, 95) * 1000  # para milissegundos
        print(f"\n--- Resultados do Cenário ({rps} RPS) ---")
        print(f"Total de requisições enviadas: {len(latencies)}")
        print(f"Latência P95: {p95:.2f} ms")
        if p95 < 500:
            print("P95 < 500ms")
        else:
            print("P95 >= 500ms!!! Verificar gargalos.")


if __name__ == "__main__":
    API_BASE_URL = "http://localhost:8000"  # TODO: Substituir pela URL do ALB

    # TODO: Na integração real, carregar essas listas dando um GET /users e GET /merchants
    mock_users = [f"user_{i}" for i in range(10)]
    mock_merchants = [f"merch_{i}" for i in range(1)]

    # Executa o cenário "Evento Especial" (200 rps por 30 segundos)
    asyncio.run(
        run_load_test(
            API_BASE_URL,
            rps=200,
            duration=30,
            users=mock_users,
            merchants=mock_merchants,
        )
    )
