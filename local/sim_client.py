"""Módulo para simular o tráfego de clientes gerando carga e provando isolamento"""

import asyncio
import aiohttp
import time
import random
import numpy as np
import argparse
import os
import json
from collections import Counter

CUSTOMERS_ENDPOINT = "/customers/"
MERCHANTS_ENDPOINT = "/merchants/"
ORDERS_ENDPOINT = "/orders/"
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}

API_URL_ENV = "API_URL"
API_USERNAME_ENV = "API_USERNAME"
API_PASSWORD_ENV = "API_PASSWORD"

DEFAULT_TOTAL_TIMEOUT_S = float(os.getenv("SIM_HTTP_TOTAL_TIMEOUT", "10"))
DEFAULT_CONNECT_TIMEOUT_S = float(os.getenv("SIM_HTTP_CONNECT_TIMEOUT", "3"))
DEFAULT_SOCK_READ_TIMEOUT_S = float(os.getenv("SIM_HTTP_SOCK_READ_TIMEOUT", "5"))

DEFAULT_CONN_LIMIT = int(os.getenv("SIM_HTTP_CONN_LIMIT", "100"))
DEFAULT_CONN_LIMIT_PER_HOST = int(os.getenv("SIM_HTTP_CONN_LIMIT_PER_HOST", "50"))

REPORT_INTERVAL_S = int(os.getenv("SIM_REPORT_INTERVAL_S", "30"))
METRICS_PATH = os.getenv("SIM_METRICS_PATH", "/tmp/dijkfood_sim_client_metrics.json")


_STATUS_ORDER = {
    "confirmed": 0,
    "preparing": 1,
    "ready_for_pickup": 2,
    "picked_up": 3,
    "in_transit": 4,
    "delivered": 5,
}


def _status_at_least(current: str | None, desired: str) -> bool:
    if not current:
        return False
    current_key = str(current).strip().lower()
    desired_key = str(desired).strip().lower()
    if current_key not in _STATUS_ORDER or desired_key not in _STATUS_ORDER:
        return False
    return _STATUS_ORDER[current_key] >= _STATUS_ORDER[desired_key]


def _read_positive_int_env(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def classify_exception(error: Exception) -> str:
    if isinstance(error, aiohttp.ClientResponseError):
        return f"http_{error.status}"
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, aiohttp.ClientConnectionError):
        return "connection"
    return error.__class__.__name__.lower()


async def fetch_order_status(
    session: aiohttp.ClientSession,
    api_url: str,
    order_id: int,
    auth: aiohttp.BasicAuth,
) -> str | None:
    try:
        async with session.get(f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}", auth=auth) as response:
            response.raise_for_status()
            payload = await response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    return str(status).strip().lower() if status is not None else None


async def post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    json: dict,
    auth: aiohttp.BasicAuth,
    attempts: int = 3,
    base_backoff_seconds: float = 0.2,
) -> aiohttp.ClientResponse:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = await session.post(url, json=json, auth=auth)
            if response.status in TRANSIENT_HTTP_STATUSES and attempt < attempts:
                await response.release()
                await asyncio.sleep(base_backoff_seconds * attempt)
                continue
            response.raise_for_status()
            return response
        except aiohttp.ClientResponseError as error:
            last_error = error
            if error.status not in TRANSIENT_HTTP_STATUSES or attempt >= attempts:
                raise
            await asyncio.sleep(base_backoff_seconds * attempt)
        except Exception as error:
            last_error = error
            if attempt >= attempts:
                raise
            await asyncio.sleep(base_backoff_seconds * attempt)

    raise RuntimeError(f"Falha inesperada no retry de POST: {last_error}")


def make_user_auth(user_id: int) -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(str(user_id), "x")


async def transition_order_for_dispatch(
    session: aiohttp.ClientSession,
    api_url: str,
    order_id: int,
    merchant_id: int,
) -> tuple[bool, str | None]:
    merchant_auth = make_user_auth(merchant_id)
    try:
        accept_url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}/accept"
        try:
            response = await post_with_retry(session, accept_url, json={}, auth=merchant_auth)
            await response.release()
        except aiohttp.ClientResponseError as error:
            # Timeout/retry pode causar transicao duplicada. Confirma estado antes de falhar.
            if error.status != 400:
                raise
            current_status = await fetch_order_status(session, api_url, order_id, merchant_auth)
            if not _status_at_least(current_status, "preparing"):
                return False, f"accept_http_{error.status}"

        ready_url = f"{api_url.rstrip('/')}{ORDERS_ENDPOINT}{order_id}/ready"
        try:
            response = await post_with_retry(session, ready_url, json={}, auth=merchant_auth)
            await response.release()
        except aiohttp.ClientResponseError as error:
            if error.status != 400:
                raise
            current_status = await fetch_order_status(session, api_url, order_id, merchant_auth)
            if not _status_at_least(current_status, "ready_for_pickup"):
                return False, f"ready_http_{error.status}"

        return True, None
    except Exception as error:
        return False, f"transition_{classify_exception(error)}"


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
        response = await post_with_retry(session, url, json=payload, auth=make_user_auth(customer_id))
        result = await response.json()
        await response.release()
        order_id = int(result.get("id", result.get("order_id", 0)) or 0)
        write_latency = time.perf_counter() - start_time

        if order_id:
            transitioned, transition_error = await transition_order_for_dispatch(
                session,
                api_url,
                order_id,
                merchant_id,
            )
            if not transitioned:
                return None, None, None, transition_error or "transition_failed"

        return write_latency, order_id, customer_id, None
    except Exception as error:
        return None, None, None, classify_exception(error)


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
    Faz GET paginado na API para buscar todos os registros e extrair IDs.
    """
    timeout = aiohttp.ClientTimeout(
        total=DEFAULT_TOTAL_TIMEOUT_S,
        connect=DEFAULT_CONNECT_TIMEOUT_S,
        sock_read=DEFAULT_SOCK_READ_TIMEOUT_S,
    )
    page_size = min(100, _read_positive_int_env("SIM_API_PAGE_SIZE", 100))
    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        try:
            url = f"{api_url.rstrip('/')}{endpoint}"
            ids: list[int] = []
            offset = 0

            while True:
                async with session.get(url, params={"offset": offset, "limit": page_size}) as response:
                    response.raise_for_status()  # Garante que não deu erro 500/404
                    data = await response.json()

                if not isinstance(data, list) or not data:
                    break

                for item in data:
                    if not isinstance(item, dict):
                        continue
                    value = item.get("id", item.get("user_id"))
                    if value is not None:
                        ids.append(int(value))

                if len(data) < page_size:
                    break
                offset += page_size

            return ids
        except Exception as e:
            print(f"Erro ao buscar {endpoint} na API: {e}")
            return []


async def run_load_test(
    api_url: str, orders_per_second: int, duration: int, customers: list, merchants: list, default_item_id: int, auth: aiohttp.BasicAuth | None
):
    """Executa o teste de carga gerando tráfego de pedidos e leituras concorrentes"""
    write_latencies = []
    read_latencies = []
    active_orders: list[tuple[int, int]] = []
    write_attempts = 0
    successful_writes = 0
    read_attempts = 0
    successful_reads = 0
    offered_writes = 0
    dropped_writes_backpressure = 0
    write_failure_reasons: Counter[str] = Counter()
    test_start = time.perf_counter()
    last_report = test_start
    deadline = test_start + max(0, int(duration))
    write_interval = 1.0 / max(1, orders_per_second)
    next_write_at = test_start
    writes_since_last_read = 0

    pending_write_tasks: set[asyncio.Task] = set()
    pending_read_tasks: set[asyncio.Task] = set()

    max_in_flight_writes = _read_positive_int_env(
        "SIM_MAX_IN_FLIGHT_WRITES",
        max(orders_per_second * 4, DEFAULT_CONN_LIMIT_PER_HOST * 2),
    )
    max_in_flight_reads = _read_positive_int_env(
        "SIM_MAX_IN_FLIGHT_READS",
        max(20, DEFAULT_CONN_LIMIT_PER_HOST),
    )
    drain_grace_seconds = float(os.getenv("SIM_DRAIN_GRACE_SECONDS", "20"))

    connector = aiohttp.TCPConnector(limit=DEFAULT_CONN_LIMIT, limit_per_host=DEFAULT_CONN_LIMIT_PER_HOST)
    timeout = aiohttp.ClientTimeout(
        total=DEFAULT_TOTAL_TIMEOUT_S,
        connect=DEFAULT_CONNECT_TIMEOUT_S,
        sock_read=DEFAULT_SOCK_READ_TIMEOUT_S,
    )

    def process_done_tasks(done_tasks: set[asyncio.Task]) -> None:
        nonlocal successful_writes, successful_reads
        for task in done_tasks:
            if task in pending_write_tasks:
                pending_write_tasks.discard(task)
                try:
                    w_lat, o_id, owner_customer_id, w_error = task.result()
                except Exception:
                    w_lat, o_id, owner_customer_id, w_error = None, None, None, "task_error"

                if w_lat is not None:
                    write_latencies.append(w_lat)
                    successful_writes += 1
                    if o_id:
                        active_orders.append((o_id, owner_customer_id))
                else:
                    write_failure_reasons[w_error or "unknown"] += 1
            elif task in pending_read_tasks:
                pending_read_tasks.discard(task)
                try:
                    r_lat = task.result()
                except Exception:
                    r_lat = None
                if r_lat is not None:
                    read_latencies.append(r_lat)
                    successful_reads += 1

    def report_progress() -> None:
        nonlocal last_report
        now_inner = time.perf_counter()
        should_report = (now_inner - last_report) >= REPORT_INTERVAL_S or now_inner >= deadline
        if not should_report:
            return

        total_elapsed = max(1e-9, now_inner - test_start)
        effective_write_rps = successful_writes / total_elapsed
        effective_read_rps = successful_reads / total_elapsed
        p95_write_ms = (float(np.percentile(write_latencies, 95)) * 1000) if write_latencies else None
        p95_read_ms = (float(np.percentile(read_latencies, 95)) * 1000) if read_latencies else None

        metrics = {
            "ts": time.time(),
            "elapsed_s": total_elapsed,
            "target_rps": orders_per_second,
            "duration_s": duration,
            "write_offered": offered_writes,
            "write_attempts": write_attempts,
            "write_dropped_backpressure": dropped_writes_backpressure,
            "write_success": successful_writes,
            "read_attempts": read_attempts,
            "read_success": successful_reads,
            "in_flight_writes": len(pending_write_tasks),
            "in_flight_reads": len(pending_read_tasks),
            "effective_write_rps": effective_write_rps,
            "effective_read_rps": effective_read_rps,
            "p95_write_ms": p95_write_ms,
            "p95_read_ms": p95_read_ms,
            "top_write_failures": write_failure_reasons.most_common(5),
        }

        try:
            with open(METRICS_PATH, "w", encoding="utf-8") as f:
                json.dump(metrics, f)
        except Exception:
            pass

        top_failures = ", ".join(f"{reason}:{count}" for reason, count in write_failure_reasons.most_common(3))

        print(
            f"[sim_client] {int(total_elapsed)}/{duration}s "
            f"tasks_ok={successful_writes}/{write_attempts} "
            f"offered={offered_writes} drop_bp={dropped_writes_backpressure} "
            f"inflight_w={len(pending_write_tasks)} "
            f"RPS={effective_write_rps:.2f} "
            f"P95_write_ms={(f'{p95_write_ms:.2f}' if p95_write_ms is not None else 'NA')} "
            f"P95_read_ms={(f'{p95_read_ms:.2f}' if p95_read_ms is not None else 'NA')} "
            f"fails=[{top_failures or 'NA'}]"
        )
        last_report = now_inner

    async with aiohttp.ClientSession(connector=connector, auth=auth, timeout=timeout) as session:
        print(f"Iniciando teste de carga: {orders_per_second} pedidos/s por {duration}s...")

        while True:
            now = time.perf_counter()
            if now >= deadline:
                break

            scheduled_now = 0
            max_burst = max(1, orders_per_second)
            while now >= next_write_at and now < deadline and scheduled_now < max_burst:
                offered_writes += 1

                if len(pending_write_tasks) >= max_in_flight_writes:
                    dropped_writes_backpressure += 1
                    next_write_at += write_interval
                    scheduled_now += 1
                    now = time.perf_counter()
                    continue

                pending_write_tasks.add(
                    asyncio.create_task(
                        place_order(
                            session,
                            api_url,
                            random.choice(customers),
                            random.choice(merchants),
                            default_item_id,
                        )
                    )
                )
                write_attempts += 1
                writes_since_last_read += 1

                if active_orders and writes_since_last_read >= 5 and len(pending_read_tasks) < max_in_flight_reads:
                    order_id, owner_customer_id = random.choice(active_orders)
                    pending_read_tasks.add(
                        asyncio.create_task(
                            check_order_status(session, api_url, order_id, owner_customer_id)
                        )
                    )
                    read_attempts += 1
                    writes_since_last_read = 0

                next_write_at += write_interval
                scheduled_now += 1
                now = time.perf_counter()

            pending_all = pending_write_tasks | pending_read_tasks
            wait_timeout = max(0.0, min(0.05, next_write_at - time.perf_counter()))
            if pending_all:
                done, _ = await asyncio.wait(pending_all, timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED)
                if done:
                    process_done_tasks(done)
            elif wait_timeout > 0:
                await asyncio.sleep(wait_timeout)

            report_progress()

        # Dá um tempo curto para concluir respostas em voo antes de finalizar as métricas.
        drain_deadline = time.perf_counter() + max(1.0, drain_grace_seconds)
        while (pending_write_tasks or pending_read_tasks) and time.perf_counter() < drain_deadline:
            pending_all = pending_write_tasks | pending_read_tasks
            done, _ = await asyncio.wait(pending_all, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            if done:
                process_done_tasks(done)

        # Evita deixar tarefas órfãs quando o backend está muito lento ou indisponível.
        pending_remaining = pending_write_tasks | pending_read_tasks
        for task in pending_remaining:
            task.cancel()
        if pending_remaining:
            await asyncio.gather(*pending_remaining, return_exceptions=True)

        report_progress()

    total_elapsed = max(1e-9, time.perf_counter() - test_start)
    effective_write_rps = successful_writes / total_elapsed
    effective_read_rps = successful_reads / total_elapsed

    print(f"\n--- Resumo de Throughput ({orders_per_second} pedidos/s alvo) ---")
    print(f"Escritas oferecidas: {offered_writes} | descartadas por backpressure: {dropped_writes_backpressure}")
    print(f"Escritas: {successful_writes}/{write_attempts} sucesso | RPS efetivo: {effective_write_rps:.2f}")
    if write_failure_reasons:
        top_failures = ", ".join(f"{reason}={count}" for reason, count in write_failure_reasons.most_common(5))
        print(f"Falhas de escrita (top): {top_failures}")
    if read_attempts:
        print(f"Leituras: {successful_reads}/{read_attempts} sucesso | RPS efetivo: {effective_read_rps:.2f}")
    else:
        print("Leituras: sem amostras (sem pedidos ativos no inicio do teste)")

    # Relatório de isolamento e latência P95
    if write_latencies:
        p95_write = np.percentile(write_latencies, 95) * 1000

        print(f"\n--- Resultados do Cenário ({orders_per_second} pedidos/s) ---")
        print(f"Latência de Roteamento (Escrita) P95: {p95_write:.2f} ms")
        if read_latencies:
            p95_read = np.percentile(read_latencies, 95) * 1000
            print(f"Latência de Consulta (Leitura) P95:  {p95_read:.2f} ms")
        else:
            p95_read = None
            print("Latência de Consulta (Leitura) P95:  sem amostras válidas")

        p95_ok = p95_write < 500 and (p95_read is None or p95_read < 500)
        throughput_ok = effective_write_rps >= (0.9 * orders_per_second)

        if p95_ok and throughput_ok:
            print("SUCESSO: Requisito P95 < 500ms alcançado.")
        else:
            print("ALERTA: gargalo de performance detectado (latência e/ou throughput abaixo do esperado).")


async def main(args):
    print(f"Conectando na API {args.api_url} para buscar entidades cadastradas...")

    username = os.getenv(API_USERNAME_ENV)
    password = os.getenv(API_PASSWORD_ENV)
    auth = aiohttp.BasicAuth(username, password) if username and password else None
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
    await run_load_test(args.api_url, args.rps, args.duration, customers, merchants, default_item_id=1, auth=auth)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gerador de Carga DijkFood")
    parser.add_argument("--api-url", default=os.getenv(API_URL_ENV), help=f"URL base da API (ou env {API_URL_ENV})")
    parser.add_argument("--rps", type=int, default=50, help="Requisições por segundo")
    parser.add_argument("--duration", type=int, default=30, help="Duração do teste em segundos")
    args = parser.parse_args()
    if not args.api_url:
        parser.error(f"--api-url é obrigatório (ou defina {API_URL_ENV}).")

    # Passa o controle para a função principal assíncrona
    asyncio.run(main(args))
