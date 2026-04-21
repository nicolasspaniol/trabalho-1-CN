"""Entrypoint unificado para o ciclo de deploy local."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import csv
from datetime import datetime
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
from botocore.exceptions import ClientError

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
    print(f"[cycle] {message}", flush=True)


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


def env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"", "0", "false", "no", "off"}


def read_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, value)


def read_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, value)


SIM_DURATION_S = int(os.getenv("SIM_DURATION_S", "300"))
SIM_REPORT_INTERVAL_S = int(os.getenv("SIM_REPORT_INTERVAL_S", "10"))
GRAPH_FILE_DEFAULT = os.getenv("GRAPH_FILE", "").strip() or os.getenv("MAPAS_FILE", "").strip() or "sp_cidade.pkl"
GRAPH_LOCATION_DEFAULT = os.getenv("GRAPH_LOCATION", "").strip() or "Sao Paulo, Sao Paulo, Brazil"
SIM_RESULTS_DIR = Path(os.getenv("SIM_RESULTS_DIR", str(PROJECT_ROOT / "simulation_results")))

SERVICE_SCALING_ENV_DEFAULTS: dict[str, dict[str, str | int]] = {
    SERVICE_NAME: {
        "min_env": "WORKER_AUTOSCALING_MIN_CAPACITY",
        "min_default": 2,
        "desired_env": "WORKER_DESIRED_COUNT",
        "desired_default": 2,
    },
    API_SERVICE_NAME: {
        "min_env": "API_AUTOSCALING_MIN_CAPACITY",
        "min_default": 2,
        "desired_env": "API_DESIRED_COUNT",
        "desired_default": 2,
    },
    LOCATION_SERVICE_NAME: {
        "min_env": "LOCATION_AUTOSCALING_MIN_CAPACITY",
        "min_default": 1,
        "desired_env": "LOCATION_DESIRED_COUNT",
        "desired_default": 1,
    },
}

SERVICE_ALB_TARGETS: dict[str, tuple[str, str]] = {
    SERVICE_NAME: (LOAD_BALANCER_NAME, TARGET_GROUP_NAME),
    API_SERVICE_NAME: (API_LOAD_BALANCER_NAME, API_TARGET_GROUP_NAME),
    LOCATION_SERVICE_NAME: (LOCATION_LOAD_BALANCER_NAME, LOCATION_TARGET_GROUP_NAME),
}

SERVICE_AUTOSCALING_POLICY_DEFAULTS: dict[str, dict[str, str | int | float]] = {
    SERVICE_NAME: {
        "max_env": "WORKER_AUTOSCALING_MAX_CAPACITY",
        "max_default": 18,
        "request_target_env": "WORKER_REQUEST_TARGET",
        "request_target_default": 180.0,
        "scale_out_cooldown_env": "WORKER_SCALE_OUT_COOLDOWN",
        "scale_out_cooldown_default": 12,
        "scale_in_cooldown_env": "WORKER_SCALE_IN_COOLDOWN",
        "scale_in_cooldown_default": 180,
        "request_scale_out_step_env": "WORKER_REQUEST_SCALE_OUT_STEP",
        "request_scale_out_step_default": 2,
        "request_scale_in_step_env": "WORKER_REQUEST_SCALE_IN_STEP",
        "request_scale_in_step_default": 1,
        "request_alarm_period_env": "WORKER_REQUEST_ALARM_PERIOD_SECONDS",
        "request_alarm_period_default": 15,
        "request_alarm_evals_env": "WORKER_REQUEST_ALARM_EVALUATION_PERIODS",
        "request_alarm_evals_default": 1,
        "request_scale_out_multiplier_env": "WORKER_REQUEST_SCALE_OUT_MULTIPLIER",
        "request_scale_out_multiplier_default": 1.02,
        "request_scale_in_multiplier_env": "WORKER_REQUEST_SCALE_IN_MULTIPLIER",
        "request_scale_in_multiplier_default": 0.75,
    },
    API_SERVICE_NAME: {
        "max_env": "API_AUTOSCALING_MAX_CAPACITY",
        "max_default": 14,
        "request_target_env": "API_REQUEST_TARGET",
        "request_target_default": 220.0,
        "scale_out_cooldown_env": "API_SCALE_OUT_COOLDOWN",
        "scale_out_cooldown_default": 12,
        "scale_in_cooldown_env": "API_SCALE_IN_COOLDOWN",
        "scale_in_cooldown_default": 180,
        "request_scale_out_step_env": "API_REQUEST_SCALE_OUT_STEP",
        "request_scale_out_step_default": 2,
        "request_scale_in_step_env": "API_REQUEST_SCALE_IN_STEP",
        "request_scale_in_step_default": 1,
        "request_alarm_period_env": "API_REQUEST_ALARM_PERIOD_SECONDS",
        "request_alarm_period_default": 15,
        "request_alarm_evals_env": "API_REQUEST_ALARM_EVALUATION_PERIODS",
        "request_alarm_evals_default": 1,
        "request_scale_out_multiplier_env": "API_REQUEST_SCALE_OUT_MULTIPLIER",
        "request_scale_out_multiplier_default": 1.02,
        "request_scale_in_multiplier_env": "API_REQUEST_SCALE_IN_MULTIPLIER",
        "request_scale_in_multiplier_default": 0.75,
    },
    LOCATION_SERVICE_NAME: {
        "max_env": "LOCATION_AUTOSCALING_MAX_CAPACITY",
        "max_default": 10,
        "request_target_env": "LOCATION_REQUEST_TARGET",
        "request_target_default": 180.0,
        "scale_out_cooldown_env": "LOCATION_SCALE_OUT_COOLDOWN",
        "scale_out_cooldown_default": 12,
        "scale_in_cooldown_env": "LOCATION_SCALE_IN_COOLDOWN",
        "scale_in_cooldown_default": 180,
        "request_scale_out_step_env": "LOCATION_REQUEST_SCALE_OUT_STEP",
        "request_scale_out_step_default": 2,
        "request_scale_in_step_env": "LOCATION_REQUEST_SCALE_IN_STEP",
        "request_scale_in_step_default": 1,
        "request_alarm_period_env": "LOCATION_REQUEST_ALARM_PERIOD_SECONDS",
        "request_alarm_period_default": 15,
        "request_alarm_evals_env": "LOCATION_REQUEST_ALARM_EVALUATION_PERIODS",
        "request_alarm_evals_default": 1,
        "request_scale_out_multiplier_env": "LOCATION_REQUEST_SCALE_OUT_MULTIPLIER",
        "request_scale_out_multiplier_default": 1.02,
        "request_scale_in_multiplier_env": "LOCATION_REQUEST_SCALE_IN_MULTIPLIER",
        "request_scale_in_multiplier_default": 0.75,
    },
}


def resolve_configured_min_capacity(service_name: str) -> int:
    defaults = SERVICE_SCALING_ENV_DEFAULTS.get(service_name)
    if not defaults:
        return 1
    return read_int_env(str(defaults["min_env"]), int(defaults["min_default"]), minimum=1)


def resolve_configured_desired_count(service_name: str) -> int:
    defaults = SERVICE_SCALING_ENV_DEFAULTS.get(service_name)
    if not defaults:
        return 1

    min_capacity = resolve_configured_min_capacity(service_name)
    desired = read_int_env(str(defaults["desired_env"]), int(defaults["desired_default"]), minimum=1)
    return max(min_capacity, desired)


def resolve_autoscaling_policy_for_service(service_name: str) -> dict[str, int | float]:
    defaults = SERVICE_AUTOSCALING_POLICY_DEFAULTS.get(service_name)
    if not defaults:
        raise ValueError(f"Servico sem defaults de autoscaling: {service_name}")

    min_capacity = resolve_configured_min_capacity(service_name)
    max_capacity = read_int_env(
        str(defaults["max_env"]),
        int(defaults["max_default"]),
        minimum=min_capacity,
    )

    return {
        "min_capacity": min_capacity,
        "max_capacity": max_capacity,
        "request_target": read_float_env(
            str(defaults["request_target_env"]),
            float(defaults["request_target_default"]),
            minimum=1.0,
        ),
        "scale_out_cooldown": read_int_env(
            str(defaults["scale_out_cooldown_env"]),
            int(defaults["scale_out_cooldown_default"]),
            minimum=5,
        ),
        "scale_in_cooldown": read_int_env(
            str(defaults["scale_in_cooldown_env"]),
            int(defaults["scale_in_cooldown_default"]),
            minimum=30,
        ),
        "request_scale_out_step": read_int_env(
            str(defaults["request_scale_out_step_env"]),
            int(defaults["request_scale_out_step_default"]),
            minimum=1,
        ),
        "request_scale_in_step": read_int_env(
            str(defaults["request_scale_in_step_env"]),
            int(defaults["request_scale_in_step_default"]),
            minimum=1,
        ),
        "request_alarm_period_seconds": read_int_env(
            str(defaults["request_alarm_period_env"]),
            int(defaults["request_alarm_period_default"]),
            minimum=15,
        ),
        "request_alarm_evaluation_periods": read_int_env(
            str(defaults["request_alarm_evals_env"]),
            int(defaults["request_alarm_evals_default"]),
            minimum=1,
        ),
        "request_scale_out_multiplier": read_float_env(
            str(defaults["request_scale_out_multiplier_env"]),
            float(defaults["request_scale_out_multiplier_default"]),
            minimum=1.01,
        ),
        "request_scale_in_multiplier": read_float_env(
            str(defaults["request_scale_in_multiplier_env"]),
            float(defaults["request_scale_in_multiplier_default"]),
            minimum=0.1,
        ),
    }


def reapply_autoscaling_policies_for_simulation(service_names: list[str]) -> None:
    requested = [name for name in service_names if name in SERVICE_ALB_TARGETS]
    if not requested:
        return

    autoscaling = boto3.client("application-autoscaling", region_name=REGION)
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    elbv2 = boto3.client("elbv2", region_name=REGION)

    for service_name in requested:
        load_balancer_name, target_group_name = SERVICE_ALB_TARGETS[service_name]
        lb_arn = elbv2.describe_load_balancers(Names=[load_balancer_name])["LoadBalancers"][0]["LoadBalancerArn"]
        tg_arn = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]
        resource_label = create_infra.build_alb_resource_label(lb_arn, tg_arn)
        policy = resolve_autoscaling_policy_for_service(service_name)

        create_infra.ensure_service_autoscaling(
            autoscaling=autoscaling,
            cluster_name=CLUSTER_NAME,
            service_name=service_name,
            resource_label=resource_label,
            cloudwatch=cloudwatch,
            min_capacity=int(policy["min_capacity"]),
            max_capacity=int(policy["max_capacity"]),
            request_target=float(policy["request_target"]),
            cpu_target=70.0,
            memory_target=75.0,
            scale_out_cooldown=int(policy["scale_out_cooldown"]),
            scale_in_cooldown=int(policy["scale_in_cooldown"]),
            request_scale_out_step=int(policy["request_scale_out_step"]),
            request_scale_in_step=int(policy["request_scale_in_step"]),
            request_alarm_period_seconds=int(policy["request_alarm_period_seconds"]),
            request_alarm_evaluation_periods=int(policy["request_alarm_evaluation_periods"]),
            request_scale_out_multiplier=float(policy["request_scale_out_multiplier"]),
            request_scale_in_multiplier=float(policy["request_scale_in_multiplier"]),
        )

        high_threshold = float(policy["request_target"]) * float(policy["request_scale_out_multiplier"])
        low_threshold = float(policy["request_target"]) * float(policy["request_scale_in_multiplier"])
        log(
            f"Autoscaling atualizado ({service_name}): min={policy['min_capacity']} max={policy['max_capacity']} "
            f"req_target={policy['request_target']:.1f} high={high_threshold:.1f} low={low_threshold:.1f} "
            f"cooldown_out={policy['scale_out_cooldown']} cooldown_in={policy['scale_in_cooldown']}"
        )


def ensure_results_csv(
    *,
    csv_path: Path,
    services: list[str],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists():
        return

    base_columns = [
        "timestamp_iso",
        "phase",
        "elapsed_s",
        "warmup_seconds",
        "target_rps",
        "write_offered",
        "write_attempts",
        "write_success",
        "write_dropped_backpressure",
        "read_attempts",
        "read_success",
        "in_flight_writes",
        "in_flight_reads",
        "effective_write_rps",
        "effective_read_rps",
        "window_seconds",
        "write_attempts_window",
        "write_success_window",
        "read_attempts_window",
        "read_success_window",
        "effective_write_rps_window",
        "effective_read_rps_window",
        "p95_write_ms",
        "p95_read_ms",
        "p95_write_ms_window",
        "p95_read_ms_window",
        "write_offered_steady",
        "write_attempts_steady",
        "write_success_steady",
        "write_dropped_backpressure_steady",
        "read_attempts_steady",
        "read_success_steady",
        "effective_write_rps_steady",
        "effective_read_rps_steady",
        "p95_write_ms_steady",
        "p95_read_ms_steady",
        "top_write_failures",
    ]

    service_columns: list[str] = []
    for service_name in services:
        normalized = service_name.lower().replace("-", "_").replace(" ", "_")
        service_columns.extend(
            [
                f"{normalized}_running",
                f"{normalized}_desired",
                f"{normalized}_pending",
            ]
        )

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(base_columns + service_columns)


def append_results_csv_row(
    *,
    csv_path: Path,
    services: list[str],
    metrics: dict[str, Any],
    service_counts: dict[str, dict[str, int]],
) -> None:
    base_values = [
        datetime.now().isoformat(timespec="seconds"),
        metrics.get("phase"),
        metrics.get("elapsed_s"),
        metrics.get("warmup_seconds"),
        metrics.get("target_rps"),
        metrics.get("write_offered"),
        metrics.get("write_attempts"),
        metrics.get("write_success"),
        metrics.get("write_dropped_backpressure"),
        metrics.get("read_attempts"),
        metrics.get("read_success"),
        metrics.get("in_flight_writes"),
        metrics.get("in_flight_reads"),
        metrics.get("effective_write_rps"),
        metrics.get("effective_read_rps"),
        metrics.get("window_seconds"),
        metrics.get("write_attempts_window"),
        metrics.get("write_success_window"),
        metrics.get("read_attempts_window"),
        metrics.get("read_success_window"),
        metrics.get("effective_write_rps_window"),
        metrics.get("effective_read_rps_window"),
        metrics.get("p95_write_ms"),
        metrics.get("p95_read_ms"),
        metrics.get("p95_write_ms_window"),
        metrics.get("p95_read_ms_window"),
        metrics.get("write_offered_steady"),
        metrics.get("write_attempts_steady"),
        metrics.get("write_success_steady"),
        metrics.get("write_dropped_backpressure_steady"),
        metrics.get("read_attempts_steady"),
        metrics.get("read_success_steady"),
        metrics.get("effective_write_rps_steady"),
        metrics.get("effective_read_rps_steady"),
        metrics.get("p95_write_ms_steady"),
        metrics.get("p95_read_ms_steady"),
        json.dumps(metrics.get("top_write_failures", []), ensure_ascii=True),
    ]

    service_values: list[int] = []
    for service_name in services:
        counts = service_counts.get(service_name, {})
        service_values.extend(
            [
                int(counts.get("running", 0)),
                int(counts.get("desired", 0)),
                int(counts.get("pending", 0)),
            ]
        )

    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(base_values + service_values)


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
    csv_path: Path,
) -> None:
    ensure_results_csv(csv_path=csv_path, services=services)
    warmup_log_interval_s = max(SIM_REPORT_INTERVAL_S, int(os.getenv("SIM_WARMUP_LOG_INTERVAL_S", "30")))
    last_warmup_log_ts = 0.0
    last_warmup_svc_summary = ""

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
        effective_write_rps_window = metrics.get("effective_write_rps_window")
        window_seconds = metrics.get("window_seconds")
        p95_write_ms = metrics.get("p95_write_ms")
        p95_write_ms_window = metrics.get("p95_write_ms_window")
        p95_read_ms = metrics.get("p95_read_ms")

        append_results_csv_row(
            csv_path=csv_path,
            services=services,
            metrics=metrics,
            service_counts=svc_counts,
        )

        # Antes do sim_client iniciar, evita spam com NA/NA em todas as colunas.
        if not isinstance(elapsed_s, (int, float)):
            now_ts = time.time()
            should_log_warmup = (
                svc_summary != last_warmup_svc_summary
                or (now_ts - last_warmup_log_ts) >= warmup_log_interval_s
            )
            if should_log_warmup:
                log(f"Simulacao (aquecendo): tasks={svc_summary or 'NA'}")
                last_warmup_log_ts = now_ts
                last_warmup_svc_summary = svc_summary
            continue

        log(
            f"Simulacao (a cada {SIM_REPORT_INTERVAL_S}s, acumulado): "
            f"tasks={svc_summary or 'NA'}; "
            f"pedidos_ok={write_success if write_success is not None else 'NA'}/{write_attempts if write_attempts is not None else 'NA'}; "
            f"elapsed_s={int(elapsed_s) if isinstance(elapsed_s, (int, float)) else 'NA'}; "
            f"rps_target={target_rps if target_rps is not None else 'NA'} "
            f"rps_eff={f'{effective_write_rps:.2f}' if isinstance(effective_write_rps, (int, float)) else 'NA'}; "
            f"rps_{int(window_seconds) if isinstance(window_seconds, (int, float)) else 'NA'}s={f'{effective_write_rps_window:.2f}' if isinstance(effective_write_rps_window, (int, float)) else 'NA'}; "
            f"p95_write_ms={f'{p95_write_ms:.2f}' if isinstance(p95_write_ms, (int, float)) else 'NA'} "
            f"p95_write_{int(window_seconds) if isinstance(window_seconds, (int, float)) else 'NA'}s_ms={f'{p95_write_ms_window:.2f}' if isinstance(p95_write_ms_window, (int, float)) else 'NA'} "
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
    autoscaling_restore_state: dict[str, dict[str, bool]] | None = None,
) -> None:
    delivery_process = None
    autoscaling_restored = False
    autoscaling_restore_lock = threading.Lock()
    minimum_sim_duration_seconds = read_int_env("SIM_MIN_DURATION_SECONDS", 300, minimum=60)
    effective_sim_duration_s = max(int(sim_duration_s), minimum_sim_duration_seconds)
    if effective_sim_duration_s != int(sim_duration_s):
        log(
            "Simulacao: duracao ajustada para garantir janela de autoscaling "
            f"({sim_duration_s}s -> {effective_sim_duration_s}s)"
        )

    def compute_warmup_seconds(duration_seconds: int) -> int:
        configured_warmup = max(0, int(os.getenv("SIM_WARMUP_SECONDS", "20")))
        return max(0, min(max(0, int(duration_seconds) - 30), configured_warmup))

    def restore_autoscaling_once(reason: str) -> None:
        nonlocal autoscaling_restored
        if not autoscaling_restore_state:
            return
        with autoscaling_restore_lock:
            if autoscaling_restored:
                return
            log(f"Simulacao: restaurando autoscaling ({reason})")
            restore_autoscaling_suspension_state(autoscaling_restore_state)
            autoscaling_restored = True

    can_load_data = supports_data_load(base_url)
    full_simulation_supported = ensure_simulation_contract(base_url, location_base_url)
    # Perfil mais realista: evita amplificacao excessiva por polling de courier,
    # mantendo pressao suficiente para acionar autoscaling em cenarios de 50/200 RPS.
    target_couriers = max(40, min(220, rps * 3))
    active_delivery_couriers = max(30, min(target_couriers, rps * 2))
    courier_per_user = target_couriers / max(1, num_users)
    auth_env: dict[str, str] = {}
    if api_username and api_password:
        auth_env = {"API_USERNAME": api_username, "API_PASSWORD": api_password}
    try:
        if not can_load_data:
            log("Simulacao: pulando carga/sim_delivery/load-test por falta de endpoints de carga; deploy segue normalmente")
            return

        log("Simulacao: preflight de endpoints (OpenAPI + fluxo critico)")
        preflight_attempts = max(1, int(os.getenv("SIM_PREFLIGHT_ATTEMPTS", "3")))
        skip_preflight_on_failure = env_flag("SIM_SKIP_PREFLIGHT_ON_FAILURE", default=False)
        for attempt in range(1, preflight_attempts + 1):
            try:
                run_python_script(
                    PROJECT_ROOT / "local" / "validate_endpoints.py",
                    ["--api-url", base_url, "--username", api_username or "admin", "--password", api_password or "admin"],
                    env=auth_env,
                )
                break
            except subprocess.CalledProcessError as exc:
                if attempt >= preflight_attempts:
                    if skip_preflight_on_failure:
                        log(
                            "Simulacao: preflight falhou apos todas as tentativas, "
                            "mas SIM_SKIP_PREFLIGHT_ON_FAILURE=1; seguindo com a carga"
                        )
                        break
                    raise exc
                backoff_seconds = min(30, 5 * attempt)
                log(
                    "Simulacao: preflight falhou na tentativa "
                    f"{attempt}/{preflight_attempts}; nova tentativa em {backoff_seconds}s"
                )
                time.sleep(backoff_seconds)

        log("Simulacao: populando base via API")
        run_python_script(
            PROJECT_ROOT / "local" / "load.py",
            ["--api-url", base_url, "--num-users", str(num_users)],
            env={
                **auth_env,
                "SIM_GRAPH_PATH": graph_file,
                "SIM_COURIER_PER_USER": f"{courier_per_user:.6f}",
                "SIM_COURIER_CAP": str(target_couriers),
                "SIM_MERCHANT_PER_USER": "0.1",
            },
        )

        if not full_simulation_supported:
            log("Simulacao: fase de pedidos/entregas ignorada por incompatibilidade de contrato")
            return

        log("Simulacao: iniciando sim_delivery em background")
        delivery_env = {**os.environ, "PYTHONUNBUFFERED": "1", **auth_env}
        # Reduz polling agressivo para evitar pressao excessiva no path de autenticacao/DB.
        delivery_env.setdefault("SIM_COURIER_MAX", str(active_delivery_couriers))
        delivery_env.setdefault("SIM_COURIER_POLL_SECONDS", "2.0")
        delivery_env.setdefault("SIM_COURIER_POLL_JITTER_SECONDS", "1.0")
        delivery_env.setdefault("SIM_API_PAGE_SIZE", "100")
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
            env=delivery_env,
        )

        time.sleep(3)

        timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file_name = f"sim_rps{rps}_users{num_users}_dur{effective_sim_duration_s}_{timestamp_suffix}.csv"
        csv_path = SIM_RESULTS_DIR / csv_file_name

        log(f"Simulacao: resultados serao salvos em {csv_path}")
        log(f"Simulacao: load test {rps} RPS por {effective_sim_duration_s}s")
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
                "csv_path": csv_path,
            },
            daemon=True,
        )
        reporter.start()
        restore_thread = None
        try:
            if autoscaling_restore_state:
                warmup_seconds = compute_warmup_seconds(effective_sim_duration_s)
                restore_delay_seconds = warmup_seconds + max(
                    0,
                    int(os.getenv("SIM_AUTOSCALING_RESTORE_DELAY_SECONDS", "0")),
                )
                if restore_delay_seconds <= 0:
                    restore_autoscaling_once("sem warmup configurado")
                else:
                    log(
                        "Simulacao: autoscaling seguira suspenso durante o warmup "
                        f"({restore_delay_seconds}s)"
                    )

                    def delayed_restore() -> None:
                        try:
                            time.sleep(restore_delay_seconds)
                            restore_autoscaling_once("apos warmup")
                        except Exception as error:
                            log(f"Simulacao: falha ao restaurar autoscaling apos warmup: {error}")

                    restore_thread = threading.Thread(target=delayed_restore, daemon=True)
                    restore_thread.start()

            max_in_flight_writes = min(1200, max(220, rps * 6))
            max_in_flight_reads = min(600, max(100, rps * 3))
            http_conn_limit = min(1600, max(220, rps * 8))
            http_conn_limit_per_host = min(1200, max(140, rps * 6))
            sim_warmup_seconds = compute_warmup_seconds(effective_sim_duration_s)

            run_python_script(
                PROJECT_ROOT / "local" / "sim_client.py",
                ["--api-url", base_url, "--rps", str(rps), "--duration", str(effective_sim_duration_s)],
                env={
                    **auth_env,
                    "SIM_METRICS_PATH": metrics_path,
                    "SIM_REPORT_INTERVAL_S": str(SIM_REPORT_INTERVAL_S),
                    "SIM_WARMUP_SECONDS": str(sim_warmup_seconds),
                    "SIM_MAX_IN_FLIGHT_WRITES": str(max_in_flight_writes),
                    "SIM_MAX_IN_FLIGHT_READS": str(max_in_flight_reads),
                    "SIM_HTTP_CONN_LIMIT": str(http_conn_limit),
                    "SIM_HTTP_CONN_LIMIT_PER_HOST": str(http_conn_limit_per_host),
                    "SIM_API_PAGE_SIZE": "100",
                },
                timeout_seconds=effective_sim_duration_s + 300,
            )
        finally:
            stop_event.set()
            reporter.join(timeout=2)
            if restore_thread is not None:
                restore_thread.join(timeout=1)
            restore_autoscaling_once("encerramento da simulacao")
    finally:
        if delivery_process is not None:
            log("Simulacao: encerrando sim_delivery")
            delivery_process.terminate()
            delivery_process.wait()
        restore_autoscaling_once("finalizacao da run_simulation")


def resolve_simulation_base_url(worker_base_url: str, override_api_url: str | None) -> str:
    if override_api_url and override_api_url.strip():
        chosen = override_api_url.strip().rstrip("/")
        log(f"Simulacao: usando API dedicada em {chosen}")
        return chosen
    return worker_base_url


def s3_object_exists(bucket_name: str, object_key: str) -> bool:
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.head_object(Bucket=bucket_name, Key=object_key)
        return True
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def ensure_graph_in_s3(bucket_name: str, graph_file: str, graph_location: str) -> None:
    if not env_flag("FORCE_GRAPH_REBUILD", default=False) and s3_object_exists(bucket_name, graph_file):
        log(
            "Dados: grafo ja existe no S3; pulando geracao/upload "
            "(use FORCE_GRAPH_REBUILD=1 para forcar)"
        )
        return

    log("Dados: gerando grafo e enviando para S3")
    run_python_script(
        PROJECT_ROOT / "local" / "create.py",
        ["--bucket", bucket_name, "--file", graph_file, "--location", graph_location],
    )


def run_deployment(execution_role_arn: str, bucket_name: str) -> None:
    serial_rollout = env_flag("DEPLOY_SERIAL_ROLLOUT", default=False)

    log("Deploy ECS: garantindo service-linked role do ECS")
    create_infra.ensure_ecs_service_linked_role(REGION)

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
        if env_flag("DB_SKIP_SCHEMA_LOAD", default=False) or env_flag("SKIP_SCHEMA_LOAD", default=False):
            log(
                "Deploy dados: carga de schema ignorada "
                "(DB_SKIP_SCHEMA_LOAD=1 ou SKIP_SCHEMA_LOAD=1)"
            )
        else:
            log(f"Deploy dados: aplicando schema em {schema_path}")
            loaded = create_data.load_schema_to_rds(db_endpoint, str(schema_path))
            if not loaded:
                raise RuntimeError(
                    "Falha ao carregar schema SQL no RDS. "
                    "Se o banco ja estiver provisionado para testes, use DB_SKIP_SCHEMA_LOAD=1."
                )
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

    if serial_rollout:
        log(f"Deploy serial: aguardando estabilidade ECS de {SERVICE_NAME}")
        wait_for_ecs_service_stable(SERVICE_NAME)

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

    if serial_rollout:
        log(f"Deploy serial: aguardando estabilidade ECS de {LOCATION_SERVICE_NAME}")
        wait_for_ecs_service_stable(LOCATION_SERVICE_NAME)

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

    if serial_rollout:
        log(f"Deploy serial: aguardando estabilidade ECS de {API_SERVICE_NAME}")
        wait_for_ecs_service_stable(API_SERVICE_NAME)


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


def wait_service_ready(alb_dns: str, target_group_name: str, service_name: str | None = None) -> None:
    label = service_name or target_group_name

    if service_name:
        log(f"ECS stable ({service_name})")
        wait_for_ecs_service_stable(service_name)

    log(f"Resolucao DNS ({label})")
    wait_for_dns_resolution(alb_dns, log=log)

    log(f"Health do target ({label})")
    elbv2 = boto3.client("elbv2", region_name=REGION)
    wait_for_healthy_targets(elbv2, target_group_name, log=log)


def wait_services_ready(service_targets: list[tuple[str, str, str]]) -> None:
    if not service_targets:
        return

    if len(service_targets) == 1:
        alb_dns, target_group_name, service_name = service_targets[0]
        wait_service_ready(alb_dns, target_group_name, service_name=service_name)
        return

    timeout_seconds = max(
        120,
        int(
            os.getenv(
                "SERVICES_READINESS_TIMEOUT_SECONDS",
                str(max(300, int(os.getenv("ECS_STABLE_TIMEOUT_SECONDS", "1200")))),
            )
        ),
    )
    heartbeat_seconds = max(10, int(os.getenv("SERVICES_READINESS_HEARTBEAT_SECONDS", "20")))

    errors: list[str] = []
    max_workers = min(4, len(service_targets))
    started_at = time.time()
    last_heartbeat_at = started_at

    # Nao use context-manager aqui: em Ctrl+C, shutdown(wait=True) pode bloquear
    # enquanto threads de readiness ainda estao em polling.
    executor = ThreadPoolExecutor(max_workers=max_workers)
    future_to_service: dict[Any, str] = {}
    try:
        future_to_service = {
            executor.submit(wait_service_ready, alb_dns, target_group_name, service_name): service_name
            for alb_dns, target_group_name, service_name in service_targets
        }
        pending_futures = set(future_to_service.keys())

        while pending_futures:
            elapsed = time.time() - started_at
            if elapsed >= timeout_seconds:
                waiting_services = ", ".join(sorted(future_to_service[item] for item in pending_futures))
                raise TimeoutError(
                    "Readiness global expirou em "
                    f"{timeout_seconds}s; servicos ainda pendentes: {waiting_services}"
                )

            done, pending_futures = wait(
                pending_futures,
                timeout=5,
                return_when=FIRST_COMPLETED,
            )

            if not done:
                now = time.time()
                if now - last_heartbeat_at >= heartbeat_seconds:
                    waiting_services = ", ".join(sorted(future_to_service[item] for item in pending_futures))
                    log(
                        "Readiness aguardando "
                        f"({int(now - started_at)}s): {waiting_services}"
                    )
                    last_heartbeat_at = now
                continue

            for future in done:
                service_name = future_to_service[future]
                try:
                    future.result()
                    log(f"Readiness concluido ({service_name})")
                except Exception as error:
                    errors.append(f"{service_name}: {error}")
    except KeyboardInterrupt:
        remaining = [
            future_to_service[item]
            for item in future_to_service
            if not item.done()
        ]
        remaining_text = ", ".join(sorted(remaining)) if remaining else "nenhum"
        log(f"Readiness interrompido pelo usuario; pendentes: {remaining_text}")
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if errors:
        raise RuntimeError("Falha no readiness dos servicos: " + " | ".join(errors))


def wait_for_ecs_service_stable(service_name: str) -> None:
    timeout_seconds = max(60, int(os.getenv("ECS_STABLE_TIMEOUT_SECONDS", "1200")))
    poll_seconds = max(3, int(os.getenv("ECS_STABLE_POLL_SECONDS", "10")))
    heartbeat_seconds = max(poll_seconds, int(os.getenv("ECS_STABLE_HEARTBEAT_SECONDS", "45")))
    require_full_stability = env_flag("ECS_REQUIRE_FULL_STABILITY", default=False)

    ecs = boto3.client("ecs", region_name=REGION)
    started_at = time.time()
    deadline = time.time() + timeout_seconds
    last_snapshot: tuple[int, int, int, int, str, int, int, int] | None = None
    last_event_message: str | None = None
    last_heartbeat_at = started_at

    while time.time() < deadline:
        response = ecs.describe_services(cluster=CLUSTER_NAME, services=[service_name])
        services = response.get("services", [])
        failures = response.get("failures", [])

        if failures:
            reasons = ", ".join(str(item.get("reason", "unknown")) for item in failures)
            raise RuntimeError(f"Falha ao consultar servico ECS {service_name}: {reasons}")

        if not services:
            raise RuntimeError(f"Servico ECS nao encontrado: {service_name}")

        service = services[0]
        status = str(service.get("status", "UNKNOWN"))
        desired = int(service.get("desiredCount", 0))
        running = int(service.get("runningCount", 0))
        pending = int(service.get("pendingCount", 0))
        deployments = service.get("deployments", [])

        primary = next((item for item in deployments if item.get("status") == "PRIMARY"), None)
        primary_rollout = str((primary or {}).get("rolloutState") or "UNKNOWN")
        primary_running = int((primary or {}).get("runningCount", 0))
        primary_desired = int((primary or {}).get("desiredCount", desired))
        primary_pending = int((primary or {}).get("pendingCount", 0))

        failed_deployments = [
            item
            for item in deployments
            if str(item.get("rolloutState") or "").upper() == "FAILED"
        ]
        if failed_deployments:
            reasons = [
                str(item.get("rolloutStateReason") or "motivo nao informado")
                for item in failed_deployments
            ]
            raise RuntimeError(
                f"Servico ECS {service_name} com rollout FAILED: "
                + " | ".join(reasons)
            )

        in_progress = [item for item in deployments if item.get("rolloutState") != "COMPLETED"]
        snapshot = (
            running,
            desired,
            pending,
            len(deployments),
            primary_rollout,
            primary_running,
            primary_desired,
            primary_pending,
        )

        now = time.time()
        should_log_heartbeat = (now - last_heartbeat_at) >= heartbeat_seconds
        if snapshot != last_snapshot or should_log_heartbeat:
            elapsed = int(now - started_at)
            log(
                f"ECS {service_name}: status={status} running={running}/{desired} "
                f"pending={pending} deployments={len(deployments)} "
                f"primary={primary_rollout} ({primary_running}/{primary_desired}, pend={primary_pending}) "
                f"elapsed={elapsed}s"
            )
            last_snapshot = snapshot
            last_heartbeat_at = now

        latest_event = (service.get("events") or [{}])[0].get("message")
        if latest_event and latest_event != last_event_message:
            log(f"ECS evento ({service_name}): {latest_event}")
            last_event_message = latest_event

        expected_primary_running = max(desired, primary_desired)
        primary_ready_for_traffic = (
            primary is not None
            and primary_running >= expected_primary_running
            and primary_pending == 0
            and primary_rollout in {"IN_PROGRESS", "COMPLETED"}
        )

        ready_for_checks = (
            status == "ACTIVE"
            and running >= desired
            and pending == 0
            and primary_ready_for_traffic
        )

        fully_stable = (
            status == "ACTIVE"
            and running == desired
            and pending == 0
            and not in_progress
            and len(deployments) == 1
            and primary_rollout == "COMPLETED"
        )

        if require_full_stability and fully_stable:
            return

        if not require_full_stability and ready_for_checks:
            return

        time.sleep(poll_seconds)

    raise TimeoutError(
        f"Servico ECS {service_name} nao atingiu readiness em {timeout_seconds}s "
        f"(running={last_snapshot[0] if last_snapshot else 'NA'}/"
        f"{last_snapshot[1] if last_snapshot else 'NA'}, pending={last_snapshot[2] if last_snapshot else 'NA'}). "
        f"Ultimo evento: {last_event_message!r}"
    )


def resolve_min_capacities_for_services(service_names: list[str]) -> dict[str, int]:
    return {
        service_name: resolve_configured_min_capacity(service_name)
        for service_name in service_names
    }


def resolve_desired_counts_for_services(service_names: list[str]) -> dict[str, int]:
    return {
        service_name: resolve_configured_desired_count(service_name)
        for service_name in service_names
    }


def snapshot_autoscaling_suspension_state(service_names: list[str]) -> dict[str, dict[str, bool]]:
    autoscaling = boto3.client("application-autoscaling", region_name=REGION)
    resource_ids = [f"service/{CLUSTER_NAME}/{name}" for name in service_names]
    response = autoscaling.describe_scalable_targets(
        ServiceNamespace="ecs",
        ResourceIds=resource_ids,
        ScalableDimension="ecs:service:DesiredCount",
    )

    state_by_service: dict[str, dict[str, bool]] = {}
    for target in response.get("ScalableTargets", []):
        resource_id = str(target.get("ResourceId", ""))
        parts = resource_id.split("/")
        if len(parts) != 3:
            continue
        service_name = parts[2]
        suspended = target.get("SuspendedState") or {}
        state_by_service[service_name] = {
            "DynamicScalingInSuspended": bool(suspended.get("DynamicScalingInSuspended", False)),
            "DynamicScalingOutSuspended": bool(suspended.get("DynamicScalingOutSuspended", False)),
            "ScheduledScalingSuspended": bool(suspended.get("ScheduledScalingSuspended", False)),
        }
    return state_by_service


def set_autoscaling_suspension_state(service_names: list[str], *, suspended: bool) -> None:
    autoscaling = boto3.client("application-autoscaling", region_name=REGION)
    min_by_service = resolve_min_capacities_for_services(service_names)

    for service_name in service_names:
        min_capacity = int(min_by_service.get(service_name, 1))
        log(
            f"Clean start: autoscaling {'suspenso' if suspended else 'reativado'} para {service_name}"
        )
        autoscaling.register_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=f"service/{CLUSTER_NAME}/{service_name}",
            ScalableDimension="ecs:service:DesiredCount",
            MinCapacity=min_capacity,
            SuspendedState={
                "DynamicScalingInSuspended": suspended,
                "DynamicScalingOutSuspended": suspended,
                "ScheduledScalingSuspended": suspended,
            },
        )


def restore_autoscaling_suspension_state(state_by_service: dict[str, dict[str, bool]]) -> None:
    if not state_by_service:
        return

    autoscaling = boto3.client("application-autoscaling", region_name=REGION)
    min_by_service = resolve_min_capacities_for_services(list(state_by_service.keys()))

    for service_name, state in state_by_service.items():
        min_capacity = int(min_by_service.get(service_name, 1))
        log(f"Clean start: restaurando estado de autoscaling para {service_name}")
        autoscaling.register_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=f"service/{CLUSTER_NAME}/{service_name}",
            ScalableDimension="ecs:service:DesiredCount",
            MinCapacity=min_capacity,
            SuspendedState={
                "DynamicScalingInSuspended": bool(state.get("DynamicScalingInSuspended", False)),
                "DynamicScalingOutSuspended": bool(state.get("DynamicScalingOutSuspended", False)),
                "ScheduledScalingSuspended": bool(state.get("ScheduledScalingSuspended", False)),
            },
        )


def enforce_clean_start_for_simulation(service_names: list[str]) -> dict[str, dict[str, bool]]:
    timeout_seconds = max(120, int(os.getenv("SIM_CLEAN_START_TIMEOUT_SECONDS", "900")))
    poll_seconds = max(3, int(os.getenv("SIM_CLEAN_START_POLL_SECONDS", "10")))

    min_by_service = resolve_min_capacities_for_services(service_names)
    desired_by_service = resolve_desired_counts_for_services(service_names)
    previous_suspension_state = snapshot_autoscaling_suspension_state(service_names)
    ecs = boto3.client("ecs", region_name=REGION)
    autoscaling = boto3.client("application-autoscaling", region_name=REGION)
    set_autoscaling_suspension_state(service_names, suspended=True)

    try:
        for service_name in service_names:
            min_capacity = int(min_by_service.get(service_name, 1))
            desired_count = int(desired_by_service.get(service_name, min_capacity))
            log(
                f"Clean start: ajustando {service_name} para min_capacity={min_capacity} "
                f"e desired={desired_count}"
            )
            autoscaling.register_scalable_target(
                ServiceNamespace="ecs",
                ResourceId=f"service/{CLUSTER_NAME}/{service_name}",
                ScalableDimension="ecs:service:DesiredCount",
                MinCapacity=min_capacity,
            )
            ecs.update_service(
                cluster=CLUSTER_NAME,
                service=service_name,
                desiredCount=desired_count,
            )

        started_at = time.time()
        deadline = started_at + timeout_seconds

        while time.time() < deadline:
            response = ecs.describe_services(cluster=CLUSTER_NAME, services=service_names)
            services = response.get("services", [])
            by_name = {str(item.get("serviceName", "")): item for item in services}

            all_clean = True
            for service_name in service_names:
                service = by_name.get(service_name)
                if not service:
                    all_clean = False
                    log(f"Clean start: {service_name} ainda nao encontrado no describe_services")
                    continue

                desired = int(service.get("desiredCount", 0))
                running = int(service.get("runningCount", 0))
                pending = int(service.get("pendingCount", 0))
                deployments = service.get("deployments", [])
                in_progress = [item for item in deployments if item.get("rolloutState") != "COMPLETED"]

                clean = (
                    running == desired
                    and pending == 0
                    and len(in_progress) == 0
                    and len(deployments) == 1
                )

                if not clean:
                    all_clean = False
                    rollout_states = [str(item.get("rolloutState") or "UNKNOWN") for item in deployments]
                    log(
                        f"Clean start aguardando {service_name}: "
                        f"running={running}/{desired} pending={pending} deployments={len(deployments)} "
                        f"rollout={rollout_states}"
                    )

            if all_clean:
                log("Clean start: servicos estabilizados para inicio da simulacao")
                return previous_suspension_state

            time.sleep(poll_seconds)

        raise TimeoutError(
            "Clean start nao convergiu no prazo; tente aumentar SIM_CLEAN_START_TIMEOUT_SECONDS "
            "ou revisar eventos de deployment no ECS."
        )
    except Exception:
        restore_autoscaling_suspension_state(previous_suspension_state)
        raise


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
    autoscaling_demo.main(["--show-only", "--wait-seconds", str(wait_seconds)])
    debug_alb_request_alarm_states([SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME])


def debug_alb_request_alarm_states(service_names: list[str]) -> None:
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    alarm_names: list[str] = []
    for service_name in service_names:
        alarm_prefix = service_name.replace("/", "-")
        alarm_names.append(f"{alarm_prefix}-AlbRequestHigh")
        alarm_names.append(f"{alarm_prefix}-AlbRequestLow")

    if not alarm_names:
        return

    response = cloudwatch.describe_alarms(AlarmNames=alarm_names)
    alarms = response.get("MetricAlarms", [])
    by_name = {str(alarm.get("AlarmName", "")): alarm for alarm in alarms}

    log("Demo autoscaling: estado dos alarmes AlbRequestHigh/Low")
    for alarm_name in alarm_names:
        alarm = by_name.get(alarm_name)
        if not alarm:
            log(f"  - {alarm_name}: nao encontrado")
            continue

        state = str(alarm.get("StateValue", "UNKNOWN"))
        reason = str(alarm.get("StateReason", "sem motivo"))
        threshold = alarm.get("Threshold")
        period = alarm.get("Period")
        eval_periods = alarm.get("EvaluationPeriods")
        datapoints_to_alarm = alarm.get("DatapointsToAlarm")
        log(
            f"  - {alarm_name}: state={state} threshold={threshold} "
            f"period={period}s evals={eval_periods} dta={datapoints_to_alarm} "
            f"reason={reason}"
        )


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
    service_name: str | None = None,
) -> None:
    base_url = f"http://{alb_dns}"

    if include_readiness:
        wait_service_ready(alb_dns, target_group_name, service_name=service_name)

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
        default=GRAPH_FILE_DEFAULT,
        help="Nome do arquivo local do grafo usado na simulacao",
    )
    parser.add_argument(
        "--graph-location",
        default=GRAPH_LOCATION_DEFAULT,
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
    parser.add_argument(
        "--skip-pre-setup",
        action="store_true",
        help="Pula o pre-setup (build/push de imagens) e reutiliza imagens ja publicadas no ECR.",
    )
    parser.add_argument(
        "--simulation-only",
        action="store_true",
        help="Nao executa deploy; usa ALBs/servicos existentes e roda apenas simulacao.",
    )
    args = parser.parse_args(argv)

    configure_aws_files()

    if args.only_delete:
        log("Teardown")
        delete.main([])
        log("Fim")
        return 0

    if args.simulation_only and args.only_delete:
        raise ValueError("--simulation-only nao pode ser combinado com --only-delete")

    if args.simulation_only and not args.with_simulation:
        raise ValueError("--simulation-only requer --with-simulation")

    if args.simulation_only:
        os.environ["MAPAS_FILE"] = args.graph_file

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

        api_base_url = f"http://{api_alb_dns}"
        location_base_url = f"http://{location_alb_dns}"

        if env_flag("SIM_REAPPLY_AUTOSCALING_POLICIES", default=True):
            log("Autoscaling: reaplicando politicas e alarmes antes da simulacao")
            reapply_autoscaling_policies_for_simulation(
                [SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME]
            )

        log("Clean start: preparando baseline confiavel para simulacao")
        autoscaling_restore_state = enforce_clean_start_for_simulation(
            [SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME]
        )

        simulation_started = False
        try:
            log("Readiness dos servicos")
            wait_services_ready(
                [
                    (worker_alb_dns, TARGET_GROUP_NAME, SERVICE_NAME),
                    (api_alb_dns, API_TARGET_GROUP_NAME, API_SERVICE_NAME),
                    (location_alb_dns, LOCATION_TARGET_GROUP_NAME, LOCATION_SERVICE_NAME),
                ]
            )

            simulation_base_url = resolve_simulation_base_url(api_base_url, args.simulation_api_url)
            if simulation_base_url == api_base_url and not supports_data_load(simulation_base_url):
                raise RuntimeError(
                    "Simulacao indisponivel na URL atual: o endpoint publico da API nao expoe /customers, /merchants e /couriers. "
                    "Use --simulation-api-url apontando para a API FastAPI de dominio."
                )

            api_username, api_password = resolve_api_auth(args.api_username, args.api_password, args.api_auth_env_file)
            simulation_started = True
            run_simulation(
                base_url=simulation_base_url,
                num_users=args.num_users,
                rps=args.rps,
                sim_duration_s=args.sim_duration,
                api_username=api_username,
                api_password=api_password,
                bucket_name="",
                graph_file=args.graph_file,
                graph_location=args.graph_location,
                location_base_url=location_base_url,
                autoscaling_restore_state=autoscaling_restore_state,
            )
        finally:
            if not simulation_started:
                restore_autoscaling_suspension_state(autoscaling_restore_state)

        if args.with_autoscaling_demo:
            run_autoscaling_demo(args.autoscaling_wait_seconds)

        log("Fim")
        return 0

    if args.skip_pre_setup or env_flag("SKIP_PRE_SETUP", default=False):
        log("Pre-setup ignorado (--skip-pre-setup ou SKIP_PRE_SETUP=1)")
    else:
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
        if env_flag("SIM_REAPPLY_AUTOSCALING_POLICIES", default=True):
            log("Autoscaling: reaplicando politicas e alarmes antes da simulacao")
            reapply_autoscaling_policies_for_simulation(
                [SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME]
            )

        log("Clean start: preparando baseline confiavel para simulacao")
        autoscaling_restore_state = enforce_clean_start_for_simulation(
            [SERVICE_NAME, API_SERVICE_NAME, LOCATION_SERVICE_NAME]
        )

        simulation_started = False
        try:
            log("Readiness dos servicos")
            wait_services_ready(
                [
                    (worker_alb_dns, TARGET_GROUP_NAME, SERVICE_NAME),
                    (api_alb_dns, API_TARGET_GROUP_NAME, API_SERVICE_NAME),
                    (location_alb_dns, LOCATION_TARGET_GROUP_NAME, LOCATION_SERVICE_NAME),
                ]
            )

            simulation_base_url = resolve_simulation_base_url(api_base_url, args.simulation_api_url)
            if simulation_base_url == api_base_url and not supports_data_load(simulation_base_url):
                raise RuntimeError(
                    "Simulacao indisponivel na URL atual: o endpoint publico da API nao expoe /customers, /merchants e /couriers. "
                    "Use --simulation-api-url apontando para a API FastAPI de dominio."
                )

            api_username, api_password = resolve_api_auth(args.api_username, args.api_password, args.api_auth_env_file)
            simulation_started = True
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
                autoscaling_restore_state=autoscaling_restore_state,
            )
        except RuntimeError as error:
            log(str(error))
            return 2
        finally:
            if not simulation_started:
                restore_autoscaling_suspension_state(autoscaling_restore_state)

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
        log("Readiness dos servicos")
        wait_services_ready(
            [
                (worker_alb_dns, TARGET_GROUP_NAME, SERVICE_NAME),
                (api_alb_dns, API_TARGET_GROUP_NAME, API_SERVICE_NAME),
                (location_alb_dns, LOCATION_TARGET_GROUP_NAME, LOCATION_SERVICE_NAME),
            ]
        )

        log("Testes")
        test_service(
            worker_alb_dns,
            TARGET_GROUP_NAME,
            include_readiness=False,
            require_graph_loaded=True,
            require_db_connected=True,
            require_tables_ok=True,
        )
        test_service(
            api_alb_dns,
            API_TARGET_GROUP_NAME,
            include_readiness=False,
            require_graph_loaded=False,
            require_db_connected=False,
            require_tables_ok=False,
        )
        test_service(
            location_alb_dns,
            LOCATION_TARGET_GROUP_NAME,
            include_readiness=False,
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
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Execucao interrompida pelo usuario.")
        raise SystemExit(130)
