import boto3
import os
import pickle
import heapq
from contextlib import asynccontextmanager
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from shared.models.route_models import RouteRequest, RouteResponse

GRAPH = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    '''Carrega o grafo da cidade de São Paulo na memória RAM.'''
    global GRAPH

    bucket = os.getenv('MAPAS_BUCKET')
    file_key = 'sp_altodepinheiros.pkl'

    s3_client = boto3.client('s3')
    local_path = '/tmp/sp_altodepinheiros.pkl'

    # Baixa o grafo da cidade
    s3_client.download_file(bucket, file_key, local_path)
    # Carrega na memória
    with open(local_path, 'rb') as f:
        GRAPH = pickle.load(f)

    yield


app = FastAPI(lifespan=lifespan)

# Conexão
# TODO atualizar os nomes das conexões
session_config = Config(max_pool_connections=50)
DYNAMODB = boto3.resource('dynamodb', region_name='us-east-1', config=session_config)
COURIERS_TABLE = DYNAMODB.Table('Couriers')

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
    while current in predecessors and predecessors[current] != current:
        path.append(current)
        current = predecessors[current]
    path.append(current)
    return path[::-1]

@app.post("/calculate-route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    try:
        response = COURIERS_TABLE.query(
            IndexName='StatusIndex',  # TODO fazer um index
            KeyConditionExpression=Key('status').eq('AVAILABLE')
        )
        # Dicionário para verificação se um nó tem um entregador
        couriers_map = {int(item['current_node']): item['id'] for item in response['Items']}
    except Exception:
        raise HTTPException(status_code=500, detail="Falha ao consultar o couriers disponiveis")

    origin = request.merchant_node
    user = request.user_node

    distances = {origin: 0}
    predecessors = {origin: origin}
    p_queue = [(0, origin)] # Corrigido para lista para funcionar com heapq
    selected_courier = None
    node_of_courier = None

    user_visited = False
    while p_queue:
        distance_node, node = heapq.heappop(p_queue)

        if distance_node > distances.get(node, float('inf')):
            continue

        # Se ainda não foi encontrado um entregador e o nó atual é um entregador
        courier_id = couriers_map.get(node)
        if selected_courier is None and courier_id is not None:
            # Tenta reservar o entregador
            # TODO verificação se está localização
            if try_reserve_courier(courier_id): # Se ele estiver disponivel
                # Define o entregador escolido
                selected_courier = courier_id
                node_of_courier = node
                # Verifica se o cliente já foi descoberto
                if user_visited: # Se sim, termina a busca
                    break
            else:
                couriers_map.pop(node, None)

        # Se o nó atual é o cliente e o entregador já foi descoberto termina a busca
        if node == user:
            user_visited = True
            if selected_courier:
                break

        # Visita dos os nós conectados no nó atual
        if node in GRAPH:
            for unvisited_node, edge_info in GRAPH[node].items():
                distance = edge_info[0].get('length', 1)
                new_dist = distance_node + distance
                if new_dist < distances.get(unvisited_node, float('inf')):
                    distances[unvisited_node] = new_dist
                    predecessors[unvisited_node] = node
                    heapq.heappush(p_queue, (new_dist, unvisited_node))

    # Se a busca terminou em nenhum entregador foi encontrado, um erro é chamado
    if not selected_courier or not user_visited:
        raise HTTPException(status_code=404, detail="No available couriers found.")

    # Retorna na forma esperada
    return RouteResponse(
        courier_id=selected_courier,
        distance_to_merchant=distances[node_of_courier],
        path_to_merchant=reconstruct_path(predecessors, node_of_courier)[::-1],
        distance_to_user=distances[user],
        path_to_user=reconstruct_path(predecessors, user)
    )

@app.get("/health")
async def health():
    return {"status": "ok", "graph_loaded": GRAPH is not None}