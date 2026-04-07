"""Módulo para simular o tráfego de clientes gerando carga e provando isolamento"""

import asyncio
import aiohttp
import time
import random
import numpy as np
import argparse
import os

CUSTOMERS_ENDPOINT = "/customers/"
MERCHANTS_ENDPOINT = "/merchants/"
ORDERS_ENDPOINT = "/orders/"


def make_user_auth(user_id: int) -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(str(user_id), "x")


async def transition_order_for_dispatch(
    session: aiohttp.ClientSession,
    api_url: str,
    order_id: int,
    merchant_id: int,
) -> bool:
    try:
        accept_url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}/accept"
        async with session.post(accept_url, json={}, auth=make_user_auth(merchant_id)) as response:
            response.raise_for_status()

        ready_url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}/ready"
        async with session.post(ready_url, json={}, auth=make_user_auth(merchant_id)) as response:
            response.raise_for_status()

        return True
    except Exception:
        return False


async def place_order(
    session: aiohttp.ClientSession, api_url: str, customer_id: int, merchant_id: int, default_item_id: int
):
    """Envia um pedido e mede a latência de escrita"""
    # TODO(grupo-api): confirmar o campo de retorno do pedido criado (id vs order_id) e a regra final de item_ids.
    payload = {"merchant_id": merchant_id, "item_ids": [default_item_id]}

    start_time = time.perf_counter()
    # Envia o POST para criar um pedido e espera a resposta para medir a latência de escrita
    try:
        url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}"
        async with session.post(url, json=payload, auth=make_user_auth(customer_id)) as response:
            response.raise_for_status()
            result = await response.json()
            order_id = int(result.get("id", result.get("order_id", 0)) or 0)
            write_latency = time.perf_counter() - start_time

            if order_id and not await transition_order_for_dispatch(session, api_url, order_id, merchant_id):
                return None, None, None

            return write_latency, order_id, customer_id
    except Exception:
        return None, None, None


async def check_order_status(
    session: aiohttp.ClientSession, api_url: str, order_id: int, customer_id: int
):
    """Consulta eventos do pedido para medir a latência de leitura sob estresse"""
    start_time = time.perf_counter()
    try:
        url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}"
        async with session.get(url, auth=make_user_auth(customer_id)) as response:
            response.raise_for_status()
            await response.json()
            return time.perf_counter() - start_time
    except Exception:
        return None


async def fetch_entity_ids(api_url: str, endpoint: str, auth: aiohttp.BasicAuth | None):
    """
    Faz um GET na API para buscar todos os registros e extrai apenas a lista de IDs.
    """
    async with aiohttp.ClientSession(auth=auth) as session:
        try:
            url = f"{api_url.rstrip('/')}{endpoint}"
            async with session.get(url) as response:
                response.raise_for_status()  # Garante que não deu erro 500/404
                data = await response.json()
                return [item["id"] for item in data if "id" in item]
        except Exception as e:
            print(f"Erro ao buscar {endpoint} na API: {e}")
            return []


async def run_load_test(
    api_url: str, rps: int, duration: int, customers: list, merchants: list, default_item_id: int, auth: aiohttp.BasicAuth | None
):
    """Executa o teste de carga gerando tráfego de pedidos e leituras concorrentes"""
    write_latencies = []
    read_latencies = []
    active_orders: list[tuple[int, int]] = []
    write_attempts = 0
    successful_writes = 0
    read_attempts = 0
    successful_reads = 0
    test_start = time.perf_counter()

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector, auth=auth) as session:
        print(
            f"Iniciando teste de carga: {rps} RPS de Escrita + Leituras concorrentes por {duration}s..."
        )

        for _ in range(duration):
            loop_start = time.perf_counter()

            # Dispara N requisições de criação de pedido
            write_tasks = [
                place_order(
                    session,
                    api_url,
                    random.choice(customers),
                    random.choice(merchants),
                    default_item_id,
                )
                for _ in range(rps)
            ]
            write_attempts += len(write_tasks)

            # Dispara requisições concorrentes de leitura para provar isolamento do banco
            read_tasks = []
            if active_orders:
                sampled_orders = [random.choice(active_orders) for _ in range(max(1, rps // 5))]
                read_tasks = [
                    check_order_status(session, api_url, order_id, owner_customer_id)
                    for order_id, owner_customer_id in sampled_orders
                ]
                read_attempts += len(read_tasks)

            write_results = await asyncio.gather(*write_tasks)
            read_results = await asyncio.gather(*read_tasks) if read_tasks else []

            for w_lat, o_id, owner_customer_id in write_results:
                if w_lat is not None:
                    write_latencies.append(w_lat)
                    successful_writes += 1
                    if o_id:
                        active_orders.append((o_id, owner_customer_id))

            valid_reads = [r_lat for r_lat in read_results if r_lat is not None]
            read_latencies.extend(valid_reads)
            successful_reads += len(valid_reads)

            elapsed = time.perf_counter() - loop_start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    total_elapsed = max(1e-9, time.perf_counter() - test_start)
    effective_write_rps = successful_writes / total_elapsed
    effective_read_rps = successful_reads / total_elapsed

    print(f"\n--- Resumo de Throughput ({rps} RPS alvo) ---")
    print(f"Escritas: {successful_writes}/{write_attempts} sucesso | RPS efetivo: {effective_write_rps:.2f}")
    if read_attempts:
        print(f"Leituras: {successful_reads}/{read_attempts} sucesso | RPS efetivo: {effective_read_rps:.2f}")
    else:
        print("Leituras: sem amostras (sem pedidos ativos no inicio do teste)")

    # Relatório de isolamento e latência P95
    if write_latencies:
        p95_write = np.percentile(write_latencies, 95) * 1000

        print(f"\n--- Resultados do Cenário ({rps} RPS) ---")
        print(f"Latência de Roteamento (Escrita) P95: {p95_write:.2f} ms")
        if read_latencies:
            p95_read = np.percentile(read_latencies, 95) * 1000
            print(f"Latência de Consulta (Leitura) P95:  {p95_read:.2f} ms")
        else:
            p95_read = None
            print("Latência de Consulta (Leitura) P95:  sem amostras válidas")

        p95_ok = p95_write < 500 and (p95_read is None or p95_read < 500)
        throughput_ok = effective_write_rps >= (0.9 * rps)

        if p95_ok and throughput_ok:
            print("SUCESSO: Requisito P95 < 500ms alcançado.")
        else:
            print("ALERTA: gargalo de performance detectado (latência e/ou throughput abaixo do esperado).")


async def main(args):
    print(f"Conectando na API {args.api_url} para buscar entidades cadastradas...")

    auth = aiohttp.BasicAuth(args.username, args.password) if args.username and args.password else None
    customers = await fetch_entity_ids(args.api_url, CUSTOMERS_ENDPOINT, auth)
    merchants = await fetch_entity_ids(args.api_url, MERCHANTS_ENDPOINT, auth)

    if not customers or not merchants:
        print("ERRO CRÍTICO: Não foi possível carregar clientes ou restaurantes.")
        print("Você tem certeza de que rodou o 'load.py' antes para popular o banco?")
        return

    print(
        f"Sucesso! {len(customers)} clientes e {len(merchants)} restaurantes carregados na memória."
    )

    # Inicia o bombardeio estocástico
    await run_load_test(args.api_url, args.rps, args.duration, customers, merchants, args.default_item_id, auth)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gerador de Carga DijkFood")
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument("--rps", type=int, default=50, help="Requisições por segundo")
    parser.add_argument(
        "--duration", type=int, default=30, help="Duração do teste em segundos"
    )
    parser.add_argument("--default-item-id", type=int, default=1, help="Item padrao para criacao de pedidos")
    parser.add_argument("--username", default=os.getenv("API_USERNAME"), help="Usuario Basic Auth da API")
    parser.add_argument("--password", default=os.getenv("API_PASSWORD"), help="Senha Basic Auth da API")
    args = parser.parse_args()

    # Passa o controle para a função principal assíncrona
    asyncio.run(main(args))
