import argparse
from dataclasses import dataclass
import time

import boto3
from botocore.exceptions import ClientError, WaiterError


@dataclass
class Config:
    region: str = "us-east-1"
    cluster_name: str = "DijkFood-Cluster"
    service_name: str = "Routing-Worker-Service"
    target_group_name: str = "tg-routing-worker"
    load_balancer_name: str = "alb-routing-worker"
    table_name: str = "Couriers"
    log_group_name: str = "/ecs/routing-worker"
    ecr_repo: str = "worker"
    task_family_prefix: str = "routing-worker-task"
    alb_sg_name: str = "routing-worker-alb"
    task_sg_name: str = "routing-worker-task"
    vpc_id: str = "vpc-0d006360e6cdf569d"


def info(msg: str) -> None:
    print(f"[delete] {msg}")


def warn(msg: str) -> None:
    print(f"[warn] {msg}")


def safe(callable_, description: str):
    try:
        return callable_()
    except WaiterError as e:
        warn(f"{description}: WaiterError - {e}")
        return None
    except ClientError as e:
        warn(f"{description}: {e.response.get('Error', {}).get('Code', 'ClientError')} - {e}")
        return None


def get_lb_arn(elbv2, cfg: Config):
    def _call():
        resp = elbv2.describe_load_balancers(Names=[cfg.load_balancer_name])
        lbs = resp.get("LoadBalancers", [])
        return lbs[0]["LoadBalancerArn"] if lbs else None

    return safe(_call, "describe-load-balancers")


def get_tg_arn(elbv2, cfg: Config):
    def _call():
        resp = elbv2.describe_target_groups(Names=[cfg.target_group_name])
        tgs = resp.get("TargetGroups", [])
        return tgs[0]["TargetGroupArn"] if tgs else None

    return safe(_call, "describe-target-groups")


def get_sg_id(ec2, group_name: str, vpc_id: str):
    def _call():
        resp = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [group_name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        groups = resp.get("SecurityGroups", [])
        return groups[0]["GroupId"] if groups else None

    return safe(_call, f"describe-security-groups ({group_name})")


def get_service_status(ecs, cfg: Config):
    def _call():
        resp = ecs.describe_services(cluster=cfg.cluster_name, services=[cfg.service_name])
        services = resp.get("services", [])
        if not services:
            return None
        return services[0].get("status")

    return safe(_call, "describe-services")


def wait_for_service_removal(ecs, cfg: Config, max_attempts: int = 12, delay_seconds: int = 5) -> None:
    for attempt in range(1, max_attempts + 1):
        status = get_service_status(ecs, cfg)
        if status is None or status == "INACTIVE":
            info("Service ECS removido ou inativo")
            return

        info(
            f"Service ainda presente (status={status}); aguardando {delay_seconds}s "
            f"[{attempt}/{max_attempts}]"
        )
        time.sleep(delay_seconds)

    warn("Service ainda presente depois da espera; seguindo o teardown")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Teardown de recursos AWS do projeto")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--keep-ecr", action="store_true", help="Nao remove repositorio ECR")
    args = parser.parse_args(argv)

    cfg = Config(region=args.region)

    session = boto3.Session(region_name=cfg.region)
    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    autoscaling = session.client("application-autoscaling")
    ec2 = session.client("ec2")
    logs = session.client("logs")
    dynamodb = session.client("dynamodb")
    ecr = session.client("ecr")

    info("1) Removendo service ECS")
    service_status = get_service_status(ecs, cfg)
    if service_status == "ACTIVE":
        safe(
            lambda: ecs.update_service(
                cluster=cfg.cluster_name,
                service=cfg.service_name,
                desiredCount=0,
            ),
            "update-service desired=0",
        )
    else:
        info(f"Service ECS nao esta ACTIVE (status={service_status}); pulando update_service")
    safe(
        lambda: ecs.delete_service(
            cluster=cfg.cluster_name,
            service=cfg.service_name,
            force=True,
        ),
        "delete-service",
    )
    wait_for_service_removal(ecs, cfg)

    info("2) Removendo autoscaling policy/target")
    resource_id = f"service/{cfg.cluster_name}/{cfg.service_name}"
    safe(
        lambda: autoscaling.delete_scaling_policy(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyName="CpuScaling",
        ),
        "delete-scaling-policy",
    )
    safe(
        lambda: autoscaling.deregister_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
        ),
        "deregister-scalable-target",
    )

    info("3) Removendo listener/ALB/TargetGroup")
    lb_arn = get_lb_arn(elbv2, cfg)
    tg_arn = get_tg_arn(elbv2, cfg)

    if lb_arn:
        listeners = safe(
            lambda: elbv2.describe_listeners(LoadBalancerArn=lb_arn).get("Listeners", []),
            "describe-listeners",
        ) or []
        for listener in listeners:
            safe(
                lambda l=listener["ListenerArn"]: elbv2.delete_listener(ListenerArn=l),
                f"delete-listener {listener.get('ListenerArn')}",
            )

        safe(lambda: elbv2.delete_load_balancer(LoadBalancerArn=lb_arn), "delete-load-balancer")
        safe(
            lambda: elbv2.get_waiter("load_balancers_deleted").wait(
                LoadBalancerArns=[lb_arn],
                WaiterConfig={"Delay": 10, "MaxAttempts": 24},
            ),
            "wait load-balancers-deleted",
        )

    if tg_arn:
        safe(lambda: elbv2.delete_target_group(TargetGroupArn=tg_arn), "delete-target-group")

    info("4) Removendo security groups customizados")
    task_sg = get_sg_id(ec2, cfg.task_sg_name, cfg.vpc_id)
    alb_sg = get_sg_id(ec2, cfg.alb_sg_name, cfg.vpc_id)

    if task_sg:
        safe(lambda: ec2.delete_security_group(GroupId=task_sg), f"delete-sg {task_sg}")
    if alb_sg:
        safe(lambda: ec2.delete_security_group(GroupId=alb_sg), f"delete-sg {alb_sg}")

    info("5) Removendo task definitions")
    task_defs = safe(
        lambda: ecs.list_task_definitions(familyPrefix=cfg.task_family_prefix).get("taskDefinitionArns", []),
        "list-task-definitions",
    ) or []
    for td in task_defs:
        safe(lambda t=td: ecs.deregister_task_definition(taskDefinition=t), f"deregister {td}")

    info("6) Removendo cluster ECS")
    safe(lambda: ecs.delete_cluster(cluster=cfg.cluster_name), "delete-cluster")

    info("7) Removendo log group")
    safe(lambda: logs.delete_log_group(logGroupName=cfg.log_group_name), "delete-log-group")

    info("8) Removendo tabela DynamoDB")
    safe(lambda: dynamodb.delete_table(TableName=cfg.table_name), "delete-table")

    if not args.keep_ecr:
        info("9) Removendo imagens/repo ECR")
        images = safe(
            lambda: ecr.list_images(repositoryName=cfg.ecr_repo).get("imageIds", []),
            "list-images",
        ) or []
        if images:
            safe(
                lambda: ecr.batch_delete_image(repositoryName=cfg.ecr_repo, imageIds=images),
                "batch-delete-image",
            )
        safe(lambda: ecr.delete_repository(repositoryName=cfg.ecr_repo, force=True), "delete-repository")
    else:
        info("9) Mantendo ECR (flag --keep-ecr)")

    info("Teardown finalizado.")


if __name__ == "__main__":
    main()
