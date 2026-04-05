"""Entrypoint unificado para o ciclo de deploy local."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

import local.create as create_data
import local.create_infra as create_infra
import local.delete as delete
from local.aws_waiters import (
    http_get_json,
    wait_for_dns_resolution,
    wait_for_healthy_targets,
    wait_for_http_ok,
    wait_for_load_balancer_dns,
)
from local.constants import (
    CLUSTER_NAME,
    LOAD_BALANCER_NAME,
    REGION,
    SERVICE_NAME,
    TABLE_NAME,
)

EXECUTION_ROLE_NAME = os.getenv("EXECUTION_ROLE_NAME", "LabRole")
TARGET_GROUP_NAME = "tg-routing-worker"
DB_INSTANCE_ID = os.getenv("DB_INSTANCE_ID", "dijkfood-postgres")
DB_SECURITY_GROUP_NAME = os.getenv("DB_SECURITY_GROUP_NAME", "dijkfood-rds-sg")
DB_SCHEMA_FILE = os.getenv("DB_SCHEMA_FILE", "local/schema.sql")

# TODO(grupo-api): alinhar contratos finais de orders e locations no OpenAPI.
# TODO(grupo-worker): validar formato final das rotas retornadas para sim_delivery.py.
# TODO(grupo-dados): definir estrategia final de versionamento/lifecycle do grafo no S3 para o mapa da cidade inteira.
# TODO(grupo-infra): automatizar policy/versioning/lifecycle do bucket S3 do mapa.
# TODO(grupo-infra): fechar a automacao dos bancos/recursos de dados faltantes que ainda nao estiverem cobertos.


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
    username = api_username or env_values.get("ADMIN_USERNAME") or os.getenv("API_USERNAME")
    password = api_password or env_values.get("ADMIN_PASSWORD") or os.getenv("API_PASSWORD")

    if username and password:
        log("Simulacao: usando credenciais Basic Auth da API")
    else:
        log("Simulacao: sem credenciais Basic Auth (API publica ou contrato ainda sem auth)")
    return username, password


def parse_rps_scenarios(value: str) -> list[int]:
    scenarios = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        scenarios.append(int(item))
    if not scenarios:
        raise ValueError("Lista de RPS vazia. Exemplo valido: 10,50,200")
    return scenarios


def run_python_script(script_path: Path, args: list[str], env: dict[str, str] | None = None) -> None:
    cmd = [sys.executable, str(script_path), *args]
    final_env = os.environ.copy()
    if env:
        final_env.update(env)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True, env=final_env)


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


def ensure_simulation_contract(base_url: str) -> bool:
    """Valida se o ALB expõe o contrato necessário para simulacao completa.

    Retorna True quando os endpoints de carga e entrega estão presentes.
    Retorna False quando apenas a carga inicial pode rodar.
    """
    try:
        hello_payload = http_get_json(f"{base_url}/hello")
        if isinstance(hello_payload, dict) and hello_payload.get("service") == "routing-worker":
            raise RuntimeError(
                "Simulacao indisponivel: o ALB atual aponta para o worker em modo teste "
                "(services/worker/app/main2.py), sem endpoints de dominio (/customers, /merchants, /couriers, /orders, /locations)."
            )
    except RuntimeError:
        raise
    except Exception:
        # Se /hello nao existir, seguimos para validacao via OpenAPI.
        pass

    paths = resolve_openapi_paths(base_url)
    if paths is None:
        log("Simulacao: OpenAPI indisponivel, seguindo sem validacao previa de contrato")
        return True

    required_load_paths = {"/customers/", "/merchants/", "/couriers/"}
    missing_load_paths = sorted(required_load_paths - paths)
    if missing_load_paths:
        raise RuntimeError(
            "Simulacao indisponivel: endpoints de carga nao encontrados no OpenAPI: "
            + ", ".join(missing_load_paths)
        )

    required_delivery_paths = {"/orders/", "/orders/{id}", "/orders/{id}/status", "/locations"}
    missing_delivery_paths = sorted(required_delivery_paths - paths)
    if missing_delivery_paths:
        log(
            "Simulacao parcial: endpoints de pedidos/telemetria ausentes no OpenAPI; "
            "sera executada apenas a populacao de dados. Faltando: "
            + ", ".join(missing_delivery_paths)
        )
        return False

    return True


def run_simulation(
    base_url: str,
    num_users: int,
    rps_scenarios: list[int],
    duration: int,
    cooldown_seconds: int,
    api_username: str | None,
    api_password: str | None,
    bucket_name: str,
    graph_file: str,
    graph_location: str,
) -> None:
    delivery_process = None
    full_simulation_supported = ensure_simulation_contract(base_url)
    auth_args: list[str] = []
    auth_env: dict[str, str] = {}
    if api_username and api_password:
        auth_args = ["--username", api_username, "--password", api_password]
        auth_env = {"API_USERNAME": api_username, "API_PASSWORD": api_password}
    try:
        log("Simulacao: gerando grafo e enviando para S3")
        run_python_script(
            PROJECT_ROOT / "local" / "create.py",
            ["--bucket", bucket_name, "--file", graph_file, "--location", graph_location],
        )

        log("Simulacao: populando base via API")
        run_python_script(
            PROJECT_ROOT / "local" / "load.py",
            ["--api-url", base_url, "--num-users", str(num_users), "--graph-path", graph_file, *auth_args],
            env=auth_env,
        )

        if not full_simulation_supported:
            log("Simulacao: fase de pedidos/entregas ignorada por incompatibilidade de contrato")
            return

        log("Simulacao: iniciando sim_delivery em background")
        delivery_process = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "local" / "sim_delivery.py"), "--api-url", base_url, *auth_args],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, **auth_env},
        )

        time.sleep(3)

        for rps in rps_scenarios:
            log(f"Simulacao: load test {rps} RPS")
            run_python_script(
                PROJECT_ROOT / "local" / "sim_client.py",
                ["--api-url", base_url, "--rps", str(rps), "--duration", str(duration), *auth_args],
                env=auth_env,
            )
            time.sleep(cooldown_seconds)
    finally:
        if delivery_process is not None:
            log("Simulacao: encerrando sim_delivery")
            delivery_process.terminate()
            delivery_process.wait()


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


def test_service(alb_dns: str) -> None:
    base_url = f"http://{alb_dns}"

    log("Resolucao DNS")
    wait_for_dns_resolution(alb_dns, log=log)

    log("Health do target")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    wait_for_healthy_targets(elbv2, TARGET_GROUP_NAME, log=log)

    health_url = f"{base_url}/health"
    log("Teste /health")
    wait_for_http_ok(health_url, log=log)

    hello_url = f"{base_url}/hello?name=world"
    log("Teste /hello")
    http_get_json(hello_url)

    burn_url = f"{base_url}/cpu-burn?seconds=5&payload_kb=32"
    log("Teste /cpu-burn")
    http_get_json(burn_url, timeout=30)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cria, testa e opcionalmente apaga a infraestrutura")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-delete", action="store_true", help="Nao executa o teardown no final.")
    mode.add_argument("--only-delete", action="store_true", help="Executa apenas o teardown.")
    parser.add_argument("--with-simulation", action="store_true", help="Executa simulacao de carga apos os smoke tests.")
    parser.add_argument("--num-users", type=int, default=100, help="Quantidade base de usuarios para load.py")
    parser.add_argument("--rps-scenarios", default="10,50,200", help="Lista de cenarios de carga em RPS (csv)")
    parser.add_argument("--duration", type=int, default=30, help="Duracao de cada cenario de carga em segundos")
    parser.add_argument("--cooldown-seconds", type=int, default=5, help="Pausa entre cenarios para o autoscaling respirar")
    parser.add_argument("--api-username", default=None, help="Usuario Basic Auth da API para simulacao")
    parser.add_argument("--api-password", default=None, help="Senha Basic Auth da API para simulacao")
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

    execution_role_arn = require_execution_role()
    os.environ["EXECUTION_ROLE_ARN"] = execution_role_arn

    log("Deploy")
    run_deployment(execution_role_arn, bucket_name)

    log("ALB")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    alb_dns = str(wait_for_load_balancer_dns(elbv2, LOAD_BALANCER_NAME, log=log))
    log(f"DNS: {alb_dns}")

    log("Testes")
    test_service(alb_dns)

    if args.with_simulation:
        rps_scenarios = parse_rps_scenarios(args.rps_scenarios)
        base_url = f"http://{alb_dns}"
        api_username, api_password = resolve_api_auth(args.api_username, args.api_password, args.api_auth_env_file)
        run_simulation(
            base_url=base_url,
            num_users=args.num_users,
            rps_scenarios=rps_scenarios,
            duration=args.duration,
            cooldown_seconds=args.cooldown_seconds,
            api_username=api_username,
            api_password=api_password,
            bucket_name=bucket_name,
            graph_file=args.graph_file,
            graph_location=args.graph_location,
        )

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
