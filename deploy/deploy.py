import os
import argparse
import socket
import sys
import time
from pathlib import Path
from os import getenv
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

import local.create as create
import local.delete as delete

# Configurações Globais
REGION = "us-east-1"
CLUSTER_NAME = "DijkFood-Cluster"
SERVICE_NAME = "Routing-Worker-Service"
TABLE_NAME = "Couriers"
BUCKET_NAME = "dijkfood-assets-sp"
EXECUTION_ROLE_ARN = getenv("EXECUTION_ROLE_ARN")
EXECUTION_ROLE_NAME = getenv("EXECUTION_ROLE_NAME", "LabRole")
LOAD_BALANCER_NAME = "alb-routing-worker"


def log(message: str) -> None:
    print(f"[cycle] {message}")


def run_deployment():
    execution_role_arn = getenv("EXECUTION_ROLE_ARN") or EXECUTION_ROLE_ARN
    create.setup_worker_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=SERVICE_NAME,
        table_name=TABLE_NAME,
        bucket_name=BUCKET_NAME,
        execution_role_arn=execution_role_arn
    )


def require_execution_role() -> str:
    execution_role_arn = getenv("EXECUTION_ROLE_ARN") or EXECUTION_ROLE_ARN
    if not execution_role_arn:
        sts = boto3.client("sts", region_name=REGION)
        account_id = sts.get_caller_identity()["Account"]
        execution_role_arn = f"arn:aws:iam::{account_id}:role/{EXECUTION_ROLE_NAME}"
    return execution_role_arn


def wait_for_load_balancer_dns(elbv2, timeout_seconds: int = 900, poll_seconds: int = 15) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME])
        load_balancers = response.get("LoadBalancers", [])
        if load_balancers:
            dns_name = load_balancers[0].get("DNSName")
            if dns_name:
                return dns_name
        log(f"Aguardando ALB ficar disponivel ({poll_seconds}s)")
        time.sleep(poll_seconds)

    raise TimeoutError("ALB nao ficou disponivel dentro do prazo")


def wait_for_dns_resolution(hostname: str, timeout_seconds: int = 300, poll_seconds: int = 10) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            socket.gethostbyname(hostname)
            return
        except OSError as error:
            last_error = error
            log(f"Aguardando DNS resolver {hostname}: {error}")
            time.sleep(poll_seconds)

    raise TimeoutError(f"DNS nao resolveu a tempo para {hostname}: {last_error}")


def wait_for_healthy_targets(elbv2, timeout_seconds: int = 900, poll_seconds: int = 10) -> None:
    target_groups = elbv2.describe_target_groups(Names=["tg-routing-worker"]).get("TargetGroups", [])
    if not target_groups:
        raise TimeoutError("Target group tg-routing-worker nao encontrado")

    tg_arn = target_groups[0]["TargetGroupArn"]
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        response = elbv2.describe_target_health(TargetGroupArn=tg_arn)
        descriptions = response.get("TargetHealthDescriptions", [])
        states = [item.get("TargetHealth", {}).get("State") for item in descriptions]
        if any(state == "healthy" for state in states):
            log("Pelo menos um target ficou healthy")
            return

        if states:
            log(f"Aguardando targets healthy. Estados atuais: {states}")
        else:
            log("Aguardando registro de targets no target group")

        time.sleep(poll_seconds)

    raise TimeoutError("Nenhum target ficou healthy dentro do prazo")


def wait_for_http_ok(url: str, timeout_seconds: int = 900, poll_seconds: int = 10) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            with urlopen(url, timeout=15) as response:
                if 200 <= response.status < 300:
                    log(f"Endpoint respondeu com HTTP {response.status}")
                    return
        except (HTTPError, URLError, socket.timeout, TimeoutError, OSError) as error:
            last_error = error
            log(f"Aguardando endpoint responder: {error}")
            time.sleep(poll_seconds)

    raise TimeoutError(f"Endpoint nao respondeu a tempo: {last_error}")


def test_service(alb_dns: str) -> None:
    base_url = f"http://{alb_dns}"
    health_url = f"{base_url}/health"
    hello_url = f"{base_url}/hello?name=world"

    log(f"Aguardando resolucao DNS de {alb_dns}")
    wait_for_dns_resolution(alb_dns)

    log("Aguardando target healthy no ALB")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    wait_for_healthy_targets(elbv2)

    log(f"Testando {health_url}")
    wait_for_http_ok(health_url)

    log(f"Testando {hello_url}")
    with urlopen(hello_url, timeout=15) as response:
        body = response.read().decode("utf-8")
        log(f"Resposta /hello: {body}")

    log(f"Testando {base_url}/cpu-burn")
    with urlopen(f"{base_url}/cpu-burn?seconds=5&payload_kb=32", timeout=30) as response:
        body = response.read().decode("utf-8")
        log(f"Resposta /cpu-burn: {body}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Cria, testa e opcionalmente apaga a infraestrutura")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--no-delete",
        action="store_true",
        help="Nao executa o teardown no final; deixa o servico rodando para inspeção.",
    )
    mode.add_argument(
        "--only-delete",
        action="store_true",
        help="Executa apenas o teardown e encerra sem criar ou testar.",
    )
    args = parser.parse_args(argv)

    if args.only_delete:
        log("Executando somente teardown")
        delete.main([])
        log("Teardown concluido")
        return

    execution_role_arn = require_execution_role()

    os.environ["EXECUTION_ROLE_ARN"] = execution_role_arn
    os.environ.setdefault("PYTHONPATH", ".")

    log("Iniciando create/deploy")
    run_deployment()

    log("Esperando ALB e endpoint subirem")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    alb_dns = wait_for_load_balancer_dns(elbv2)
    log(f"ALB DNS: {alb_dns}")

    log("Executando testes")
    test_service(alb_dns)

    if args.no_delete:
        log("Teardown ignorado por --no-delete")
        log("Ciclo create/teste concluido")
        return

    log("Iniciando teardown")
    delete.main([])

    log("Ciclo create/teste/apagar finalizado")

if __name__ == "__main__":
    main()