"""Entrypoint unificado para o ciclo de deploy local."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

import local.create as create_data
import local.autoscaling_demo as autoscaling_demo
import local.create_infra as create_infra
import local.delete as delete
import local.fault_tolerance_demo as fault_tolerance_demo
from local.aws_waiters import (
    http_get_json,
    wait_for_dns_resolution,
    wait_for_healthy_targets,
    wait_for_http_ok,
    wait_for_load_balancer_dns,
)
from local.constants import (
    API_LOAD_BALANCER_NAME,
    API_SERVICE_NAME,
    API_TARGET_GROUP_NAME,
    CLUSTER_NAME,
    LOAD_BALANCER_NAME,
    LOCATION_LOAD_BALANCER_NAME,
    LOCATION_SERVICE_NAME,
    LOCATION_TARGET_GROUP_NAME,
    REGION,
    SERVICE_NAME,
    TABLE_NAME,
)

EXECUTION_ROLE_NAME = os.getenv("EXECUTION_ROLE_NAME", "LabRole")
TARGET_GROUP_NAME = "tg-routing-worker"
DB_INSTANCE_ID = os.getenv("DB_INSTANCE_ID", "dijkfood-postgres")
DB_SECURITY_GROUP_NAME = os.getenv("DB_SECURITY_GROUP_NAME", "dijkfood-rds-sg")
DB_SCHEMA_FILE = os.getenv("DB_SCHEMA_FILE", "local/schema.sql")

# TODO(grupo-api): publicar o contrato final de couriers/me/location e picked_up no OpenAPI.
# TODO(grupo-worker): validar o formato final das rotas e o fluxo de courier contra o contrato novo.
# TODO(grupo-dados): definir estrategia final de versionamento/lifecycle do grafo no S3 para o mapa da cidade inteira.
# TODO(grupo-infra): automatizar policy/versioning/lifecycle do bucket S3 do mapa.


def log(message: str) -> None:
    print(f"[cycle] {message}")


def parse_simple_env_file(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_file.exists():
        return values

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


SIM_DURATION_S = int(os.getenv("SIM_DURATION_S", "300"))
SIM_REPORT_INTERVAL_S = int(os.getenv("SIM_REPORT_INTERVAL_S", "30"))


def configure_aws_files() -> None:
    credentials_file = PROJECT_ROOT / ".aws" / "credentials"
    config_file = PROJECT_ROOT / ".aws" / "config"

    if credentials_file.exists():
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials_file)
        log(f"Usando credenciais locais em {credentials_file}")

    if config_file.exists():
        os.environ["AWS_CONFIG_FILE"] = str(config_file)
        log(f"Usando config local em {config_file}")


def resolve_account_id() -> str:
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


def resolve_bucket_name(account_id: str) -> str:
    bucket_name = os.getenv("BUCKET_NAME", "").strip()
    if bucket_name:
        return bucket_name
    return f"dijkfood-assets-sp-{account_id}"


def resolve_db_password(account_id: str) -> str:
    env_password = os.getenv("DB_PASSWORD", "").strip()
    if env_password:
        return env_password

    env_values = parse_simple_env_file(PROJECT_ROOT / "services" / "api" / ".env")
    password = env_values.get("DB_PASSWORD") or env_values.get("POSTGRES_PASSWORD") or env_values.get("MASTER_PASSWORD")
    if password:
        return password

    password = f"DijkFood-{account_id}-Rds!"
    log("DB_PASSWORD nao definido; usando senha padrao gerada para o RDS")
    return password


def resolve_api_auth(api_username: str | None, api_password: str | None, api_auth_env_file: str) -> tuple[str | None, str | None]:
    if api_username and api_password:
        return api_username, api_password

    env_values = parse_simple_env_file(PROJECT_ROOT / api_auth_env_file)
    username = (
        api_username
        or env_values.get("ADMIN_USERNAME")
        or os.getenv("ADMIN_USERNAME")
        or os.getenv("API_USERNAME")
    )
    password = (
        api_password
        or env_values.get("ADMIN_PASSWORD")
        or os.getenv("ADMIN_PASSWORD")
        or os.getenv("API_PASSWORD")
    )

    if not (username and password):
        username = "admin"
        password = "admin"
        log("Simulacao: credenciais nao informadas; usando fallback admin/admin")

    if username and password:
        log("Simulacao: usando credenciais Basic Auth da API")
    else:
        log("Simulacao: sem credenciais Basic Auth (API publica ou contrato ainda sem auth)")
    return username, password


def run_python_script(
    script_path: Path,
    args: list[str],
    env: dict[str, str] | None = None,
    *,
    timeout_seconds: int | None = None,
) -> None:
    # -u: evita buffering (importante para observar progresso no deploy/simulacao).
    cmd = [sys.executable, "-u", str(script_path), *args]
    final_env = os.environ.copy()
    final_env.setdefault("PYTHONUNBUFFERED", "1")
    if env:
        final_env.update(env)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True, env=final_env, timeout=timeout_seconds)


def describe_ecs_service_tasks(ecs, cluster: str, services: list[str]) -> dict[str, dict[str, int]]:
    response = ecs.describe_services(cluster=cluster, services=services)
    result: dict[str, dict[str, int]] = {}
    for service in response.get("services", []):
        name = service.get("serviceName")
        if not name:
            continue
        result[str(name)] = {
            "desired": int(service.get("desiredCount", 0)),
            "running": int(service.get("runningCount", 0)),
            "pending": int(service.get("pendingCount", 0)),
        }
    return result


def read_json_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def simulation_reporter(
    *,
    stop_event: threading.Event,
    ecs,
    cluster: str,
    services: list[str],
    metrics_path: str,
) -> None:
    while not stop_event.is_set():
        stop_event.wait(SIM_REPORT_INTERVAL_S)
        if stop_event.is_set():
            break

        svc_counts = describe_ecs_service_tasks(ecs, cluster, services)
        metrics = read_json_file(metrics_path) or {}

        svc_summary = " | ".join(
            f"{name}: {counts.get('running', 0)}/{counts.get('desired', 0)} (pend={counts.get('pending', 0)})"
            for name, counts in svc_counts.items()
        )

        elapsed_s = metrics.get("elapsed_s")
        write_success = metrics.get("write_success")
        write_attempts = metrics.get("write_attempts")
        target_rps = metrics.get("target_rps")
        effective_write_rps = metrics.get("effective_write_rps")
        p95_write_ms = metrics.get("p95_write_ms")
        p95_read_ms = metrics.get("p95_read_ms")

        log(
            "Simulacao (30s): "
            f"tasks={svc_summary or 'NA'}; "
            f"pedidos_ok={write_success if write_success is not None else 'NA'}/{write_attempts if write_attempts is not None else 'NA'}; "
            f"elapsed_s={int(elapsed_s) if isinstance(elapsed_s, (int, float)) else 'NA'}; "
            f"rps_target={target_rps if target_rps is not None else 'NA'} "
            f"rps_eff={f'{effective_write_rps:.2f}' if isinstance(effective_write_rps, (int, float)) else 'NA'}; "
            f"p95_write_ms={f'{p95_write_ms:.2f}' if isinstance(p95_write_ms, (int, float)) else 'NA'} "
            f"p95_read_ms={f'{p95_read_ms:.2f}' if isinstance(p95_read_ms, (int, float)) else 'NA'}"
        )


def resolve_openapi_paths(base_url: str) -> set[str] | None:
    try:
        payload = http_get_json(f"{base_url}/openapi.json")
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return None
    return set(paths.keys())


def normalize_openapi_paths(paths: set[str]) -> set[str]:
    normalized: set[str] = set()
    for path in paths:
        clean = path.strip()
        if not clean:
            continue
        normalized.add(clean.rstrip("/") or "/")
    return normalized


def supports_data_load(base_url: str) -> bool:
    paths = resolve_openapi_paths(base_url)
    if paths is None:
        return True

    normalized_paths = normalize_openapi_paths(paths)
    required_load_paths = {"/customers", "/merchants", "/couriers"}
    return required_load_paths <= normalized_paths


def ensure_simulation_contract(base_url: str, location_base_url: str | None = None) -> bool:
    """Valida se o ALB expoe o contrato necessario para simulacao completa.

    Retorna True quando a API suporta o contrato final de courier.
    Retorna False quando apenas a carga inicial pode rodar.
    """
    try:
        hello_payload = http_get_json(f"{base_url}/hello")
        if isinstance(hello_payload, dict) and hello_payload.get("service") == "routing-worker":
            raise RuntimeError(
                "Simulacao indisponivel: o ALB atual aponta para o worker em modo teste "
                "(services/worker/app/main2.py), sem endpoints de dominio (/customers, /merchants, /couriers, /orders, /couriers/me/location, /orders/{order_id}/ready, /orders/{order_id}/picked_up)."
            )
    except RuntimeError:
        raise
    except Exception:
        pass

    paths = resolve_openapi_paths(base_url)
    location_paths = resolve_openapi_paths(location_base_url) if location_base_url else None
    if paths is None and location_paths is None:
        log("Simulacao: OpenAPI indisponivel, seguindo sem validacao previa de contrato")
        return True

    normalized_paths = set()
    if paths is not None:
        normalized_paths.update(normalize_openapi_paths(paths))
    if location_paths is not None:
        normalized_paths.update(normalize_openapi_paths(location_paths))

    required_load_paths = {"/customers", "/merchants", "/couriers"}
    missing_load_paths = sorted(required_load_paths - normalized_paths)
    if missing_load_paths:
        log(
            "Simulacao: endpoints de carga ausentes no OpenAPI; seguindo sem carga via API: "
            + ", ".join(missing_load_paths)
        )
        return False

    required_new_delivery_paths = {
        "/orders",
        "/couriers/me/order",
        "/orders/{order_id}/accept",
        "/orders/{order_id}/ready",
        "/couriers/me/location",
        "/orders/{order_id}/picked_up",
    }

    if required_new_delivery_paths <= normalized_paths:
        log("Simulacao: contrato completo de courier detectado (accept + ready + couriers/me/location + picked_up)")
        return True

    missing_new_paths = sorted(required_new_delivery_paths - normalized_paths)
    log(
        "Simulacao parcial: contrato final de courier incompleto; faltando: "
        + ", ".join(missing_new_paths)
    )
    return False


def run_simulation(
    base_url: str,
    num_users: int,
    rps: int,
    sim_duration_s: int,
    api_username: str | None,
    api_password: str | None,
    bucket_name: str,
    graph_file: str,
    graph_location: str,
    location_base_url: str | None,
) -> None:
    delivery_process = None
    can_load_data = supports_data_load(base_url)
    full_simulation_supported = ensure_simulation_contract(base_url, location_base_url)
    auth_env: dict[str, str] = {}
    if api_username and api_password:
        auth_env = {"API_USERNAME": api_username, "API_PASSWORD": api_password}
    try:
        if not can_load_data:
            log("Simulacao: pulando carga/sim_delivery/load-test por falta de endpoints de carga; deploy segue normalmente")
            return

        log("Simulacao: preflight de endpoints (OpenAPI + fluxo critico)")
        run_python_script(
            PROJECT_ROOT / "local" / "validate_endpoints.py",
            ["--api-url", base_url, "--username", api_username or "admin", "--password", api_password or "admin"],
            env=auth_env,
        )

        log("Simulacao: populando base via API")
        run_python_script(
            PROJECT_ROOT / "local" / "load.py",
            ["--api-url", base_url, "--num-users", str(num_users)],
            env={**auth_env, "SIM_GRAPH_PATH": graph_file},
        )

        if not full_simulation_supported:
            log("Simulacao: fase de pedidos/entregas ignorada por incompatibilidade de contrato")
            return

        log("Simulacao: iniciando sim_delivery em background")
        delivery_process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(PROJECT_ROOT / "local" / "sim_delivery.py"),
                "--api-url",
                base_url,
                "--location-api-url",
                location_base_url or base_url,
            ],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1", **auth_env},
        )

        time.sleep(3)

        log(f"Simulacao: load test {rps} RPS por {sim_duration_s}s")
        metrics_path = f"/tmp/dijkfood_sim_metrics_{int(time.time())}.json"
        ecs = boto3.client("ecs", region_name=REGION)
        stop_event = threading.Event()
        reporter = threading.Thread(
            target=simulation_reporter,
            kwargs={
                "stop_event": stop_event,
                "ecs": ecs,
                "cluster": CLUSTER_NAME,
                "services": [SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME],
                "metrics_path": metrics_path,
            },
            daemon=True,
        )
        reporter.start()
        try:
            run_python_script(
                PROJECT_ROOT / "local" / "sim_client.py",
                ["--api-url", base_url, "--rps", str(rps), "--duration", str(sim_duration_s)],
                env={**auth_env, "SIM_METRICS_PATH": metrics_path, "SIM_REPORT_INTERVAL_S": str(SIM_REPORT_INTERVAL_S)},
                timeout_seconds=sim_duration_s + 300,
            )
        finally:
            stop_event.set()
            reporter.join(timeout=2)
    finally:
        if delivery_process is not None:
            log("Simulacao: encerrando sim_delivery")
            delivery_process.terminate()
            delivery_process.wait()


def resolve_simulation_base_url(worker_base_url: str, override_api_url: str | None) -> str:
    if override_api_url and override_api_url.strip():
        chosen = override_api_url.strip().rstrip("/")
        log(f"Simulacao: usando API dedicada em {chosen}")
        return chosen
    return worker_base_url


def ensure_graph_in_s3(bucket_name: str, graph_file: str, graph_location: str) -> None:
    log("Dados: gerando grafo e enviando para S3")
    run_python_script(
        PROJECT_ROOT / "local" / "create.py",
        ["--bucket", bucket_name, "--file", graph_file, "--location", graph_location],
    )


def run_deployment(execution_role_arn: str, bucket_name: str) -> None:
    log("Deploy dados: garantindo bucket S3")
    create_data.setup_s3_bucket(bucket_name)

    log("Deploy dados: garantindo tabela DynamoDB")
    create_data.setup_dynamo(TABLE_NAME)

    log("Deploy dados: garantindo SG e instancia RDS")
    sg_id = create_data.setup_security_group(DB_SECURITY_GROUP_NAME)
    db_endpoint = create_data.setup_rds(DB_INSTANCE_ID, sg_id)
    if not db_endpoint:
        raise RuntimeError("Falha ao criar/obter endpoint do RDS")
    os.environ["DB_HOST"] = db_endpoint

    schema_path = PROJECT_ROOT / DB_SCHEMA_FILE
    if schema_path.exists():
        log(f"Deploy dados: aplicando schema em {schema_path}")
        loaded = create_data.load_schema_to_rds(db_endpoint, str(schema_path))
        if not loaded:
            raise RuntimeError("Falha ao carregar schema SQL no RDS")
    else:
        log(f"Deploy dados: schema nao encontrado em {schema_path}, seguindo sem carga SQL")

    create_infra.setup_worker_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=SERVICE_NAME,
        table_name=TABLE_NAME,
        bucket_name=bucket_name,
        execution_role_arn=execution_role_arn,
    )

    worker_alb_dns = str(
        wait_for_load_balancer_dns(
            boto3.client("elbv2", region_name=REGION),
            LOAD_BALANCER_NAME,
            log=log,
        )
    )

    log("Deploy location: criando servico de telemetria")
    create_infra.setup_location_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=LOCATION_SERVICE_NAME,
        execution_role_arn=execution_role_arn,
    )

    location_alb_dns = str(
        wait_for_load_balancer_dns(
            boto3.client("elbv2", region_name=REGION),
            LOCATION_LOAD_BALANCER_NAME,
            log=log,
        )
    )

    log("Deploy API: criando servico FastAPI de dominio")
    create_infra.setup_api_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=API_SERVICE_NAME,
        worker_base_url=f"http://{worker_alb_dns}",
        location_base_url=f"http://{location_alb_dns}",
        db_host=os.environ["DB_HOST"],
        db_password=os.environ["DB_PASSWORD"],
        execution_role_arn=execution_role_arn,
    )


def run_pre_deploy_setup() -> None:
    setup_script = PROJECT_ROOT / "local" / "pre_deploy_setup.sh"
    log("Pre-setup")
    subprocess.run(["bash", str(setup_script)], cwd=str(PROJECT_ROOT), check=True)


def require_execution_role() -> str:
    execution_role_arn = os.getenv("EXECUTION_ROLE_ARN")
    if not execution_role_arn:
        sts = boto3.client("sts", region_name=REGION)
        account_id = sts.get_caller_identity()["Account"]
        execution_role_arn = f"arn:aws:iam::{account_id}:role/{EXECUTION_ROLE_NAME}"
    return execution_role_arn


def wait_service_ready(alb_dns: str, target_group_name: str) -> None:
    log("Resolucao DNS")
    wait_for_dns_resolution(alb_dns, log=log)

    log("Health do target")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    wait_for_healthy_targets(elbv2, target_group_name, log=log)


def test_api_service(base_url: str) -> None:
    health_url = f"{base_url}/health"
    openapi_url = f"{base_url}/openapi.json"
    log("Teste API /health")
    wait_for_http_ok(health_url, log=log)
    log("Teste API /openapi.json")
    wait_for_http_ok(openapi_url, log=log)


def test_location_service(base_url: str) -> None:
    health_url = f"{base_url}/health"
    log("Teste location /health")
    wait_for_http_ok(health_url, log=log)


def run_autoscaling_demo(wait_seconds: int) -> None:
    log("Demo autoscaling: exibindo estado dos servicos ECS")
    autoscaling_demo.main(["--wait-seconds", str(wait_seconds)])


def run_fault_demo(api_base_url: str, worker_base_url: str, location_base_url: str) -> None:
    log("Demo fault tolerance: derrubando uma task por servico e validando disponibilidade no ALB")
    fault_tolerance_demo.main(
        [
            "--skip-rds",
            "--health-urls",
            f"{api_base_url}/health,{worker_base_url}/health,{location_base_url}/health",
        ]
    )


def test_service(
    alb_dns: str,
    target_group_name: str,
    include_readiness: bool = True,
    require_graph_loaded: bool = True,
    require_db_connected: bool = True,
    require_tables_ok: bool = True,
) -> None:
    base_url = f"http://{alb_dns}"

    if include_readiness:
        wait_service_ready(alb_dns, target_group_name)

    health_url = f"{base_url}/health"
    log("Teste /health")
    wait_for_http_ok(health_url, log=log)
    health_payload = http_get_json(health_url)
    if not isinstance(health_payload, dict):
        # Alguns servicos expõem /health minimalista (boolean).
        if health_payload is True and not (require_graph_loaded or require_db_connected or require_tables_ok):
            return
        raise RuntimeError('Health check falhou: payload invalido.')

    if require_graph_loaded:
        graph_loaded = bool(health_payload.get('graph_loaded'))
        if not graph_loaded:
            graph_error = health_payload.get('graph_error')
            raise RuntimeError(
                "Health check falhou: grafo nao carregado na task. "
                f"graph_error={graph_error!r}"
            )

    if require_db_connected:
        db_connected = bool(health_payload.get('db_connected'))
        if not db_connected:
            db_error = health_payload.get('db_error')
            raise RuntimeError(
                'Health check falhou: conexao com RDS indisponivel na task. '
                f'db_error={db_error!r}'
            )

    if require_tables_ok:
        tables_ok = bool(health_payload.get('tables_ok'))
        if not tables_ok:
            missing_tables = health_payload.get('missing_tables')
            raise RuntimeError(
                'Health check falhou: tabelas obrigatorias ausentes no RDS. '
                f'missing_tables={missing_tables!r}'
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cria, testa e opcionalmente apaga a infraestrutura")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-delete", action="store_true", help="Nao executa o teardown no final.")
    mode.add_argument("--only-delete", action="store_true", help="Executa apenas o teardown.")
    parser.add_argument("--with-simulation", action="store_true", help="Executa simulacao de carga apos os smoke tests.")
    parser.add_argument("--num-users", type=int, default=100, help="Quantidade base de usuarios para load.py")
    parser.add_argument("--rps", type=int, default=50, help="Pedidos por segundo na simulacao")
    parser.add_argument(
        "--sim-duration",
        type=int,
        default=SIM_DURATION_S,
        help="Duracao do teste de carga em segundos.",
    )
    parser.add_argument("--api-username", default=None, help="Usuario Basic Auth da API para simulacao")
    parser.add_argument("--api-password", default=None, help="Senha Basic Auth da API para simulacao")
    parser.add_argument(
        "--with-autoscaling-demo",
        action="store_true",
        help="Exibe o estado/configuracao do autoscaling apos a simulacao.",
    )
    parser.add_argument(
        "--autoscaling-wait-seconds",
        type=int,
        default=90,
        help="Tempo de espera antes de coletar o estado do autoscaling.",
    )
    parser.add_argument(
        "--with-fault-demo",
        action="store_true",
        help="Derruba uma task por servico e valida recuperacao com o ALB.",
    )
    parser.add_argument(
        "--graph-file",
        default="sp_altodepinheiros.pkl",
        help="Nome do arquivo local do grafo usado na simulacao",
    )
    parser.add_argument(
        "--graph-location",
        default="Alto de Pinheiros, Sao Paulo, Brazil",
        help="Regiao usada para gerar o grafo no create.py",
    )
    parser.add_argument(
        "--api-auth-env-file",
        default="services/api/.env",
        help="Arquivo .env com ADMIN_USERNAME/ADMIN_PASSWORD para usar na simulacao",
    )
    parser.add_argument(
        "--simulation-api-url",
        default=None,
        help="URL base da API de dominio para simulacao (quando o ALB deste deploy aponta para worker)",
    )
    args = parser.parse_args(argv)

    configure_aws_files()

    if args.only_delete:
        log("Teardown")
        delete.main([])
        log("Fim")
        return 0

    run_pre_deploy_setup()

    account_id = resolve_account_id()
    bucket_name = resolve_bucket_name(account_id)
    db_password = resolve_db_password(account_id)
    os.environ["BUCKET_NAME"] = bucket_name
    os.environ["DB_PASSWORD"] = db_password
    os.environ["MAPAS_FILE"] = args.graph_file

    execution_role_arn = require_execution_role()
    os.environ["EXECUTION_ROLE_ARN"] = execution_role_arn

    log("Deploy dados: garantindo bucket S3 antes do upload do grafo")
    create_data.setup_s3_bucket(bucket_name)
    ensure_graph_in_s3(bucket_name, args.graph_file, args.graph_location)

    log("Deploy")
    run_deployment(execution_role_arn, bucket_name)

    log("ALB worker")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    worker_alb_dns = str(wait_for_load_balancer_dns(elbv2, LOAD_BALANCER_NAME, log=log))
    log(f"DNS worker: {worker_alb_dns}")

    log("ALB API")
    api_alb_dns = str(wait_for_load_balancer_dns(elbv2, API_LOAD_BALANCER_NAME, log=log))
    log(f"DNS API: {api_alb_dns}")

    log("ALB location")
    location_alb_dns = str(wait_for_load_balancer_dns(elbv2, LOCATION_LOAD_BALANCER_NAME, log=log))
    log(f"DNS location: {location_alb_dns}")

    worker_base_url = f"http://{worker_alb_dns}"
    api_base_url = f"http://{api_alb_dns}"
    location_base_url = f"http://{location_alb_dns}"

    if args.with_simulation:
        log("Readiness worker")
        wait_service_ready(worker_alb_dns, TARGET_GROUP_NAME)
        log("Readiness API")
        wait_service_ready(api_alb_dns, API_TARGET_GROUP_NAME)
        log("Readiness location")
        wait_service_ready(location_alb_dns, LOCATION_TARGET_GROUP_NAME)

        simulation_base_url = resolve_simulation_base_url(api_base_url, args.simulation_api_url)
        if simulation_base_url == api_base_url and not supports_data_load(simulation_base_url):
            raise RuntimeError(
                "Simulacao indisponivel na URL atual: o endpoint publico da API nao expoe /customers, /merchants e /couriers. "
                "Use --simulation-api-url apontando para a API FastAPI de dominio."
            )

        api_username, api_password = resolve_api_auth(args.api_username, args.api_password, args.api_auth_env_file)
        try:
            run_simulation(
                base_url=simulation_base_url,
                num_users=args.num_users,
                rps=args.rps,
                sim_duration_s=args.sim_duration,
                api_username=api_username,
                api_password=api_password,
                bucket_name=bucket_name,
                graph_file=args.graph_file,
                graph_location=args.graph_location,
                location_base_url=location_base_url,
            )
        except RuntimeError as error:
            log(str(error))
            return 2

        if args.with_autoscaling_demo:
            run_autoscaling_demo(args.autoscaling_wait_seconds)

        if args.with_fault_demo:
            run_fault_demo(api_base_url, worker_base_url, location_base_url)

        log("Testes")
        test_service(
            worker_alb_dns,
            TARGET_GROUP_NAME,
            include_readiness=False,
            require_graph_loaded=True,
            require_db_connected=True,
            require_tables_ok=True,
        )
        test_api_service(api_base_url)
        test_location_service(location_base_url)
    else:
        log("Testes")
        test_service(
            worker_alb_dns,
            TARGET_GROUP_NAME,
            include_readiness=True,
            require_graph_loaded=True,
            require_db_connected=True,
            require_tables_ok=True,
        )
        test_service(
            api_alb_dns,
            API_TARGET_GROUP_NAME,
            include_readiness=True,
            require_graph_loaded=False,
            require_db_connected=False,
            require_tables_ok=False,
        )
        test_service(
            location_alb_dns,
            LOCATION_TARGET_GROUP_NAME,
            include_readiness=True,
            require_graph_loaded=False,
            require_db_connected=False,
            require_tables_ok=False,
        )

        if args.with_autoscaling_demo:
            run_autoscaling_demo(args.autoscaling_wait_seconds)

        if args.with_fault_demo:
            run_fault_demo(api_base_url, worker_base_url, location_base_url)

    if args.no_delete:
        log("Teardown: ignorado")
        log("Fim")
        return 0

    log("Teardown")
    delete.main([])
    log("Fim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
