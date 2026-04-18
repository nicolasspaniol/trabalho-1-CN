"""Configura e inspeciona autoscaling de multiplos servicos ECS."""

import argparse
import os
import time

import boto3

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
    TARGET_GROUP_NAME,
)


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_services(raw_services: str | None) -> list[str]:
    from_arg = parse_csv(raw_services)
    if from_arg:
        return from_arg

    from_env = parse_csv(os.getenv("ECS_SERVICES"))
    if from_env:
        return from_env

    # Fallback: pelo menos o servico principal atual.
    return [API_SERVICE_NAME, SERVICE_NAME, LOCATION_SERVICE_NAME]


def build_alb_resource_label(load_balancer_arn: str, target_group_arn: str) -> str:
    lb_suffix = load_balancer_arn.split("loadbalancer/", 1)[1]
    tg_suffix = target_group_arn.split("targetgroup/", 1)[1]
    return f"{lb_suffix}/targetgroup/{tg_suffix}"


def get_resource_label(elbv2, load_balancer_name: str, target_group_name: str) -> str:
    lb_arn = elbv2.describe_load_balancers(Names=[load_balancer_name])["LoadBalancers"][0]["LoadBalancerArn"]
    tg_arn = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]
    return build_alb_resource_label(lb_arn, tg_arn)


SERVICE_ALB_MAPPING = {
    API_SERVICE_NAME: (API_LOAD_BALANCER_NAME, API_TARGET_GROUP_NAME, 120.0),
    SERVICE_NAME: (LOAD_BALANCER_NAME, TARGET_GROUP_NAME, 20.0),
    LOCATION_SERVICE_NAME: (LOCATION_LOAD_BALANCER_NAME, LOCATION_TARGET_GROUP_NAME, 200.0),
}


def configure_service_autoscaling(
    autoscaling,
    cluster_name: str,
    service_name: str,
    resource_label: str | None,
    min_capacity: int,
    max_capacity: int,
    request_target: float | None,
    cpu_target: float,
    memory_target: float,
    scale_out_cooldown: int,
    scale_in_cooldown: int,
) -> None:
    resource_id = f"service/{cluster_name}/{service_name}"

    autoscaling.register_scalable_target(
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        MinCapacity=min_capacity,
        MaxCapacity=max_capacity,
    )

    autoscaling.put_scaling_policy(
        PolicyName="CpuScaling",
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": cpu_target,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ECSServiceAverageCPUUtilization"
            },
            "ScaleOutCooldown": scale_out_cooldown,
            "ScaleInCooldown": scale_in_cooldown,
        },
    )

    autoscaling.put_scaling_policy(
        PolicyName="MemoryScaling",
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": memory_target,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ECSServiceAverageMemoryUtilization"
            },
            "ScaleOutCooldown": scale_out_cooldown,
            "ScaleInCooldown": scale_in_cooldown,
        },
    )

    if resource_label and request_target is not None:
        autoscaling.put_scaling_policy(
            PolicyName="AlbRequestScaling",
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration={
                "TargetValue": request_target,
                "PredefinedMetricSpecification": {
                    "PredefinedMetricType": "ALBRequestCountPerTarget",
                    "ResourceLabel": resource_label,
                },
                "ScaleOutCooldown": scale_out_cooldown,
                "ScaleInCooldown": scale_in_cooldown,
            },
        )


def show_service_status(ecs, autoscaling, cluster_name: str, service_name: str) -> None:
    service_resp = ecs.describe_services(cluster=cluster_name, services=[service_name])
    services = service_resp.get("services", [])
    if not services:
        print(f"[autoscaling] {service_name}: servico nao encontrado")
        return

    service = services[0]
    resource_id = f"service/{cluster_name}/{service_name}"

    target_resp = autoscaling.describe_scalable_targets(
        ServiceNamespace="ecs",
        ResourceIds=[resource_id],
        ScalableDimension="ecs:service:DesiredCount",
    )
    policies_resp = autoscaling.describe_scaling_policies(
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
    )

    targets = target_resp.get("ScalableTargets", [])
    policies = policies_resp.get("ScalingPolicies", [])

    desired = service.get("desiredCount", 0)
    running = service.get("runningCount", 0)
    pending = service.get("pendingCount", 0)

    print(f"[autoscaling] {service_name}")
    print(f"  status={service.get('status')} desired={desired} running={running} pending={pending}")

    if targets:
        target = targets[0]
        print(f"  scalable_target min={target.get('MinCapacity')} max={target.get('MaxCapacity')}")
    else:
        print("  scalable_target: nao configurado")

    if policies:
        print("  policies:")
        for policy in policies:
            metric = policy.get("TargetTrackingScalingPolicyConfiguration", {}).get("PredefinedMetricSpecification", {}).get("PredefinedMetricType")
            target_value = policy.get("TargetTrackingScalingPolicyConfiguration", {}).get("TargetValue")
            print(f"    - {policy.get('PolicyName')} metric={metric} target={target_value}")
    else:
        print("  policies: nenhuma")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Configura e inspeciona autoscaling para servicos ECS")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--cluster", default=CLUSTER_NAME)
    parser.add_argument(
        "--services",
        default=None,
        help="Lista CSV de servicos ECS (ex: service-a,service-b,service-c). Se omitido, usa ECS_SERVICES ou SERVICE_NAME.",
    )
    parser.add_argument("--min-capacity", type=int, default=2)
    parser.add_argument("--max-capacity", type=int, default=20)
    parser.add_argument("--cpu-target", type=float, default=70.0)
    parser.add_argument("--memory-target", type=float, default=75.0)
    parser.add_argument(
        "--request-target",
        type=float,
        default=None,
        help="Override de target de requisicoes por target do ALB. Se omitido, usa o padrao por servico.",
    )
    parser.add_argument("--scale-out-cooldown", type=int, default=30)
    parser.add_argument("--scale-in-cooldown", type=int, default=300)
    parser.add_argument("--wait-seconds", type=int, default=0, help="Aguarda e reexibe o estado apos a configuracao.")
    parser.add_argument("--show-only", action="store_true", help="Nao altera configuracao, apenas exibe estado")
    args = parser.parse_args(argv)

    services = resolve_services(args.services)

    session = boto3.Session(region_name=args.region)
    ecs = session.client("ecs")
    autoscaling = session.client("application-autoscaling")
    elbv2 = session.client("elbv2")

    if not args.show_only:
        for service_name in services:
            print(f"[autoscaling] configurando {service_name}...")
            mapping = SERVICE_ALB_MAPPING.get(service_name)
            resource_label = None
            request_target = args.request_target
            if mapping:
                lb_name, tg_name, default_request_target = mapping
                resource_label = get_resource_label(elbv2, lb_name, tg_name)
                if request_target is None:
                    request_target = default_request_target
            configure_service_autoscaling(
                autoscaling=autoscaling,
                cluster_name=args.cluster,
                service_name=service_name,
                resource_label=resource_label,
                min_capacity=args.min_capacity,
                max_capacity=args.max_capacity,
                request_target=request_target,
                cpu_target=args.cpu_target,
                memory_target=args.memory_target,
                scale_out_cooldown=args.scale_out_cooldown,
                scale_in_cooldown=args.scale_in_cooldown,
            )

    if args.wait_seconds > 0:
        print(f"[autoscaling] aguardando {args.wait_seconds}s para reavaliar o estado...")
        time.sleep(args.wait_seconds)

    for service_name in services:
        show_service_status(ecs, autoscaling, args.cluster, service_name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
