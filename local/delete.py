import argparse
import os
import time

import boto3
from botocore.exceptions import ClientError

from local.constants import (
    ALB_SG_NAME,
    API_ALB_SG_NAME,
    API_ECR_REPO,
    API_LOAD_BALANCER_NAME,
    API_LOG_GROUP_NAME,
    API_SERVICE_NAME,
    API_TARGET_GROUP_NAME,
    API_TASK_FAMILY,
    API_TASK_SG_NAME,
    CLUSTER_NAME,
    ECR_REPO,
    LOAD_BALANCER_NAME,
    LOG_GROUP_NAME,
    REGION,
    SERVICE_NAME,
    TABLE_NAME,
    TASK_FAMILY,
    TASK_SG_NAME,
    TARGET_GROUP_NAME,
)

DB_INSTANCE_ID = os.getenv("DB_INSTANCE_ID", "dijkfood-postgres")
DB_SECURITY_GROUP_NAME = os.getenv("DB_SECURITY_GROUP_NAME", "dijkfood-rds-sg")


def log(message: str) -> None:
    print(f"[delete] {message}")


def warn(message: str) -> None:
    print(f"[warn] {message}")


def safe(callable_, label: str):
    try:
        return callable_()
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "ClientError")
        warn(f"{label}: {code} - {error}")
        return None


def get_session(region: str = REGION):
    return boto3.Session(region_name=region)


def resolve_account_id(sts) -> str:
    return sts.get_caller_identity()["Account"]


def resolve_bucket_name(account_id: str) -> str:
    env_bucket = os.getenv("BUCKET_NAME", "").strip()
    if env_bucket:
        return env_bucket
    return f"dijkfood-assets-sp-{account_id}"


def get_lb_arn(elbv2):
    lbs = safe(lambda: elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME]).get("LoadBalancers", []), "describe-load-balancers") or []
    return lbs[0]["LoadBalancerArn"] if lbs else None


def get_lb_arn_by_name(elbv2, load_balancer_name: str):
    lbs = safe(lambda: elbv2.describe_load_balancers(Names=[load_balancer_name]).get("LoadBalancers", []), "describe-load-balancers") or []
    return lbs[0]["LoadBalancerArn"] if lbs else None


def get_tg_arn(elbv2):
    tgs = safe(lambda: elbv2.describe_target_groups(Names=[TARGET_GROUP_NAME]).get("TargetGroups", []), "describe-target-groups") or []
    return tgs[0]["TargetGroupArn"] if tgs else None


def get_tg_arn_by_name(elbv2, target_group_name: str):
    tgs = safe(lambda: elbv2.describe_target_groups(Names=[target_group_name]).get("TargetGroups", []), "describe-target-groups") or []
    return tgs[0]["TargetGroupArn"] if tgs else None


def resolve_vpc_id(ec2, elbv2):
    lbs = safe(lambda: elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME]).get("LoadBalancers", []), "describe-load-balancers") or []
    if lbs:
        return lbs[0].get("VpcId")

    env_vpc = os.getenv("VPC_ID", "").strip()
    if env_vpc:
        return env_vpc

    default_vpcs = safe(
        lambda: ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", []),
        "describe-vpcs-default",
    ) or []
    return default_vpcs[0].get("VpcId") if default_vpcs else None


def get_sg_id(ec2, name, vpc_id=None):
    filters = [{"Name": "group-name", "Values": [name]}]
    if vpc_id:
        filters.append({"Name": "vpc-id", "Values": [vpc_id]})
    groups = safe(lambda: ec2.describe_security_groups(Filters=filters).get("SecurityGroups", []), f"describe-security-groups ({name})") or []
    return groups[0]["GroupId"] if groups else None


def wait_service_inactive(ecs, cluster_name: str, service_name: str, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        services = safe(lambda: ecs.describe_services(cluster=cluster_name, services=[service_name]).get("services", []), "describe-services") or []
        if not services:
            return
        status = services[0].get("status")
        running = services[0].get("runningCount", 0)
        pending = services[0].get("pendingCount", 0)
        if status == "INACTIVE" and running == 0 and pending == 0:
            return
        time.sleep(5)


def wait_cluster_no_tasks(ecs, cluster_name: str, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        arns = safe(lambda: ecs.list_tasks(cluster=cluster_name).get("taskArns", []), "list-tasks") or []
        if not arns:
            return
        time.sleep(5)


def wait_lb_deleted(elbv2, lb_arn: str, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])
            time.sleep(5)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "LoadBalancerNotFound":
                return
            warn(f"wait-lb-deleted: {error}")
            return


def delete_bucket_recursive(s3, bucket_name: str) -> None:
    log(f"8) Removendo bucket S3 {bucket_name}")

    # Remove todas as versoes e delete markers (bucket versionado ou nao).
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        objects = []
        for version in page.get("Versions", []):
            objects.append({"Key": version["Key"], "VersionId": version["VersionId"]})
        for marker in page.get("DeleteMarkers", []):
            objects.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
        if objects:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects, "Quiet": True})

    # Cobre o caso de bucket sem versionamento.
    cont = None
    while True:
        kwargs = {"Bucket": bucket_name}
        if cont:
            kwargs["ContinuationToken"] = cont
        page = s3.list_objects_v2(**kwargs)
        objs = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objs, "Quiet": True})
        if not page.get("IsTruncated"):
            break
        cont = page.get("NextContinuationToken")

    s3.delete_bucket(Bucket=bucket_name)


def delete_sg_with_retry(ec2, sg_id: str, max_attempts: int = 12) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            return
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "DependencyViolation" and attempt < max_attempts:
                time.sleep(5)
                continue
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Teardown de recursos AWS do projeto")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--keep-ecr", action="store_true", help="Nao remove repositorio ECR")
    args = parser.parse_args(argv)

    session = get_session(args.region)
    sts = session.client("sts")
    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    autoscaling = session.client("application-autoscaling")
    ec2 = session.client("ec2")
    logs = session.client("logs")
    dynamodb = session.client("dynamodb")
    ecr = session.client("ecr")
    rds = session.client("rds")
    s3 = session.client("s3")

    account_id = resolve_account_id(sts)
    bucket_name = resolve_bucket_name(account_id)
    vpc_id = resolve_vpc_id(ec2, elbv2)

    service_names = [SERVICE_NAME, API_SERVICE_NAME]
    for name in service_names:
        log(f"1) Removendo service ECS: {name}")
        safe(lambda n=name: ecs.update_service(cluster=CLUSTER_NAME, service=n, desiredCount=0), "update-service desired=0")
        safe(lambda n=name: ecs.delete_service(cluster=CLUSTER_NAME, service=n, force=True), "delete-service")
        wait_service_inactive(ecs, CLUSTER_NAME, name)

    log("2) Removendo autoscaling policy/target")
    for name in service_names:
        resource_id = f"service/{CLUSTER_NAME}/{name}"
        for policy_name in ("CpuScaling", "MemoryScaling"):
            safe(
                lambda rid=resource_id, pname=policy_name: autoscaling.delete_scaling_policy(
                    ServiceNamespace="ecs",
                    ResourceId=rid,
                    ScalableDimension="ecs:service:DesiredCount",
                    PolicyName=pname,
                ),
                "delete-scaling-policy",
            )
        safe(
            lambda rid=resource_id: autoscaling.deregister_scalable_target(
                ServiceNamespace="ecs",
                ResourceId=rid,
                ScalableDimension="ecs:service:DesiredCount",
            ),
            "deregister-scalable-target",
        )

    log("3) Removendo listener/ALB/TargetGroup")
    lb_names = [LOAD_BALANCER_NAME, API_LOAD_BALANCER_NAME]
    tg_names = [TARGET_GROUP_NAME, API_TARGET_GROUP_NAME]
    for lb_name in lb_names:
        lb_arn = get_lb_arn_by_name(elbv2, lb_name)
        if lb_arn:
            listeners = safe(lambda arn=lb_arn: elbv2.describe_listeners(LoadBalancerArn=arn).get("Listeners", []), "describe-listeners") or []
            for listener in listeners:
                safe(lambda arn=listener["ListenerArn"]: elbv2.delete_listener(ListenerArn=arn), f"delete-listener {listener.get('ListenerArn')}")
            safe(lambda arn=lb_arn: elbv2.delete_load_balancer(LoadBalancerArn=arn), "delete-load-balancer")
            wait_lb_deleted(elbv2, lb_arn)

    for tg_name in tg_names:
        tg_arn = get_tg_arn_by_name(elbv2, tg_name)
        if tg_arn:
            safe(lambda arn=tg_arn: elbv2.delete_target_group(TargetGroupArn=arn), "delete-target-group")

    log("4) Removendo task definitions")
    families = [TASK_FAMILY, API_TASK_FAMILY]
    for family in families:
        task_defs = safe(lambda fam=family: ecs.list_task_definitions(familyPrefix=fam).get("taskDefinitionArns", []), "list-task-definitions") or []
        for arn in task_defs:
            safe(lambda task_definition=arn: ecs.deregister_task_definition(taskDefinition=task_definition), f"deregister {arn}")

    log("5) Removendo cluster ECS")
    wait_cluster_no_tasks(ecs, CLUSTER_NAME)
    safe(lambda: ecs.delete_cluster(cluster=CLUSTER_NAME), "delete-cluster")

    log("6) Removendo log groups")
    for log_group in (LOG_GROUP_NAME, API_LOG_GROUP_NAME):
        safe(lambda name=log_group: logs.delete_log_group(logGroupName=name), "delete-log-group")

    log("7) Removendo tabela DynamoDB")
    safe(lambda: dynamodb.delete_table(TableName=TABLE_NAME), "delete-table")

    log(f"8) Removendo RDS {DB_INSTANCE_ID}")
    safe(lambda: rds.delete_db_instance(DBInstanceIdentifier=DB_INSTANCE_ID, SkipFinalSnapshot=True), "delete-rds")

    try:
        delete_bucket_recursive(s3, bucket_name)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "ClientError")
        if code in {"NoSuchBucket", "AccessDenied"}:
            warn(f"delete-bucket: {code} - {error}")
        else:
            warn(f"delete-bucket: {code} - {error}")

    log("10) Removendo security groups customizados")
    for name in (TASK_SG_NAME, ALB_SG_NAME, API_TASK_SG_NAME, API_ALB_SG_NAME, DB_SECURITY_GROUP_NAME):
        sg_id = get_sg_id(ec2, name, vpc_id=vpc_id)
        if not sg_id:
            continue
        try:
            delete_sg_with_retry(ec2, sg_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "ClientError")
            warn(f"delete-sg {sg_id}: {code} - {error}")

    if args.keep_ecr:
        log("11) Mantendo ECR (flag --keep-ecr)")
    else:
        log("11) Removendo imagens/repo ECR")
        for repo_name in (ECR_REPO, API_ECR_REPO):
            images = safe(lambda rn=repo_name: ecr.list_images(repositoryName=rn).get("imageIds", []), "list-images") or []
            if images:
                safe(lambda rn=repo_name, ids=images: ecr.batch_delete_image(repositoryName=rn, imageIds=ids), "batch-delete-image")
            safe(lambda rn=repo_name: ecr.delete_repository(repositoryName=rn, force=True), "delete-repository")

    log("Teardown finalizado.")


if __name__ == "__main__":
    main()
