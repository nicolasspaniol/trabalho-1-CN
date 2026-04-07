"""Validação pré-simulação dos endpoints principais da API."""

import argparse
import asyncio
import base64
import os
from typing import Any

import aiohttp


REQUIRED_OPENAPI_PATHS = {
    "/customers/",
    "/customers/me",
    "/merchants/",
    "/couriers/",
    "/couriers/me/order",
    "/couriers/me/location",
    "/orders/",
    "/orders/{order_id}",
    "/orders/{order_id}/events",
    "/orders/{order_id}/accept",
    "/orders/{order_id}/ready",
    "/orders/{order_id}/picked_up",
    "/orders/{order_id}/in_transit",
    "/orders/{order_id}/delivered",
}


def auth_header(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    api_url: str,
    path: str,
    *,
    expected_status: int | None = None,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    url = f"{api_url.rstrip('/')}{path}"
    async with session.request(method, url, json=json, params=params, headers=headers) as response:
        body = await response.text()
        if expected_status is not None and response.status != expected_status:
            raise RuntimeError(
                f"{method} {path} retornou {response.status}, esperado {expected_status}. Body: {body[:500]}"
            )
        if response.status >= 400:
            raise RuntimeError(f"{method} {path} falhou com {response.status}. Body: {body[:500]}")
        if not body.strip():
            return None
        return await response.json()


async def validate(api_url: str, username: str, password: str) -> None:
    admin_headers = auth_header(username, password)

    async with aiohttp.ClientSession() as session:
        print("[preflight] Validando OpenAPI...")
        spec = await request_json(session, "GET", api_url, "/openapi.json", expected_status=200)
        paths = set((spec or {}).get("paths", {}).keys())
        missing = sorted(REQUIRED_OPENAPI_PATHS - paths)
        if missing:
            raise RuntimeError(
                "OpenAPI incompleto; faltando paths: "
                + ", ".join(missing)
                + ". Possivel causa: imagem da API desatualizada no ECS (build com cache)."
            )

        print("[preflight] Criando entidades base...")
        customer = await request_json(
            session,
            "POST",
            api_url,
            "/customers/",
            expected_status=201,
            headers=admin_headers,
            json={
                "name": "Preflight Customer",
                "email": f"preflight_customer_{os.getpid()}@example.com",
                "phone": "+55 11 90000-0001",
                "address": 4661191738,
            },
        )
        merchant = await request_json(
            session,
            "POST",
            api_url,
            "/merchants/",
            expected_status=201,
            headers=admin_headers,
            json={
                "name": "Preflight Merchant",
                "type": "Brazilian",
                "address": 2477696769,
                "items": [
                    {
                        "name": "item_preflight",
                        "preparation_time": 8,
                        "price": 21.5,
                    }
                ],
            },
        )
        courier = await request_json(
            session,
            "POST",
            api_url,
            "/couriers/",
            expected_status=201,
            headers=admin_headers,
            json={
                "name": "Preflight Courier",
                "vehicle_type": "bicycle",
                "availability": True,
                "location": 9643789758,
            },
        )

        customer_id = int(customer["id"])
        merchant_id = int(merchant["id"])
        courier_id = int(courier["id"])

        print("[preflight] Validando endpoints /me...")
        await request_json(
            session,
            "GET",
            api_url,
            "/customers/me",
            expected_status=200,
            headers=auth_header(str(customer_id), "x"),
        )
        try:
            await request_json(
                session,
                "GET",
                api_url,
                "/merchants/me",
                expected_status=200,
                headers=auth_header(str(merchant_id), "x"),
            )
        except RuntimeError as exc:
            print(f"[preflight] Aviso: /merchants/me nao respondeu como esperado: {exc}")

        await request_json(
            session,
            "GET",
            api_url,
            f"/merchants/{merchant_id}",
            expected_status=200,
            headers=admin_headers,
        )

        print("[preflight] Validando listagens principais...")
        await request_json(session, "GET", api_url, "/customers/", expected_status=200, headers=admin_headers)
        await request_json(session, "GET", api_url, "/merchants/", expected_status=200, headers=admin_headers)
        await request_json(session, "GET", api_url, "/couriers/", expected_status=200, headers=admin_headers)

        print("[preflight] Criando e avançando pedido no fluxo completo...")
        order = await request_json(
            session,
            "POST",
            api_url,
            "/orders/",
            expected_status=201,
            headers=auth_header(str(customer_id), "x"),
            json={"merchant_id": merchant_id, "item_ids": [1]},
        )
        order_id = int(order["id"])

        await request_json(
            session,
            "POST",
            api_url,
            f"/orders/{order_id}/accept",
            expected_status=200,
            headers=auth_header(str(merchant_id), "x"),
            json={},
        )
        await request_json(
            session,
            "POST",
            api_url,
            f"/orders/{order_id}/ready",
            expected_status=200,
            headers=auth_header(str(merchant_id), "x"),
            json={},
        )

        # Recupera pedido atribuído para autenticar fluxo do courier.
        courier_order = await request_json(
            session,
            "GET",
            api_url,
            "/couriers/me/order",
            expected_status=200,
            headers=auth_header(str(courier_id), "x"),
        )
        if int(courier_order["id"]) != order_id:
            raise RuntimeError(
                f"/couriers/me/order retornou pedido {courier_order.get('id')} em vez de {order_id}"
            )

        await request_json(
            session,
            "POST",
            api_url,
            f"/orders/{order_id}/picked_up",
            expected_status=200,
            headers=auth_header(str(courier_id), "x"),
            json={},
        )
        await request_json(
            session,
            "POST",
            api_url,
            f"/orders/{order_id}/in_transit",
            expected_status=200,
            headers=auth_header(str(courier_id), "x"),
            json={},
        )
        await request_json(
            session,
            "PUT",
            api_url,
            "/couriers/me/location",
            expected_status=200,
            headers=auth_header(str(courier_id), "x"),
            params={"location": 5454738291},
        )
        await request_json(
            session,
            "POST",
            api_url,
            f"/orders/{order_id}/delivered",
            expected_status=200,
            headers=auth_header(str(courier_id), "x"),
            json={},
        )

        await request_json(
            session,
            "GET",
            api_url,
            f"/orders/{order_id}",
            expected_status=200,
            headers=auth_header(str(customer_id), "x"),
        )
        events = await request_json(
            session,
            "GET",
            api_url,
            f"/orders/{order_id}/events",
            expected_status=200,
            headers=admin_headers,
        )
        if not isinstance(events, list) or len(events) < 4:
            raise RuntimeError("/orders/{order_id}/events retornou historico incompleto")

        print("[preflight] Validacao de endpoints concluida com sucesso.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Valida fluxo critico dos endpoints da API")
    parser.add_argument("--api-url", required=True, help="URL base da API")
    parser.add_argument("--username", default=os.getenv("API_USERNAME", "admin"), help="Usuario Basic Auth admin")
    parser.add_argument("--password", default=os.getenv("API_PASSWORD", "admin"), help="Senha Basic Auth admin")
    args = parser.parse_args()

    asyncio.run(validate(args.api_url, args.username, args.password))
