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
COURIERS_TABLE = DYNAMODB.Table('Couriers')

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

def try_reserve_courier(courier_id: str) -> bool:
    '''Tenta fazer uma atualização condicional na tabela dos couriers, mais especificadamente no status deles'''
    try:
        # TODO verificar as estruturas da conexão com o dynamodb
        COURIERS_TABLE.update_item(
            Key={'id': courier_id},
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
    origin = request.merchant_node
    user = request.user_node
    # Ler do RDS, e não receber como entrada
    couriers_set = set(request.available_couriers)

    distances = {origin: 0}
    predecessors = {origin: origin}
    p_queue = {origin: 0}
    selected_courier = None

    user_visited = False
    while p_queue:
        distance_node, node = heapq.heappop(p_queue)

        if distance_node > distances.get(node, float('inf')):
            continue

        # Se ainda não foi encontrado um entregador e o nó atual é um entregador
        if selected_courier is None and node in couriers_set:
            # Tenta reservar o entregador
            # TODO verificação se está localização
            if try_reserve_courier(str(node)): # Se ele estiver disponivel
                # Define o entregador escolido
                selected_courier = str(node)
                # Verifica se o cliente já foi descoberto
                if user_visited: # Se sim, termina a busca
                    break
            else:
                couriers_set.remove(node)

        # Se o nó atual é o cliente e o entregador já foi descoberto termina a busca
        if node==user:
            user_visited = True
            if selected_courier:
                break

        # Visita dos os nós conectados no nó atual
        for unvisited_node, edge_info in GRAPH[node].items():
            distance = edge_info[0].get('length', 1)
            if distance_node + distance < distances.get(unvisited_node, float('inf')):
                distances[unvisited_node] = distance_node + distance
                predecessors[unvisited_node] = node
                heapq.heappush(p_queue, (distances[unvisited_node], unvisited_node))

    # Se a busca terminou em nenhum entregador foi encontrado, um erro é chamado
    if not selected_courier:
        raise HTTPException(status_code=404, detail="No available couriers found.")

    # Retorna na forma esperada
    return RouteResponse(
        courier_id=selected_courier,
        distance_to_merchant=distances[selected_courier],
        path_to_merchant=reconstruct_path(predecessors, int(selected_courier))[::-1],
        distance_to_user= distances[user],
        path_to_user=reconstruct_path(predecessors, user)
    )

@app.get("/health")
async def health():
    return {"status": "ok", "graph_loaded": GRAPH is not None}
