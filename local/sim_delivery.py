"""Módulo para simular a frota de entregadores em tempo real nas ruas."""

import argparse
import asyncio
import os
from typing import Any

import aiohttp

NEW_DELIVERY_MODE = "new"
PARTIAL_DELIVERY_MODE = "partial"


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

    new_required = {
        "/couriers/me/location",
        "/couriers/me/order",
        "/orders/{order_id}/accept",
        "/orders/{order_id}/picked_up",
        "/orders/{order_id}/delivered",
    }
    if new_required <= paths:
        return NEW_DELIVERY_MODE
    return PARTIAL_DELIVERY_MODE


async def get_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def get_courier_current_order(session: aiohttp.ClientSession, api_url: str) -> dict[str, Any] | None:
    try:
        payload = await get_json(session, f"{api_url.rstrip('/')}/couriers/me/order")
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


async def update_courier_location(session: aiohttp.ClientSession, api_url: str, location: int) -> bool:
    try:
        async with session.put(
            f"{api_url.rstrip('/')}/couriers/me/location",
            json={"location": location},
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


async def deliver_order_new(session: aiohttp.ClientSession, api_url: str, order: dict[str, Any]) -> bool:
    order_id = int(order["id"])
    merchant_id = int(order["merchant_id"])
    customer_id = int(order["customer_id"])

    merchant_address = await get_merchant_address(session, api_url, merchant_id)
    customer_address = await get_customer_address(session, api_url, customer_id)

    if merchant_address is None or customer_address is None:
        return False

    print(f"✓ Novo pedido atribuído: id={order_id}, merchant={merchant_id}, customer={customer_id}")

    # No contrato completo, o accept continua válido antes do picked_up.
    if not await accept_order(session, api_url, order_id):
        return False

    print(f"  ↳ Movendo courier para restaurante no node {merchant_address}")
    if not await update_courier_location(session, api_url, merchant_address):
        return False

    if not await mark_order_picked_up(session, api_url, order_id):
        return False

    await asyncio.sleep(1)

    print(f"  ↳ Movendo courier para cliente no node {customer_address}")
    if not await update_courier_location(session, api_url, customer_address):
        return False

    if not await mark_order_delivered(session, api_url, order_id):
        return False

    print(f"  ✓ Pedido {order_id} entregue com sucesso")
    return True


async def delivery_worker(api_url: str, username: str | None = None, password: str | None = None):
    """
    Simula um courier monitorando sua fila de pedidos.

    O worker tenta detectar qual contrato a API expõe:
    - Contrato final: PUT /couriers/me/location + POST /orders/{id}/accept + POST /orders/{id}/picked_up + POST /orders/{id}/delivered
    - Parcial: apenas monitora e registra que falta contrato
    """
    auth = None
    username = username or os.getenv("API_USERNAME")
    password = password or os.getenv("API_PASSWORD")
    if username and password:
        auth = aiohttp.BasicAuth(username, password)

    async with aiohttp.ClientSession(auth=auth) as session:
        paths = await fetch_openapi_paths(session, api_url)
        delivery_mode = resolve_delivery_mode(paths)

        if delivery_mode == NEW_DELIVERY_MODE:
            print("Iniciando courier worker com contrato final: accept + couriers/me/location + picked_up + delivered")
        else:
            print("Iniciando courier worker em modo parcial: contrato de delivery incompleto")

        last_handled_order_id: int | None = None

        while True:
            try:
                current_order = await get_courier_current_order(session, api_url)

                if not current_order:
                    if last_handled_order_id is not None:
                        print(f"✓ Pedido {last_handled_order_id} finalizado")
                        last_handled_order_id = None
                    else:
                        print("  └─ Aguardando novo pedido...")
                    await asyncio.sleep(2)
                    continue

                order_id = int(current_order.get("id"))
                status = current_order.get("status")

                if order_id == last_handled_order_id:
                    print(f"  └─ Pedido {order_id} em andamento (status={status})")
                    await asyncio.sleep(2)
                    continue

                if delivery_mode == NEW_DELIVERY_MODE:
                    handled = await deliver_order_new(session, api_url, current_order)
                    if handled:
                        last_handled_order_id = order_id
                else:
                    print(
                        "  └─ Contrato de courier incompleto; aguardando /orders/{order_id}/accept, "
                        "/couriers/me/location, /orders/{order_id}/picked_up e /orders/{order_id}/delivered"
                    )
                    last_handled_order_id = order_id

            except Exception as e:
                print(f"✗ Erro no courier worker: {e}")

            await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Central de Entregadores DijkFood")
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument("--username", default=None, help="Usuario Basic Auth da API")
    parser.add_argument("--password", default=None, help="Senha Basic Auth da API")
    args = parser.parse_args()

    asyncio.run(delivery_worker(args.api_url, username=args.username, password=args.password))
