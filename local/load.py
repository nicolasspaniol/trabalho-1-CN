"""Módulo responsável por gerar Clientes, Restaurantes e Entregadores"""

import random
import pickle
import asyncio
import aiohttp
import osmnx as ox
from faker import Faker


COURIER_PER_USER = 3  # 3 entregadores por cliente
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
    Carrega o mapa e extrai coordenadas reais dos nós para evitar que
    entidades sejam criadas fora da malha viária
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


# TODO: Adicionar tratamento de erros
async def create_entity(
    session: aiohttp.ClientSession, api_url: str, endpoint: str, payload: dict
):
    """
    Envia o POST para a API para cadastrar a entidade
    """
    async with session.post(f"{api_url}/{endpoint}", json=payload) as response:
        return await response.json()


async def populate_database(api_url: str, graph_path: str, num_users: int = 100):
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

        # Gerar Couriers (3x mais que users)
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
    # Teste com Alto de Pinheiros, São Paulo
    API_BASE_URL = "http://localhost:8000"  # TODO: Colocar URL do ALB depois
    asyncio.run(
        populate_database(API_BASE_URL, "sp_altodepinheiros.pkl", num_users=100)
    )
