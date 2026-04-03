import argparse
import os
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

import local.create as create
import local.delete as delete
from local.constants import (
    BUCKET_NAME,
    CLUSTER_NAME,
    LOAD_BALANCER_NAME,
    REGION,
    SERVICE_NAME,
    TABLE_NAME,
)

EXECUTION_ROLE_NAME = os.getenv("EXECUTION_ROLE_NAME", "LabRole")


def log(message: str) -> None:
    print(f"[cycle] {message}")


def run_deployment(execution_role_arn: str) -> None:
    create.setup_worker_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=SERVICE_NAME,
        table_name=TABLE_NAME,
        bucket_name=BUCKET_NAME,
        execution_role_arn=execution_role_arn,
    )


def require_execution_role() -> str:
    execution_role_arn = os.getenv("EXECUTION_ROLE_ARN")
    if not execution_role_arn:
        sts = boto3.client("sts", region_name=REGION)
        account_id = sts.get_caller_identity()["Account"]
        execution_role_arn = f"arn:aws:iam::{account_id}:role/{EXECUTION_ROLE_NAME}"
    return execution_role_arn


def wait_until(check, timeout_seconds: int, poll_seconds: int, timeout_message: str, retry_message=None):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            result = check()
            if result:
                return result
        except (HTTPError, URLError, socket.timeout, TimeoutError, OSError) as error:
            last_error = error
            if retry_message:
                log(f"{retry_message}: {error}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"{timeout_message}: {last_error}")


def wait_for_load_balancer_dns(elbv2):
    return wait_until(
        lambda: next(
            (
                lb.get("DNSName")
                for lb in elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME]).get("LoadBalancers", [])
                if lb.get("DNSName")
            ),
            None,
        ),
        timeout_seconds=900,
        poll_seconds=15,
        timeout_message="ALB nao ficou disponivel dentro do prazo",
        retry_message="Aguardando ALB ficar disponivel",
    )


def wait_for_dns_resolution(hostname: str):
    return wait_until(
        lambda: socket.gethostbyname(hostname),
        timeout_seconds=300,
        poll_seconds=10,
        timeout_message=f"DNS nao resolveu a tempo para {hostname}",
        retry_message=f"Aguardando DNS resolver {hostname}",
    )


def wait_for_healthy_targets(elbv2):
    target_groups = elbv2.describe_target_groups(Names=["tg-routing-worker"]).get("TargetGroups", [])
    if not target_groups:
        raise TimeoutError("Target group tg-routing-worker nao encontrado")
    tg_arn = target_groups[0]["TargetGroupArn"]

    def has_healthy_target():
        states = [item.get("TargetHealth", {}).get("State") for item in elbv2.describe_target_health(TargetGroupArn=tg_arn).get("TargetHealthDescriptions", [])]
        if any(state == "healthy" for state in states):
            return True
        if states:
            log(f"Aguardando targets healthy. Estados atuais: {states}")
        else:
            log("Aguardando registro de targets no target group")
        return False

    wait_until(
        has_healthy_target,
        timeout_seconds=900,
        poll_seconds=10,
        timeout_message="Nenhum target ficou healthy dentro do prazo",
    )


def wait_for_http_ok(url: str):
    return wait_until(
        lambda: urlopen(url, timeout=15).status < 400,
        timeout_seconds=900,
        poll_seconds=10,
        timeout_message=f"Endpoint nao respondeu a tempo: {url}",
        retry_message=f"Aguardando endpoint responder",
    )


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
    mode.add_argument("--no-delete", action="store_true", help="Nao executa o teardown no final.")
    mode.add_argument("--only-delete", action="store_true", help="Executa apenas o teardown.")
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
    run_deployment(execution_role_arn)

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