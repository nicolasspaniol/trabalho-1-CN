"""Módulo para simular o tráfego de clientes gerando carga e provando isolamento"""

import asyncio
import aiohttp
import time
import random
import numpy as np
import argparse


async def place_order(
    session: aiohttp.ClientSession, api_url: str, user_id: str, merchant_id: str
):
    """Envia um pedido e mede a latência de escrita"""
    num_items = 1 + np.random.poisson(1.5)
    items_mock = [f"item_{i}" for i in range(num_items)]
    payload = {"user_id": user_id, "merchant_id": merchant_id, "items": items_mock}

    start_time = time.perf_counter()
    # Envia o POST para criar um pedido e espera a resposta para medir a latência de escrita
    try:
        async with session.post(f"{api_url}/orders", json=payload) as response:
            result = await response.json()
            latency = time.perf_counter() - start_time
            # Assume que a API retorna o order_id criado
            return latency, result.get("order_id", "dummy_id")
    except Exception:
        return None, None


async def check_order_status(
    session: aiohttp.ClientSession, api_url: str, order_id: str
):
    """Consulta um pedido específico para medir a latência de leitura sob estresse"""
    start_time = time.perf_counter()
    try:
        async with session.get(f"{api_url}/orders/{order_id}") as response:
            await response.json()
            return time.perf_counter() - start_time
    except Exception:
        return None


async def fetch_entity_ids(api_url: str, endpoint: str, id_field: str):
    """
    Faz um GET na API para buscar todos os registros e extrai apenas a lista de IDs.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{api_url}/{endpoint}") as response:
                response.raise_for_status()  # Garante que não deu erro 500/404
                data = await response.json()
                # Extrai o ID de cada registro retornado pela API
                return [item[id_field] for item in data]
        except Exception as e:
            print(f"Erro ao buscar {endpoint} na API: {e}")
            return []


async def run_load_test(
    api_url: str, rps: int, duration: int, users: list, merchants: list
):
    """Executa o teste de carga gerando tráfego de pedidos e leituras concorrentes"""
    write_latencies = []
    read_latencies = []
    active_orders = ["dummy_id"]  # Usado para os primeiros GETs até popularem os reais

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        print(
            f"Iniciando teste de carga: {rps} RPS de Escrita + Leituras concorrentes por {duration}s..."
        )

        for _ in range(duration):
            loop_start = time.perf_counter()

            # Dispara N requisições de criação de pedido
            write_tasks = [
                place_order(
                    session, api_url, random.choice(users), random.choice(merchants)
                )
                for _ in range(rps)
            ]

            # Dispara requisições concorrentes de leitura para provar isolamento do banco
            read_tasks = [
                check_order_status(session, api_url, random.choice(active_orders))
                for _ in range(max(1, rps // 5))
            ]

            write_results = await asyncio.gather(*write_tasks)
            read_results = await asyncio.gather(*read_tasks)

            for w_lat, o_id in write_results:
                if w_lat is not None:
                    write_latencies.append(w_lat)
                    if o_id != "dummy_id":
                        active_orders.append(o_id)

            read_latencies.extend(
                [r_lat for r_lat in read_results if r_lat is not None]
            )

            elapsed = time.perf_counter() - loop_start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    # Relatório de Isolamento (Avaliação Crítica do Professor)
    if write_latencies and read_latencies:
        p95_write = np.percentile(write_latencies, 95) * 1000
        p95_read = np.percentile(read_latencies, 95) * 1000

        print(f"\n--- Resultados do Cenário ({rps} RPS) ---")
        print(f"Latência de Roteamento (Escrita) P95: {p95_write:.2f} ms")
        print(f"Latência de Consulta (Leitura) P95:  {p95_read:.2f} ms")

        if p95_write < 500 and p95_read < 500:
            print("SUCESSO: Requisito P95 < 500ms alcançado.")
        else:
            print("ALERTA: Gargalo de performance detectado.")


async def main(args):
    print(f"Conectando na API {args.api_url} para buscar entidades cadastradas...")

    # ATENÇÃO: O nome do campo (ex: 'user_id' ou apenas 'id') depende de como
    # o SP construiu o schema de resposta lá no FastAPI. Alinhe isso com ele!
    users = await fetch_entity_ids(args.api_url, "users", "user_id")
    merchants = await fetch_entity_ids(args.api_url, "merchants", "merchant_id")

    if not users or not merchants:
        print("ERRO CRÍTICO: Não foi possível carregar usuários ou restaurantes.")
        print("Você tem certeza de que rodou o 'load.py' antes para popular o banco?")
        return

    print(
        f"Sucesso! {len(users)} usuários e {len(merchants)} restaurantes carregados na memória."
    )

    # Inicia o bombardeio estocástico
    await run_load_test(args.api_url, args.rps, args.duration, users, merchants)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gerador de Carga DijkFood")
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument("--rps", type=int, default=50, help="Requisições por segundo")
    parser.add_argument(
        "--duration", type=int, default=30, help="Duração do teste em segundos"
    )
    args = parser.parse_args()

    # Passa o controle para a função principal assíncrona
    asyncio.run(main(args))
