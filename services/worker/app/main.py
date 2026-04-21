import os
import pickle
import heapq
import time
import asyncio
from contextlib import asynccontextmanager
from psycopg2 import pool
from fastapi import FastAPI, HTTPException
from shared.route_models import RouteRequest, RouteResponse
from datetime import datetime

GRAPH = None
GRAPH_LOAD_ERROR = None
DB_POOL = None
DB_POOL_ERROR = None
REQUIRED_TABLES = (
    'courier',
)


def try_load_graph_local() -> bool:
    global GRAPH, GRAPH_LOAD_ERROR
    graph_path = "/app/sp_cidade.pkl"
    try:
        with open(graph_path, 'rb') as f:
            GRAPH = pickle.load(f)
        GRAPH_LOAD_ERROR = None
        return True
    except Exception as error:
        GRAPH = None
        GRAPH_LOAD_ERROR = str(error)
        return False
import os
import pickle
import heapq
import time
import asyncio
from contextlib import asynccontextmanager
from psycopg2 import pool
from fastapi import FastAPI, HTTPException
from shared.route_models import RouteRequest, RouteResponse
from datetime import datetime

GRAPH = None
GRAPH_LOAD_ERROR = None
GRAPH_LAST_LOAD_ATTEMPT = 0.0
DB_POOL = None
DB_POOL_ERROR = None
REQUIRED_TABLES = (
    'courier',
)


def graph_retry_interval_seconds() -> float:
    try:
        return max(1.0, float(os.getenv('GRAPH_RETRY_INTERVAL_SECONDS', '30')))
    except (TypeError, ValueError):
        return 30.0




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
        min_connections = max(0, int(os.getenv('DB_POOL_MIN_CONNECTIONS', '0')))
        max_connections = max(min_connections, int(os.getenv('DB_POOL_MAX_CONNECTIONS', '5')))
        DB_POOL = pool.SimpleConnectionPool(
            minconn=min_connections,
            maxconn=max_connections,
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

    try:
        max_candidates = max(1, int(os.getenv('ROUTE_MAX_AVAILABLE_COURIERS', '500')))
    except (TypeError, ValueError):
        max_candidates = 500

    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT user_id, location FROM courier WHERE availability IS TRUE LIMIT %s',
                (max_candidates,),
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
                'UPDATE courier SET availability = FALSE WHERE user_id = %s AND availability IS TRUE RETURNING user_id',
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
    start_time = datetime.utcnow()
    print(f"[STARTUP] Início do worker: {start_time.isoformat()}Z")
    graph_start = datetime.utcnow()
    print(f"[STARTUP] Iniciando carregamento do grafo local: {graph_start.isoformat()}Z")
    try_load_graph_local()
    graph_end = datetime.utcnow()
    print(f"[STARTUP] Grafo carregado: {graph_end.isoformat()}Z (duração: {(graph_end - graph_start).total_seconds():.2f}s)")
    print(f"[STARTUP] Worker pronto para uso: {graph_end.isoformat()}Z (tempo total desde início: {(graph_end - start_time).total_seconds():.2f}s)")
    yield


app = FastAPI(lifespan=lifespan)


def get_edge_length(edge_info: dict) -> float:
    '''Retorna comprimento de aresta para Graph/DiGraph e MultiGraph/MultiDiGraph.'''
    if not isinstance(edge_info, dict) or not edge_info:
        return 1.0

    # Graph/DiGraph: edge_info ja e o dict de atributos da aresta.
    if 'length' in edge_info:
        try:
            return float(edge_info.get('length', 1) or 1)
        except (TypeError, ValueError):
            return 1.0

    lengths = []
    # MultiGraph/MultiDiGraph: edge_info e um dict de chave->atributos.
    for attributes in edge_info.values():
        if isinstance(attributes, dict):
            try:
                lengths.append(float(attributes.get('length', 1) or 1))
            except (TypeError, ValueError):
                continue

    if not lengths:
        return 1.0
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


def find_distances_for_targets(origin: int, user: int, available_couriers: list[tuple[int, int]]):
    """Executa Dijkstra com parada antecipada para cliente e couriers mais proximos."""
    distances = {origin: 0.0}
    predecessors = {origin: origin}
    p_queue = [(0.0, origin)]

    couriers_by_location: dict[int, list[int]] = {}
    for courier_id, location in available_couriers:
        couriers_by_location.setdefault(location, []).append(courier_id)
    for courier_ids in couriers_by_location.values():
        courier_ids.sort()

    nearest_courier_distance: float | None = None
    candidate_couriers: list[tuple[float, int, int]] = []

    while p_queue:
        distance_node, node = heapq.heappop(p_queue)

        if distance_node > distances.get(node, float('inf')):
            continue

        user_distance = distances.get(user)
        if user_distance is not None and nearest_courier_distance is not None:
            if distance_node > max(user_distance, nearest_courier_distance):
                break

        courier_ids = couriers_by_location.get(node)
        if courier_ids:
            if nearest_courier_distance is None:
                nearest_courier_distance = distance_node
            if distance_node == nearest_courier_distance:
                for courier_id in courier_ids:
                    candidate_couriers.append((distance_node, courier_id, node))

        if node in GRAPH:
            for unvisited_node, edge_info in GRAPH[node].items():
                distance = get_edge_length(edge_info)
                new_dist = distance_node + distance
                if new_dist < distances.get(unvisited_node, float('inf')):
                    distances[unvisited_node] = new_dist
                    predecessors[unvisited_node] = node
                    heapq.heappush(p_queue, (new_dist, unvisited_node))

    candidate_couriers.sort(key=lambda item: (item[0], item[1]))
    return distances, predecessors, candidate_couriers


def find_route_with_available_couriers(origin: int, user: int, available_couriers: list[tuple[int, int]]):
    """Busca todas as rotas alcançáveis e reserva o courier mais perto do restaurante."""
    distances, predecessors, candidate_couriers = find_distances_for_targets(
        origin,
        user,
        available_couriers,
    )

    if user not in distances:
        return None, None, distances, predecessors

    for _distance_to_merchant, courier_id, location in candidate_couriers:
        if reserve_courier(courier_id):
            return courier_id, location, distances, predecessors

    return None, None, distances, predecessors

@app.post("/calculate-route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    if GRAPH is None:
        try_load_graph_local()
    if GRAPH is None:
        raise HTTPException(status_code=503, detail="Grafo não disponível: " + (GRAPH_LOAD_ERROR or "Erro desconhecido"))

    queue_wait_seconds = max(0.0, float(os.getenv('ROUTE_QUEUE_WAIT_SECONDS', '1.5')))
    retry_interval_seconds = max(0.1, float(os.getenv('ROUTE_QUEUE_RETRY_INTERVAL_SECONDS', '0.2')))
    deadline = time.monotonic() + queue_wait_seconds

    origin = request.merchant_node
    user = request.user_node
    while True:
        try:
            available_couriers = fetch_available_couriers()
        except Exception:
            raise HTTPException(status_code=500, detail="Falha ao consultar o couriers disponiveis")

        if not available_couriers:
            if time.monotonic() < deadline:
                await asyncio.sleep(retry_interval_seconds)
                continue
            raise HTTPException(status_code=503, detail="Fila de despacho esgotada: nenhum entregador disponível no momento")

        selected_courier, node_of_courier, distances, predecessors = find_route_with_available_couriers(
            origin,
            user,
            available_couriers,
        )

        if selected_courier and node_of_courier is not None:
            break

        if user not in distances:
            raise HTTPException(status_code=404, detail="Sem rota disponível: Cliente inalcançável")

        if time.monotonic() < deadline:
            await asyncio.sleep(retry_interval_seconds)
            continue

        raise HTTPException(status_code=503, detail="Fila de despacho esgotada: nenhum entregador disponível no momento")

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
        try_load_graph_local()
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
