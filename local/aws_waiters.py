from collections import Counter
import json
import os
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def wait_until(
    check: Callable[[], object],
    timeout_seconds: int,
    poll_seconds: int,
    timeout_message: str,
    retry_message: str | None = None,
    log: Callable[[str], None] | None = None,
):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            result = check()
            if result:
                return result
        except (HTTPError, URLError, socket.timeout, TimeoutError, OSError) as error:
            last_error = error
            if retry_message and log:
                log(retry_message)
        time.sleep(poll_seconds)
    raise TimeoutError(f"{timeout_message}: {last_error}")


def http_get_json(url: str, timeout: int = 15) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_load_balancer_dns(elbv2, load_balancer_name: str, log: Callable[[str], None] | None = None):
    return wait_until(
        lambda: next(
            (
                lb.get("DNSName")
                for lb in elbv2.describe_load_balancers(Names=[load_balancer_name]).get("LoadBalancers", [])
                if lb.get("DNSName")
            ),
            None,
        ),
        timeout_seconds=900,
        poll_seconds=15,
        timeout_message="ALB nao ficou disponivel dentro do prazo",
        retry_message="Aguardando ALB",
        log=log,
    )


def wait_for_dns_resolution(hostname: str, log: Callable[[str], None] | None = None):
    return wait_until(
        lambda: socket.gethostbyname(hostname),
        timeout_seconds=300,
        poll_seconds=10,
        timeout_message=f"DNS nao resolveu a tempo para {hostname}",
        retry_message="Aguardando DNS",
        log=log,
    )


def wait_for_healthy_targets(elbv2, target_group_name: str, log: Callable[[str], None] | None = None):
    timeout_seconds = max(60, int(os.getenv("TARGET_HEALTH_TIMEOUT_SECONDS", "600")))
    poll_seconds = max(3, int(os.getenv("TARGET_HEALTH_POLL_SECONDS", "8")))
    minimum_healthy = max(1, int(os.getenv("TARGET_HEALTH_MIN_COUNT", "1")))

    target_groups = elbv2.describe_target_groups(Names=[target_group_name]).get("TargetGroups", [])
    if not target_groups:
        raise TimeoutError(f"Target group {target_group_name} nao encontrado")
    tg_arn = target_groups[0]["TargetGroupArn"]

    def has_healthy_target():
        states = [
            item.get("TargetHealth", {}).get("State")
            for item in elbv2.describe_target_health(TargetGroupArn=tg_arn).get("TargetHealthDescriptions", [])
        ]
        healthy_count = sum(1 for state in states if state == "healthy")
        if healthy_count >= minimum_healthy:
            return True
        if log:
            counts = Counter(state or "unknown" for state in states)
            summary = ", ".join(f"{state}:{count}" for state, count in sorted(counts.items())) or "sem-targets"
            log(
                "Aguardando targets healthy "
                f"({healthy_count}/{minimum_healthy}) estados=[{summary}]"
            )
        return False

    wait_until(
        has_healthy_target,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        timeout_message=(
            "Nao houve targets healthy suficientes dentro do prazo "
            f"(minimo={minimum_healthy})"
        ),
        log=log,
    )


def wait_for_http_ok(url: str, log: Callable[[str], None] | None = None):
    return wait_until(
        lambda: urlopen(url, timeout=15).status < 400,
        timeout_seconds=900,
        poll_seconds=10,
        timeout_message=f"Endpoint nao respondeu a tempo: {url}",
        retry_message="Aguardando endpoint",
        log=log,
    )
