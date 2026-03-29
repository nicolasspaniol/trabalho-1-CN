import boto3
import os
import osmnx as ox
import pickle
import heapq
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from shared.models.route_models import RouteRequest, RouteResponse

app = FastAPI()

GRAPH = None
# Conexão
# TODO atualizar os nomes das conexões
DYNAMODB = boto3.resource('dynamodb', region_name='us-east-1')
DRIVERS_TABLE = DYNAMODB.Table('Entregadores')

@app.on_event("startup")
async def startup_event():
    '''Carrega o grafo da cidade de São Paulo na memória RAM'''
    global GRAPH

    bucket = os.getenv('MAPAS_BUCKET')
    file_key = 'sp_graph.graphml'

    s3_client = boto3.client('s3')
    # TODO alterar para o nome correto
    local_path = '/tmp/graph.pkl'

    # Baixa o grafo da cidade
    s3_client.download_file(bucket, file_key, local_path)
    # Carrega na memória
    with open(local_path, 'rb') as f:
        GRAPH = pickle.load(f)

def try_reserve_driver(driver_id: str) -> bool:
    '''Tenta fazer uma atualização condicional na tabela dos drivers, mais especificadamente no status deles'''
    try:
        # TODO verificar as estruturas da conexão com o dynamodb
        DRIVERS_TABLE.update_item(
            Key={'id': driver_id},
            ConditionExpression="#s = :avail",
            UpdateExpression="SET #s = :busy",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':busy': 'BUSY', ':avail': 'AVAILABLE'}
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise e

def reconstruct_path(predecessors: dict, target: int) -> list[int]:
    '''Reconstroi o caminho encontrado pelo Dilkstra'''
    path = []
    current = target
    while current in predecessors:
        path.append(current)
        current = predecessors[current]
    return path[::-1]

@app.post("/calculate-route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    origin = request.restaurant_node
    client = request.client_node
    drivers_set = set(request.available_drivers)

    distances = {origin: 0}
    predecessors = {origin: origin}
    p_queue = {origin: 0}
    selected_driver = None

    client_visited = False
    while p_queue:
        distance_node, node = heapq.heappop(p_queue)

        if distance_node > distances.get(node, float('inf')):
            continue

        # Se ainda não foi encontrado um entregador e o nó atual é um entregador
        if selected_driver is None and node in drivers_set:
            # Tenta reservar o entregador
            if try_reserve_driver(str(node)): # Se ele estiver disponivel
                # Define o entregador escolido
                selected_driver = str(node)
                # Verifica se o cliente já foi descoberto
                if client_visited: # Se sim, termina a busca
                    break
            else:
                drivers_set.remove(node)

        # Se o nó atual é o cliente e o entregador já foi descoberto termina a busca
        if node==client:
            client_visited = True
            if selected_driver:
                break

        # Visita dos os nós conectados no nó atual
        for unvisited_node, edge_info in GRAPH[node].items():
            distance = edge_info[0].get('length', 1)
            if distance_node + distance < distances.get(unvisited_node, float('inf')):
                distances[unvisited_node] = distance_node + distance
                predecessors[unvisited_node] = node
                heapq.heappush(p_queue, (distances[unvisited_node], unvisited_node))

    # Se a busca terminou em nenhum entregador foi encontrado, um erro é chamado
    if not selected_driver:
        raise HTTPException(status_code=404, detail="No available drivers found.")

    # Retorna na forma esperada
    return RouteResponse(
        driver_id=selected_driver,
        distance_to_restaurant=distances[selected_driver],
        path_to_restaurant=reconstruct_path(predecessors, int(selected_driver))[::-1],
        distance_to_client= distances[client],
        path_to_client=reconstruct_path(predecessors, client)
    )

@app.get("/health")
async def health():
    return {"status": "ok", "graph_loaded": GRAPH is not None}
