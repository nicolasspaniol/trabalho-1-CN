"""Módulo para simular a frota de entregadores em tempo real nas ruas."""

import argparse
import asyncio
import os
from typing import Any

import aiohttp

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


async def fetch_courier_ids(session: aiohttp.ClientSession, api_url: str) -> list[int]:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/couriers/")
    except Exception as e:
        print(f"  Erro ao buscar lista de couriers: {e}")
        return []

    if not isinstance(payload, list):
        return []

    courier_ids: list[int] = []
    for item in payload:
        if isinstance(item, dict):
            value = item.get("id", item.get("user_id"))
            try:
                if value is not None:
                    courier_ids.append(int(value))
            except (TypeError, ValueError):
                continue
    return courier_ids


async def get_courier_current_order(session: aiohttp.ClientSession, api_url: str) -> dict[str, Any] | None:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/couriers/me/order")
    except aiohttp.ClientResponseError as e:
        if e.status == 404:
            return None
        print(f"  Erro ao buscar pedido atual: {e}")
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
    try:
        async with session.put(
            f"{location_api_url.rstrip('/')}/couriers/me/location",
            params={"order_id": order_id, "location": location},
        ) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao atualizar location do courier: {e}")
        return False


async def mark_order_picked_up(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    try:
        async with session.post(f"{api_url.rstrip('/')}/orders/{order_id}/picked_up", json={}) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao marcar pedido {order_id} como picked_up: {e}")
        return False


async def mark_order_ready(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    try:
        async with session.post(f"{api_url.rstrip('/')}/orders/{order_id}/ready", json={}) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao marcar pedido {order_id} como ready: {e}")
        return False


async def mark_order_in_transit(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    try:
        async with session.post(f"{api_url.rstrip('/')}/orders/{order_id}/in_transit", json={}) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao marcar pedido {order_id} como in_transit: {e}")
        return False


async def mark_order_delivered(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    try:
        async with session.post(f"{api_url.rstrip('/')}/orders/{order_id}/delivered", json={}) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao marcar pedido {order_id} como delivered: {e}")
        return False


async def accept_order(session: aiohttp.ClientSession, api_url: str, order_id: int) -> bool:
    try:
        async with session.post(f"{api_url.rstrip('/')}/orders/{order_id}/accept", json={}) as response:
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"  Erro ao aceitar pedido {order_id}: {e}")
        return False


async def deliver_order_new(
    session: aiohttp.ClientSession,
    api_url: str,
    location_api_url: str,
    order: dict[str, Any],
    can_mark_delivered: bool,
) -> bool:
    order_id = int(order["id"])
    print(f"✓ Novo pedido atribuído: id={order_id}")

    if not await mark_order_picked_up(session, api_url, order_id):
        return False

    if not await mark_order_in_transit(session, api_url, order_id):
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
            return False

    print(f"  ✓ Pedido {order_id} entregue com sucesso")
    return True


async def run_courier_loop(
    api_url: str,
    location_api_url: str,
    courier_username: str,
    delivery_mode: str,
    can_mark_delivered: bool,
):
    auth = aiohttp.BasicAuth(courier_username, "x")

    async with aiohttp.ClientSession(auth=auth) as session:
        last_handled_order_id: int | None = None

        while True:
            try:
                current_order = await get_courier_current_order(session, api_url)

                if not current_order:
                    if last_handled_order_id is not None:
                        print(f"✓ Pedido {last_handled_order_id} finalizado (courier={courier_username})")
                        last_handled_order_id = None
                    await asyncio.sleep(2)
                    continue

                order_id_value = current_order.get("id")
                if order_id_value is None:
                    await asyncio.sleep(2)
                    continue
                order_id = int(order_id_value)
                status = current_order.get("status")

                if order_id == last_handled_order_id:
                    await asyncio.sleep(2)
                    continue

                if delivery_mode == NEW_DELIVERY_MODE:
                    if not str(status).upper().endswith("READY_FOR_PICKUP"):
                        await asyncio.sleep(2)
                        continue
                    handled = await deliver_order_new(
                        session,
                        api_url,
                        location_api_url,
                        current_order,
                        can_mark_delivered,
                    )
                    if handled:
                        last_handled_order_id = order_id
                else:
                    print(
                        "  └─ Contrato de courier incompleto; aguardando /orders/{order_id}/accept, "
                        "/orders/{order_id}/ready, /couriers/me/location e /orders/{order_id}/picked_up"
                    )
                    last_handled_order_id = order_id

            except Exception as e:
                print(f"✗ Erro no courier worker ({courier_username}): {e}")

            await asyncio.sleep(2)


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

    async with aiohttp.ClientSession(auth=admin_auth) as admin_session:
        courier_ids = await fetch_courier_ids(admin_session, api_url)

    selected_couriers: list[str] = []
    if courier_ids:
        selected_couriers = [str(cid) for cid in courier_ids]
        print(f"Iniciando courier worker com {len(selected_couriers)} couriers")
    elif username and username.isnumeric():
        selected_couriers = [username]
        print(f"Iniciando courier worker usando courier_id informado: {username}")
    else:
        print("Nao foi possivel descobrir um courier valido; simulacao de entrega nao sera iniciada")
        return

    async with aiohttp.ClientSession(auth=admin_auth) as session:
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

        tasks = [
            asyncio.create_task(
                run_courier_loop(
                    api_url=api_url,
                    location_api_url=location_api_url,
                    courier_username=courier_username,
                    delivery_mode=delivery_mode,
                    can_mark_delivered=can_mark_delivered,
                )
            )
            for courier_username in selected_couriers
        ]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Central de Entregadores DijkFood")
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument(
        "--location-api-url",
        default=None,
        help="URL base da API de location (ALB). Quando omitida, usa --api-url.",
    )
    parser.add_argument("--username", default=None, help="Usuario Basic Auth da API")
    parser.add_argument("--password", default=None, help="Senha Basic Auth da API")
    args = parser.parse_args()

    asyncio.run(
        delivery_worker(
            args.api_url,
            location_api_url=args.location_api_url,
            username=args.username,
            password=args.password,
        )
    )
