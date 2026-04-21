#!/usr/bin/env python3
"""Diagnostica policies TargetTracking e alarmes CloudWatch gerados automaticamente."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3


def _parse_services(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _extract_metric_summary(policy: dict[str, Any]) -> str:
    cfg = policy.get("TargetTrackingScalingPolicyConfiguration") or {}
    predefined = (cfg.get("PredefinedMetricSpecification") or {}).get("PredefinedMetricType")
    customized = cfg.get("CustomizedMetricSpecification")
    if predefined:
        return str(predefined)
    if isinstance(customized, dict):
        namespace = customized.get("Namespace")
        metric_name = customized.get("MetricName")
        return f"{namespace}/{metric_name}"
    return "unknown"


def _extract_resource_label(policy: dict[str, Any]) -> str | None:
    cfg = policy.get("TargetTrackingScalingPolicyConfiguration") or {}
    predefined = cfg.get("PredefinedMetricSpecification") or {}
    label = predefined.get("ResourceLabel")
    if label is None:
        return None
    return str(label)


def _parse_resource_label(label: str) -> tuple[str, str] | None:
    # Formato esperado:
    # app/<lb-name>/<lb-id>/targetgroup/<tg-name>/<tg-id>
    marker = "/targetgroup/"
    if marker not in label:
        return None
    lb_dim, tail = label.split(marker, 1)
    tg_dim = f"targetgroup/{tail}"
    return lb_dim, tg_dim


def describe_target_tracking(
    app_asg: Any,
    cw: Any,
    *,
    cluster_name: str,
    services: list[str],
    print_metric_points: bool,
) -> int:
    all_alarm_names: set[str] = set()
    found_policies = 0
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=20)

    for service_name in services:
        resource_id = f"service/{cluster_name}/{service_name}"
        resp = app_asg.describe_scaling_policies(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
        )
        policies = resp.get("ScalingPolicies", [])
        print(f"\n=== {service_name} ({resource_id}) ===")
        if not policies:
            print("Sem scaling policies.")
            continue

        for policy in policies:
            policy_name = str(policy.get("PolicyName", ""))
            policy_type = str(policy.get("PolicyType", ""))
            print(f"- policy={policy_name} type={policy_type}")

            if policy_type != "TargetTrackingScaling":
                continue

            found_policies += 1
            cfg = policy.get("TargetTrackingScalingPolicyConfiguration") or {}
            target_value = cfg.get("TargetValue")
            scale_out_cd = cfg.get("ScaleOutCooldown")
            scale_in_cd = cfg.get("ScaleInCooldown")
            disable_in = cfg.get("DisableScaleIn")
            metric_summary = _extract_metric_summary(policy)
            resource_label = _extract_resource_label(policy)
            alarms = policy.get("Alarms") or []

            print(
                f"  metric={metric_summary} target={target_value} "
                f"scaleOutCooldown={scale_out_cd}s scaleInCooldown={scale_in_cd}s "
                f"disableScaleIn={disable_in}"
            )
            if resource_label:
                print(f"  resourceLabel={resource_label}")

            if alarms:
                print("  alarms:")
                for alarm in alarms:
                    name = alarm.get("AlarmName")
                    arn = alarm.get("AlarmARN")
                    if name:
                        all_alarm_names.add(str(name))
                    print(f"    - {name} ({arn})")
            else:
                print("  alarms: none")

            if print_metric_points and resource_label and metric_summary == "ALBRequestCountPerTarget":
                parsed = _parse_resource_label(resource_label)
                if not parsed:
                    print("  metric_points: resourceLabel inesperado; ignorando")
                    continue
                lb_dim, tg_dim = parsed
                data = cw.get_metric_statistics(
                    Namespace="AWS/ApplicationELB",
                    MetricName="RequestCountPerTarget",
                    Dimensions=[
                        {"Name": "LoadBalancer", "Value": lb_dim},
                        {"Name": "TargetGroup", "Value": tg_dim},
                    ],
                    StartTime=start,
                    EndTime=now,
                    Period=60,
                    Statistics=["Average", "Maximum"],
                )
                points = sorted(data.get("Datapoints", []), key=lambda x: x["Timestamp"])
                if not points:
                    print("  metric_points(20m): sem datapoints")
                else:
                    print("  metric_points(20m, period=60s):")
                    for p in points[-10:]:
                        ts = p["Timestamp"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        avg = p.get("Average")
                        mx = p.get("Maximum")
                        print(f"    - {ts} avg={avg} max={mx}")

    if not all_alarm_names:
        print("\nNenhum alarme vinculado às policies foi encontrado.")
        return found_policies

    print("\n=== CloudWatch Alarms (detalhe) ===")
    alarm_names = sorted(all_alarm_names)
    for i in range(0, len(alarm_names), 100):
        chunk = alarm_names[i : i + 100]
        alarms_resp = cw.describe_alarms(AlarmNames=chunk)
        alarms = alarms_resp.get("MetricAlarms", [])
        for alarm in sorted(alarms, key=lambda a: str(a.get("AlarmName", ""))):
            name = str(alarm.get("AlarmName"))
            period = alarm.get("Period")
            evals = alarm.get("EvaluationPeriods")
            dta = alarm.get("DatapointsToAlarm")
            state = alarm.get("StateValue")
            comp = alarm.get("ComparisonOperator")
            threshold = alarm.get("Threshold")
            treat = alarm.get("TreatMissingData")
            namespace = alarm.get("Namespace")
            metric_name = alarm.get("MetricName")
            dimensions = alarm.get("Dimensions") or []
            print(
                f"- {name}: state={state} period={period}s evals={evals} dta={dta} "
                f"comp={comp} threshold={threshold} treatMissing={treat}"
            )
            if namespace or metric_name:
                print(f"  metric={namespace}/{metric_name} dimensions={json.dumps(dimensions, ensure_ascii=False)}")
            if alarm.get("Metrics"):
                print(f"  metric_math={json.dumps(alarm['Metrics'], ensure_ascii=False)}")

    return found_policies


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostica policies/alarmes criados automaticamente por Target Tracking (ECS)."
    )
    parser.add_argument("--region", required=True, help="Região AWS (ex: us-east-1)")
    parser.add_argument("--cluster", required=True, help="Nome do cluster ECS")
    parser.add_argument(
        "--services",
        required=True,
        help="Lista CSV de serviços ECS (ex: DijkFood-Api-Service,Routing-Worker-Service,DijkFood-Location-Service)",
    )
    parser.add_argument(
        "--with-metric-points",
        action="store_true",
        help="Também consulta os últimos datapoints (20m) de RequestCountPerTarget (period=60s).",
    )
    args = parser.parse_args()

    services = _parse_services(args.services)
    if not services:
        raise SystemExit("Nenhum serviço informado em --services.")

    session = boto3.Session(region_name=args.region)
    app_asg = session.client("application-autoscaling")
    cw = session.client("cloudwatch")

    found = describe_target_tracking(
        app_asg,
        cw,
        cluster_name=args.cluster,
        services=services,
        print_metric_points=bool(args.with_metric_points),
    )
    print(f"\nTarget tracking policies encontradas: {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
