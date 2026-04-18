"""Módulo para simular a frota de entregadores em tempo real nas ruas."""

import argparse
import asyncio
import os
import random
import time
from typing import Any

import aiohttp

API_URL_ENV = "API_URL"

COURIER_MAX = int(os.getenv("SIM_COURIER_MAX", "200"))
COURIER_POLL_SECONDS = float(os.getenv("SIM_COURIER_POLL_SECONDS", "1"))
COURIER_POLL_JITTER_SECONDS = float(os.getenv("SIM_COURIER_POLL_JITTER_SECONDS", "0.25"))
ERROR_BACKOFF_MAX_SECONDS = float(os.getenv("SIM_COURIER_ERROR_BACKOFF_MAX_SECONDS", "30"))

NEW_DELIVERY_MODE = "new"
PARTIAL_DELIVERY_MODE = "partial"

REQUIRED_DELIVERY_PATHS = {
    "/couriers/me/location",
    "/couriers/me/order",
    "/orders/{order_id}/accept",
    "/orders/{order_id}/ready",
    "/orders/{order_id}/picked_up",
}

OPTIONAL_DELIVERED_PATH = "/orders/{order_id}/delivered"


class DeliveryStats:
    def __init__(self) -> None:
        self.assigned = 0
        self.completed = 0
        self.failed = 0

    def mark_assigned(self) -> None:
        self.assigned += 1

    def mark_completed(self) -> None:
        self.completed += 1

    def mark_failed(self) -> None:
        self.failed += 1

    def summary(self) -> str:
        return (
            f"pedidos_atribuidos={self.assigned} "
            f"pedidos_concluidos={self.completed} "
            f"falhas={self.failed}"
        )


async def fetch_openapi_paths(session: aiohttp.ClientSession, api_url: str) -> set[str] | None:
    try:
        async with session.get(f"{api_url.rstrip('/')}/openapi.json") as response:
            if response.status != 200:
                return None
            payload = await response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return None

    return set(paths.keys())


def resolve_delivery_mode(paths: set[str] | None) -> str:
    if not paths:
        return PARTIAL_DELIVERY_MODE

    if REQUIRED_DELIVERY_PATHS <= paths:
        return NEW_DELIVERY_MODE
    return PARTIAL_DELIVERY_MODE


def supports_delivered(paths: set[str] | None) -> bool:
    return bool(paths and OPTIONAL_DELIVERED_PATH in paths)


async def get_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


_RETRY_STATUSES = {429, 502, 503, 504}


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


async def _fetch_order_status(session: aiohttp.ClientSession, api_url: str, order_id: int) -> str | None:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/orders/{order_id}")
    except Exception:
        return None
    if isinstance(payload, dict):
        status = payload.get("status")
        return str(status).strip().lower() if status is not None else None
    return None


async def _request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    api_url: str | None = None,
    order_id: int | None = None,
    desired_status: str | None = None,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    attempts: int = 5,
    base_delay_s: float = 0.25,
    error_prefix: str,
) -> bool:
    delay = base_delay_s
    last_exc: Exception | None = None
    last_http_status: int | None = None
    last_http_body: str | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with session.request(method, url, params=params, json=json) as response:
                if response.status in _RETRY_STATUSES:
                    body = await response.text()
                    last_http_status = int(response.status)
                    last_http_body = body[:200]
                    if attempt >= attempts:
                        response.raise_for_status()
                else:
                    if response.status == 400 and desired_status and api_url and order_id:
                        # Timeout/retry pode causar transição duplicada. Confirma status antes de falhar.
                        current = await _fetch_order_status(session, api_url, order_id)
                        if _status_at_least(current, desired_status):
                            return True
                    response.raise_for_status()
                    return True
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            aiohttp.ServerDisconnectedError,
            asyncio.TimeoutError,
        ) as e:
            last_exc = e
            if desired_status and api_url and order_id:
                # Se o servidor aplicou a transição mas o cliente não viu a resposta, trata como sucesso.
                current = await _fetch_order_status(session, api_url, order_id)
                if _status_at_least(current, desired_status):
                    return True
            if attempt >= attempts:
                break
        except Exception as e:
            print(f"{error_prefix}: {e}")
            return False

        await asyncio.sleep(delay + random.random() * min(0.1, delay))
        delay = min(5.0, delay * 2)

    if last_exc is not None:
        print(f"{error_prefix}: {last_exc}")
        return False
    if last_http_status is not None:
        print(f"{error_prefix}: HTTP {last_http_status} apos {attempts} tentativas body={last_http_body!r}")
    return False


async def fetch_courier_ids(session: aiohttp.ClientSession, api_url: str) -> list[int]:
    try:
        page_size = min(100, max(1, int(os.getenv("SIM_API_PAGE_SIZE", "100"))))
    except (TypeError, ValueError):
        page_size = 100
    offset = 0
    courier_ids: list[int] = []

    try:
        while True:
            payload = await get_json(
                session,
                f"{api_url.rstrip('/')}/couriers/?offset={offset}&limit={page_size}",
            )
            if not isinstance(payload, list) or not payload:
                break

            for item in payload:
                if isinstance(item, dict):
                    value = item.get("id", item.get("user_id"))
                    try:
                        if value is not None:
                            courier_ids.append(int(value))
                    except (TypeError, ValueError):
                        continue

            if len(payload) < page_size:
                break
            offset += page_size
    except Exception as e:
        print(f"  Erro ao buscar lista de couriers: {e}")
        return []
    return courier_ids


async def get_courier_current_order(session: aiohttp.ClientSession, api_url: str) -> dict[str, Any] | None:
    try:
        # Modo leve: evita leituras no DynamoDB (rota/localizacao) que aumentam latencia e timeout sob carga.
        payload = await get_json(
            session,
            f"{api_url.rstrip('/')}/couriers/me/order?include_route=false&include_location=false",
        )
    except aiohttp.ClientResponseError as e:
        if e.status in (404, 500, 502, 503, 504):
            return None
        print(f"  Erro ao buscar pedido atual: {e}")
        return None
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientPayloadError,
        asyncio.TimeoutError,
    ):
        # Erro transitório esperado sob carga; o loop principal aplica o ritmo de polling.
        return None
    except Exception as e:
        print(f"  Erro ao buscar pedido atual: {e}")
        return None

    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    return None


async def get_customer_address(session: aiohttp.ClientSession, api_url: str, customer_id: int) -> int | None:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/customers/{customer_id}")
        address = payload.get("address") if isinstance(payload, dict) else None
        return int(address) if address is not None else None
    except Exception as e:
        print(f"  Erro ao buscar endereco do cliente {customer_id}: {e}")
        return None


async def get_merchant_address(session: aiohttp.ClientSession, api_url: str, merchant_id: int) -> int | None:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/merchants/{merchant_id}")
        address = payload.get("address") if isinstance(payload, dict) else None
        return int(address) if address is not None else None
    except Exception as e:
        print(f"  Erro ao buscar endereco do restaurante {merchant_id}: {e}")
        return None


async def update_courier_location(
    session: aiohttp.ClientSession,
    location_api_url: str,
    order_id: int,
    location: int,
) -> bool:
    return await _request_with_retry(
        session,
        "PUT",
        f"{location_api_url.rstrip('/')}/couriers/me/location",
        api_url=location_api_url,
        order_id=order_id,
        params={"order_id": order_id},
        json={"location": location},
        error_prefix="  Erro ao atualizar location do courier",
    )


async def mark_order_picked_up(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    return await _request_with_retry(
        session,
        "POST",
        f"{api_url.rstrip('/')}/orders/{order_id}/picked_up",
        api_url=api_url,
        order_id=order_id,
        desired_status="picked_up",
        json={},
        error_prefix=f"  Erro ao marcar pedido {order_id} como picked_up",
    )


async def mark_order_ready(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    return await _request_with_retry(
        session,
        "POST",
        f"{api_url.rstrip('/')}/orders/{order_id}/ready",
        api_url=api_url,
        order_id=order_id,
        desired_status="ready_for_pickup",
        json={},
        error_prefix=f"  Erro ao marcar pedido {order_id} como ready",
    )


async def mark_order_in_transit(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    return await _request_with_retry(
        session,
        "POST",
        f"{api_url.rstrip('/')}/orders/{order_id}/in_transit",
        api_url=api_url,
        order_id=order_id,
        desired_status="in_transit",
        json={},
        error_prefix=f"  Erro ao marcar pedido {order_id} como in_transit",
    )


async def mark_order_delivered(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    return await _request_with_retry(
        session,
        "POST",
        f"{api_url.rstrip('/')}/orders/{order_id}/delivered",
        api_url=api_url,
        order_id=order_id,
        desired_status="delivered",
        json={},
        error_prefix=f"  Erro ao marcar pedido {order_id} como delivered",
    )


async def accept_order(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    return await _request_with_retry(
        session,
        "POST",
        f"{api_url.rstrip('/')}/orders/{order_id}/accept",
        api_url=api_url,
        order_id=order_id,
        desired_status="preparing",
        json={},
        error_prefix=f"  Erro ao aceitar pedido {order_id}",
    )


async def deliver_order_new(
    session: aiohttp.ClientSession,
    api_url: str,
    location_api_url: str,
    order: dict[str, Any],
    can_mark_delivered: bool,
    stats: DeliveryStats,
) -> bool:
    order_id = int(order["id"])
    stats.mark_assigned()

    if not await mark_order_picked_up(session, api_url, order_id):
        stats.mark_failed()
        return False

    if not await mark_order_in_transit(session, api_url, order_id):
        stats.mark_failed()
        return False

    await asyncio.sleep(1)

    route = order.get("delivery_route") if isinstance(order, dict) else None
    if isinstance(route, dict):
        path_to_user = route.get("path_to_user")
        if isinstance(path_to_user, list) and path_to_user:
            try:
                customer_node = int(path_to_user[-1])
                await update_courier_location(session, location_api_url, order_id, customer_node)
            except (TypeError, ValueError):
                pass

    if can_mark_delivered:
        if not await mark_order_delivered(session, api_url, order_id):
            stats.mark_failed()
            return False

    stats.mark_completed()
    return True


async def run_courier_loop(
    api_url: str,
    location_api_url: str,
    courier_username: str,
    delivery_mode: str,
    can_mark_delivered: bool,
    stats: DeliveryStats,
):
    auth = aiohttp.BasicAuth(courier_username, "x")

    timeout = aiohttp.ClientTimeout(
        total=float(os.getenv("SIM_HTTP_TOTAL_TIMEOUT", "30")),
        connect=float(os.getenv("SIM_HTTP_CONNECT_TIMEOUT", "5")),
        sock_read=float(os.getenv("SIM_HTTP_SOCK_READ_TIMEOUT", "20")),
    )
    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        last_handled_order_id: int | None = None
        error_backoff = 0.0
        last_error_log = 0.0

        while True:
            try:
                current_order = await get_courier_current_order(session, api_url)
                error_backoff = 0.0

                if not current_order:
                    last_handled_order_id = None
                    await asyncio.sleep(_poll_sleep_seconds())
                    continue

                order_id_value = current_order.get("id")
                if order_id_value is None:
                    await asyncio.sleep(2)
                    continue
                order_id = int(order_id_value)
                status = current_order.get("status")

                if order_id == last_handled_order_id:
                    await asyncio.sleep(_poll_sleep_seconds())
                    continue

                if delivery_mode == NEW_DELIVERY_MODE:
                    if not str(status).upper().endswith("READY_FOR_PICKUP"):
                        await asyncio.sleep(_poll_sleep_seconds())
                        continue
                    handled = await deliver_order_new(
                        session,
                        api_url,
                        location_api_url,
                        current_order,
                        can_mark_delivered,
                        stats,
                    )
                    if handled:
                        last_handled_order_id = order_id
                else:
                    last_handled_order_id = order_id

            except Exception as e:
                # Backoff por courier: evita que timeouts em cascata virem DDoS no ALB/API.
                error_backoff = min(ERROR_BACKOFF_MAX_SECONDS, max(1.0, error_backoff * 2) if error_backoff else 2.0)
                now = time.monotonic()
                # Evita spam de log em loops rápidos.
                if now - last_error_log >= 10.0:
                    print(f"✗ Erro no courier worker ({courier_username}): {e} (backoff={error_backoff:.1f}s)")
                    last_error_log = now

            await asyncio.sleep(_poll_sleep_seconds() + error_backoff)


async def report_delivery_progress(stats: DeliveryStats, interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        print(f"[sim_delivery] {stats.summary()}")


async def delivery_worker(
    api_url: str,
    location_api_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
):
    """
    Simula um courier monitorando sua fila de pedidos.

    O worker tenta detectar qual contrato a API expõe:
    - Contrato final mínimo: PUT /couriers/me/location + POST /orders/{id}/accept + POST /orders/{id}/ready + POST /orders/{id}/picked_up
    - Contrato final estendido: inclui também POST /orders/{id}/delivered
    - Parcial: apenas monitora e registra que falta contrato
    """
    username = username or os.getenv("API_USERNAME")
    password = password or os.getenv("API_PASSWORD")

    admin_auth = aiohttp.BasicAuth(username, password) if username and password else None
    location_api_url = location_api_url or api_url

    timeout = aiohttp.ClientTimeout(
        total=float(os.getenv("SIM_HTTP_TOTAL_TIMEOUT", "30")),
        connect=float(os.getenv("SIM_HTTP_CONNECT_TIMEOUT", "5")),
        sock_read=float(os.getenv("SIM_HTTP_SOCK_READ_TIMEOUT", "20")),
    )
    async with aiohttp.ClientSession(auth=admin_auth, timeout=timeout) as admin_session:
        courier_ids = await fetch_courier_ids(admin_session, api_url)

    selected_couriers: list[str] = []
    if courier_ids:
        if COURIER_MAX > 0:
            if len(courier_ids) > COURIER_MAX:
                courier_ids = random.sample(courier_ids, COURIER_MAX)
        selected_couriers = [str(cid) for cid in courier_ids]
        print(f"Iniciando courier worker com {len(selected_couriers)} couriers")
    elif username and username.isnumeric():
        selected_couriers = [username]
        print(f"Iniciando courier worker usando courier_id informado: {username}")
    else:
        print("Nao foi possivel descobrir um courier valido; simulacao de entrega nao sera iniciada")
        return

    async with aiohttp.ClientSession(auth=admin_auth, timeout=timeout) as session:
        api_paths = await fetch_openapi_paths(session, api_url)
        location_paths = await fetch_openapi_paths(session, location_api_url)
        paths = set()
        if api_paths:
            paths.update(api_paths)
        if location_paths:
            paths.update(location_paths)

        delivery_mode = resolve_delivery_mode(paths)
        can_mark_delivered = supports_delivered(api_paths)

        if delivery_mode == NEW_DELIVERY_MODE:
            if can_mark_delivered:
                print("Iniciando courier worker com contrato final: accept + ready + couriers/me/location + picked_up + delivered")
            else:
                print("Iniciando courier worker com contrato final mínimo: accept + ready + couriers/me/location + picked_up")
        else:
            print("Iniciando courier worker em modo parcial: contrato de delivery incompleto")

        stats = DeliveryStats()
        tasks = [
            asyncio.create_task(
                run_courier_loop(
                    api_url=api_url,
                    location_api_url=location_api_url,
                    courier_username=courier_username,
                    delivery_mode=delivery_mode,
                    can_mark_delivered=can_mark_delivered,
                    stats=stats,
                )
            )
            for courier_username in selected_couriers
        ]
        tasks.append(asyncio.create_task(report_delivery_progress(stats, interval_seconds=5)))
        await asyncio.gather(*tasks)


def _poll_sleep_seconds() -> float:
    base = max(0.1, COURIER_POLL_SECONDS)
    jitter = max(0.0, COURIER_POLL_JITTER_SECONDS)
    return base + (random.random() * jitter)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Central de Entregadores DijkFood")
    parser.add_argument("--api-url", default=os.getenv(API_URL_ENV), help=f"URL base da API (ou env {API_URL_ENV})")
    parser.add_argument(
        "--location-api-url",
        default=None,
        help="URL base da API de location (ALB). Quando omitida, usa --api-url.",
    )
    args = parser.parse_args()
    if not args.api_url:
        parser.error(f"--api-url é obrigatório (ou defina {API_URL_ENV}).")

    asyncio.run(
        delivery_worker(
            args.api_url,
            location_api_url=args.location_api_url,
        )
    )
