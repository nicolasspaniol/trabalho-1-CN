"""Módulo responsável por gerar Clientes, Restaurantes e Entregadores"""

import random
import pickle
import asyncio
import aiohttp
import argparse
import os
from faker import Faker


def _read_float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _read_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


COURIER_PER_USER = _read_float_env("SIM_COURIER_PER_USER", 3.0)
COURIER_CAP = _read_int_env("SIM_COURIER_CAP", 0)
MERCHANT_PER_USER = _read_float_env("SIM_MERCHANT_PER_USER", 0.1)
fake = Faker("pt_BR")

API_URL_ENV = "API_URL"
API_USERNAME_ENV = "API_USERNAME"
API_PASSWORD_ENV = "API_PASSWORD"

# Config "avançada" via env para reduzir flags no CLI.
GRAPH_PATH = (
    os.getenv("SIM_GRAPH_PATH", "").strip()
    or "sp_cidade.pkl"
)
MAX_CONCURRENCY = int(os.getenv("SIM_PRELOAD_MAX_CONCURRENCY", "20"))
RETRIES = int(os.getenv("SIM_PRELOAD_RETRIES", "3"))
RETRY_BACKOFF_SECONDS = float(os.getenv("SIM_PRELOAD_RETRY_BACKOFF_SECONDS", "0.25"))

CUSTOMERS_ENDPOINT = "/customers/"
MERCHANTS_ENDPOINT = "/merchants/"
COURIERS_ENDPOINT = "/couriers/"


class EndpointRequestError(RuntimeError):
    def __init__(self, endpoint: str, status: int, message: str):
        super().__init__(f"{endpoint} -> HTTP {status}: {message}")
        self.endpoint = endpoint
        self.status = status


def load_graph_from_pickle(filepath: str):
    """
    Lê e carrega o arquivo pickle para retornar o grafo
    """
    try:
        with open(filepath, "rb") as f:
            G = pickle.load(f)
        return G
    except Exception as e:
        print(f"Erro ao carregar o grafo do arquivo {filepath}: {e}")
        raise


def sample_valid_locations(graph_path: str, num_users: int):
    """
    Extrai ids de nós da malha viária para preencher address/location (inteiro)
    conforme contrato da API.
    """
    G = load_graph_from_pickle(graph_path)
    node_ids = [int(node_id) for node_id in G.nodes()]

    num_couriers = max(1, int(num_users * max(0.0, COURIER_PER_USER)))
    if COURIER_CAP > 0:
        num_couriers = min(num_couriers, COURIER_CAP)
    num_merchants = max(1, int(num_users * MERCHANT_PER_USER))
    total_entities = num_users + num_couriers + num_merchants

    sampled_nodes = random.choices(node_ids, k=total_entities)

    # (Clientes, Entregadores, Restaurantes)
    return (
        sampled_nodes[:num_users],
        sampled_nodes[num_users : num_users + num_couriers],
        sampled_nodes[-num_merchants:],
    )


async def create_entity(
    session: aiohttp.ClientSession, api_url: str, endpoint: str, payload: dict
):
    """
    Envia o POST para a API para cadastrar a entidade
    """
    url = f"{api_url.rstrip('/')}{endpoint}"
    async with session.post(url, json=payload) as response:
        if response.status >= 400:
            body = (await response.text())[:500]
            raise EndpointRequestError(endpoint, response.status, body)
        return await response.json()


async def create_entity_with_retry(
    session: aiohttp.ClientSession,
    api_url: str,
    endpoint: str,
    payload: dict,
    retries: int,
    retry_backoff_seconds: float,
):
    attempt = 0
    while True:
        try:
            return await create_entity(session, api_url, endpoint, payload)
        except EndpointRequestError as error:
            is_transient = error.status in {429, 500, 502, 503, 504}
            if attempt >= retries or not is_transient:
                raise
            await asyncio.sleep(retry_backoff_seconds * (2 ** attempt))
            attempt += 1


async def run_bounded_task(
    semaphore: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    api_url: str,
    endpoint: str,
    payload: dict,
    retries: int,
    retry_backoff_seconds: float,
):
    async with semaphore:
        return await create_entity_with_retry(
            session,
            api_url,
            endpoint,
            payload,
            retries,
            retry_backoff_seconds,
        )


async def populate_database(
    api_url: str,
    graph_path: str,
    num_users: int,
    username: str | None = None,
    password: str | None = None,
    max_concurrency: int = 20,
    retries: int = 3,
    retry_backoff_seconds: float = 0.25,
):
    """
    Criação assíncrona de todas as entidades no DB
    """
    print("Amostrando coordenadas válidas do grafo...")
    user_locs, courier_locs, merchant_locs = sample_valid_locations(
        graph_path, num_users
    )

    auth = aiohttp.BasicAuth(username, password) if username and password else None
    semaphore = asyncio.Semaphore(max_concurrency)
    async with aiohttp.ClientSession(auth=auth) as session:
        tasks = []

        # Gerar Users
        for loc in user_locs:
            payload = {
                "name": fake.name(),
                "email": fake.email(),
                "phone": fake.phone_number(),
                "address": loc,
            }
            tasks.append(
                run_bounded_task(
                    semaphore,
                    session,
                    api_url,
                    CUSTOMERS_ENDPOINT,
                    payload,
                    retries,
                    retry_backoff_seconds,
                )
            )

        # Gerar Couriers
        for loc in courier_locs:
            payload = {
                "name": fake.name(),
                "vehicle_type": random.choice(["motorcycle", "bicycle"]),
                "availability": True,
                "location": loc,
            }
            tasks.append(
                run_bounded_task(
                    semaphore,
                    session,
                    api_url,
                    COURIERS_ENDPOINT,
                    payload,
                    retries,
                    retry_backoff_seconds,
                )
            )

        # Gerar Merchants
        for loc in merchant_locs:
            payload = {
                "name": fake.company(),
                "type": random.choice(
                    ["Italian", "Japanese", "Brazilian", "Mexican", "Indian", "Chinese"]
                ),
                "address": loc,
                "items": [
                    {
                        "name": "item_padrao",
                        "preparation_time": random.randint(5, 20),
                        "price": round(random.uniform(10.0, 60.0), 2),
                    }
                ],
            }
            tasks.append(
                run_bounded_task(
                    semaphore,
                    session,
                    api_url,
                    MERCHANTS_ENDPOINT,
                    payload,
                    retries,
                    retry_backoff_seconds,
                )
            )

        print(f"Enviando {len(tasks)} requisições de cadastro para a API...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        failures = [err for err in results if isinstance(err, Exception)]
        if failures:
            print("Falhas detectadas durante carga inicial:")
            for failure in failures:
                if isinstance(failure, EndpointRequestError):
                    print(f"  - {failure}")
                else:
                    print(f"  - erro inesperado: {failure}")
            raise RuntimeError(f"Carga inicial falhou em {len(failures)} requisicoes")

        print(f"Banco populado! {len(results)} registros criados.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populador do Banco de Dados DijkFood")
    parser.add_argument("--api-url", default=os.getenv(API_URL_ENV), help=f"URL base da API (ou env {API_URL_ENV})")
    parser.add_argument("--num-users", type=int, default=100, help="Quantidade base de usuários")
    args = parser.parse_args()
    if not args.api_url:
        parser.error(f"--api-url é obrigatório (ou defina {API_URL_ENV}).")

    asyncio.run(
        populate_database(
            args.api_url,
            GRAPH_PATH,
            args.num_users,
            os.getenv(API_USERNAME_ENV),
            os.getenv(API_PASSWORD_ENV),
            MAX_CONCURRENCY,
            RETRIES,
            RETRY_BACKOFF_SECONDS,
        )
    )
