"""Módulo responsável por gerar Clientes, Restaurantes e Entregadores"""

import random
import pickle
import asyncio
import aiohttp
import osmnx as ox
import argparse
import os
from faker import Faker

COURIER_PER_USER = 3  # REGRA: 3 entregadores por cliente
MERCHANT_PER_USER = 0.1  # 1 restaurante para cada 10 clientes
fake = Faker("pt_BR")

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

    num_couriers = num_users * COURIER_PER_USER
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


async def populate_database(api_url: str, graph_path: str, num_users: int, username: str | None = None, password: str | None = None):
    """
    Criação assíncrona de todas as entidades no DB
    """
    print("Amostrando coordenadas válidas do grafo...")
    user_locs, courier_locs, merchant_locs = sample_valid_locations(
        graph_path, num_users
    )

    auth = aiohttp.BasicAuth(username, password) if username and password else None
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
            tasks.append(create_entity(session, api_url, CUSTOMERS_ENDPOINT, payload))

        # Gerar Couriers
        for loc in courier_locs:
            payload = {
                "name": fake.name(),
                "vehicle_type": random.choice(["motorcycle", "bicycle"]),
                "availability": True,
                "location": loc,
            }
            tasks.append(create_entity(session, api_url, COURIERS_ENDPOINT, payload))

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
            tasks.append(create_entity(session, api_url, MERCHANTS_ENDPOINT, payload))

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
    parser.add_argument("--api-url", required=True, help="URL base da API (ALB)")
    parser.add_argument(
        "--graph-path", default="sp_altodepinheiros.pkl", help="Caminho do grafo local"
    )
    parser.add_argument(
        "--num-users", type=int, default=100, help="Quantidade base de usuários"
    )
    parser.add_argument("--username", default=os.getenv("API_USERNAME"), help="Usuario Basic Auth da API")
    parser.add_argument("--password", default=os.getenv("API_PASSWORD"), help="Senha Basic Auth da API")
    args = parser.parse_args()

    asyncio.run(populate_database(args.api_url, args.graph_path, args.num_users, args.username, args.password))
