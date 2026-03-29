"""Módulo responsável por gerar Clientes, Restaurantes e Entregadores"""

import random
import pickle
import asyncio
import aiohttp
import osmnx as ox
import argparse
from faker import Faker

COURIER_PER_USER = 3  # REGRA: 3 entregadores por cliente
MERCHANT_PER_USER = 0.1  # 1 restaurante para cada 10 clientes
fake = Faker("pt_BR")


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
    Extrai coordenadas reais dos nós da malha viária
    para criar entidades com localizações plausíveis
    """
    G = load_graph_from_pickle(graph_path)
    nodes_data = list(G.nodes(data=True))

    num_couriers = num_users * COURIER_PER_USER
    num_merchants = max(1, int(num_users * MERCHANT_PER_USER))
    total_entities = num_users + num_couriers + num_merchants

    sampled_nodes = random.choices(nodes_data, k=total_entities)
    locations = [{"lat": data["y"], "lon": data["x"]} for _, data in sampled_nodes]

    # (Clientes, Entregadores, Restaurantes)
    return (
        locations[:num_users],
        locations[num_users : num_users + num_couriers],
        locations[-num_merchants:],
    )


async def create_entity(
    session: aiohttp.ClientSession, api_url: str, endpoint: str, payload: dict
):
    """
    Envia o POST para a API para cadastrar a entidade
    """
    async with session.post(f"{api_url}/{endpoint}", json=payload) as response:
        return await response.json()


async def populate_database(api_url: str, graph_path: str, num_users: int):
    """
    Criação assíncrona de todas as entidades no DB
    """
    print("Amostrando coordenadas válidas do grafo...")
    user_locs, courier_locs, merchant_locs = sample_valid_locations(
        graph_path, num_users
    )

    async with aiohttp.ClientSession() as session:
        tasks = []

        # Gerar Users
        for loc in user_locs:
            payload = {
                "name": fake.name(),
                "email": fake.email(),
                "phone": fake.phone_number(),
                "location": loc,
            }
            tasks.append(create_entity(session, api_url, "users", payload))

        # Gerar Couriers
        for loc in courier_locs:
            payload = {
                "name": fake.name(),
                "vehicle": random.choice(["motorcycle", "bicycle"]),
                "location": loc,
            }
            tasks.append(create_entity(session, api_url, "couriers", payload))

        # Gerar Merchants
        for loc in merchant_locs:
            payload = {
                "name": fake.company(),
                "cuisine": random.choice(
                    ["Italian", "Japanese", "Brazilian", "Mexican", "Indian", "Chinese"]
                ),
                "location": loc,
            }
            tasks.append(create_entity(session, api_url, "merchants", payload))

        print(f"Enviando {len(tasks)} requisições de cadastro para a API...")
        results = await asyncio.gather(*tasks)
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
    args = parser.parse_args()

    asyncio.run(populate_database(args.api_url, args.graph_path, args.num_users))
