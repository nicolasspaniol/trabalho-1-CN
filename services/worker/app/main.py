import boto3
import os
import pickle
import heapq
from contextlib import asynccontextmanager
from psycopg2 import pool
from fastapi import FastAPI, HTTPException
from shared.route_models import RouteRequest, RouteResponse

GRAPH = None
GRAPH_LOAD_ERROR = None
DB_POOL = None
DB_POOL_ERROR = None
REQUIRED_TABLES = (
    'courier',
)


def try_load_graph_from_s3() -> bool:
    global GRAPH, GRAPH_LOAD_ERROR

    bucket = os.getenv('MAPAS_BUCKET')
    file_key = os.getenv('MAPAS_FILE', 'sp_altodepinheiros.pkl')

    s3_client = boto3.client('s3')
    local_path = f"/tmp/{os.path.basename(file_key)}"

    try:
        s3_client.download_file(bucket, file_key, local_path)
        with open(local_path, 'rb') as f:
            GRAPH = pickle.load(f)
        GRAPH_LOAD_ERROR = None
        return True
    except Exception as error:
        GRAPH = None
        GRAPH_LOAD_ERROR = str(error)
        return False


def try_init_db_pool() -> bool:
    global DB_POOL, DB_POOL_ERROR

    if DB_POOL is not None:
        return True

    db_host = os.getenv('DB_HOST', '').strip()
    db_name = os.getenv('DB_NAME', 'postgres').strip() or 'postgres'
    db_user = os.getenv('DB_USER', 'postgres').strip() or 'postgres'
    db_password = os.getenv('DB_PASSWORD', '').strip()

    if not db_host or not db_password:
        DB_POOL_ERROR = 'DB_HOST ou DB_PASSWORD nao configurado'
        return False

    try:
        DB_POOL = pool.SimpleConnectionPool(
            minconn=1,
            maxconn=int(os.getenv('DB_POOL_MAX_CONNECTIONS', '5')),
            host=db_host,
            dbname=db_name,
            user=db_user,
            password=db_password,
        )
        DB_POOL_ERROR = None
        return True
    except Exception as error:
        DB_POOL = None
        DB_POOL_ERROR = str(error)
        return False


def fetch_available_couriers() -> list[tuple[int, int]]:
    if not try_init_db_pool():
        raise RuntimeError(DB_POOL_ERROR or 'Falha ao inicializar pool do RDS')

    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT courier_id, location FROM courier WHERE availability IS TRUE'
            )
            rows = cursor.fetchall()
        return [(int(courier_id), int(location)) for courier_id, location in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        DB_POOL.putconn(conn)


def reserve_courier(courier_id: int) -> bool:
    if not try_init_db_pool():
        raise RuntimeError(DB_POOL_ERROR or 'Falha ao inicializar pool do RDS')

    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'UPDATE courier SET availability = FALSE WHERE courier_id = %s AND availability IS TRUE RETURNING courier_id',
                (courier_id,),
            )
            reserved = cursor.fetchone()
        conn.commit()
        return reserved is not None
    except Exception:
        conn.rollback()
        raise
    finally:
        DB_POOL.putconn(conn)


def check_db_health() -> tuple[bool, str | None, list[str]]:
    '''Valida conexão com RDS e presença das tabelas esperadas.''' 
    if not try_init_db_pool():
        return False, DB_POOL_ERROR or 'Falha ao inicializar pool do RDS', list(REQUIRED_TABLES)

    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                ''',
                (list(REQUIRED_TABLES),),
            )
            rows = cursor.fetchall()

        existing_tables = {table_name for (table_name,) in rows}
        missing_tables = [table for table in REQUIRED_TABLES if table not in existing_tables]
        return True, None, missing_tables
    except Exception as error:
        conn.rollback()
        return False, str(error), list(REQUIRED_TABLES)
    finally:
        DB_POOL.putconn(conn)

@asynccontextmanager
async def lifespan(_: FastAPI):
    '''Carrega o grafo da cidade de São Paulo na memória RAM.'''
    try_load_graph_from_s3()

    yield


app = FastAPI(lifespan=lifespan)


def get_edge_length(edge_info: dict) -> float:
    '''Retorna comprimento de aresta para Graph/DiGraph e MultiGraph/MultiDiGraph.'''
    if not isinstance(edge_info, dict) or not edge_info:
        return 1

    # Graph/DiGraph: edge_info ja e o dict de atributos da aresta.
    if 'length' in edge_info:
        return edge_info.get('length', 1)

    lengths = []
    # MultiGraph/MultiDiGraph: edge_info e um dict de chave->atributos.
    for attributes in edge_info.values():
        if isinstance(attributes, dict):
            lengths.append(attributes.get('length', 1))

    if not lengths:
        return 1
    return min(lengths)

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
    if GRAPH is None:
        try_load_graph_from_s3()
    if GRAPH is None:
        raise HTTPException(status_code=503, detail="Grafo não disponível: " + (GRAPH_LOAD_ERROR or "Erro desconhecido"))

    try:
        available_couriers = fetch_available_couriers()
        couriers_map = {location: courier_id for courier_id, location in available_couriers}
    except Exception:
        raise HTTPException(status_code=500, detail="Falha ao consultar o couriers disponiveis")

    origin = request.merchant_node
    user = request.user_node

    distances = {origin: 0}
    predecessors = {origin: origin}
    p_queue = [(0, origin)]
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
            if reserve_courier(courier_id): # Se ele estiver disponivel
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
                distance = get_edge_length(edge_info)
                new_dist = distance_node + distance
                if new_dist < distances.get(unvisited_node, float('inf')):
                    distances[unvisited_node] = new_dist
                    predecessors[unvisited_node] = node
                    heapq.heappush(p_queue, (new_dist, unvisited_node))

    # Se a busca terminou em nenhum entregador foi encontrado, um erro é chamado
    if not selected_courier or not user_visited:
        raise HTTPException(status_code=404, detail="Sem rota disponível: " + ("Nenhum entregador disponível" if not selected_courier else "Cliente inalcançável"))

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
    if GRAPH is None:
        try_load_graph_from_s3()
    db_connected, db_error, missing_tables = check_db_health()
    graph_loaded = GRAPH is not None

    status = 'ok' if graph_loaded and db_connected and not missing_tables else 'degraded'
    return {
        'status': status,
        'graph_loaded': graph_loaded,
        'graph_error': GRAPH_LOAD_ERROR,
        'db_connected': db_connected,
        'db_error': db_error,
        'tables_ok': not missing_tables,
        'missing_tables': missing_tables,
    }