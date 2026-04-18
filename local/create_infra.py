import time
import os

import boto3

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
    IMAGE_TAG,
    LOAD_BALANCER_NAME,
    LOG_GROUP_NAME,
    LOCATION_ALB_SG_NAME,
    LOCATION_ECR_REPO,
    LOCATION_LOAD_BALANCER_NAME,
    LOCATION_LOG_GROUP_NAME,
    LOCATION_SERVICE_NAME,
    LOCATION_TARGET_GROUP_NAME,
    LOCATION_TASK_FAMILY,
    LOCATION_TASK_SG_NAME,
    REGION,
    SERVICE_NAME,
    SUBNETS as DEFAULT_SUBNETS,
    TABLE_NAME,
    TASK_FAMILY,
    TASK_SG_NAME,
    TARGET_GROUP_NAME,
    VPC_ID as DEFAULT_VPC_ID,
)


def parse_csv_env(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_account_id(sts):
    return sts.get_caller_identity()["Account"]


def resolve_image_uri(account_id, region, repo_name: str, env_var: str):
    if env_var in os.environ and os.environ[env_var].strip():
        return os.environ[env_var].strip()
    return f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{IMAGE_TAG}"


def resolve_graph_file_key():
    return os.getenv("MAPAS_FILE", "sp_altodepinheiros.pkl").strip() or "sp_altodepinheiros.pkl"


def resolve_db_host():
    db_host = os.getenv("DB_HOST", "").strip()
    if not db_host:
        raise ValueError("DB_HOST nao definido. O worker precisa do endpoint do RDS.")
    return db_host


def resolve_db_password():
    db_password = os.getenv("DB_PASSWORD", "").strip()
    if not db_password:
        raise ValueError("DB_PASSWORD nao definido. O worker precisa da senha do RDS.")
    return db_password


def resolve_vpc_id(ec2):
    env_vpc = os.getenv("VPC_ID", "").strip()
    if env_vpc:
        return env_vpc
    if DEFAULT_VPC_ID:
        return DEFAULT_VPC_ID

    default_vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", [])
    if not default_vpcs:
        raise ValueError("Nao foi possivel resolver VPC_ID automaticamente. Defina VPC_ID no ambiente.")
    return default_vpcs[0]["VpcId"]


def resolve_subnets(ec2, vpc_id):
    env_subnets = parse_csv_env(os.getenv("SUBNETS", ""))
    if env_subnets:
        return env_subnets
    if DEFAULT_SUBNETS:
        return DEFAULT_SUBNETS

    candidates = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "map-public-ip-on-launch", "Values": ["true"]},
        ]
    ).get("Subnets", [])

    by_az = {}
    for subnet in sorted(candidates, key=lambda item: (item.get("AvailabilityZone", ""), item["SubnetId"])):
        az = subnet.get("AvailabilityZone")
        if az and az not in by_az:
            by_az[az] = subnet["SubnetId"]

    selected = list(by_az.values())[:2]
    if len(selected) < 2:
        raise ValueError("Nao foi possivel resolver ao menos 2 subnets publicas em AZs distintas.")
    return selected


def ensure_cluster_active(ecs, cluster_name: str):
    container_insights = os.getenv("ECS_CONTAINER_INSIGHTS", "enhanced").strip() or "enhanced"

    cluster_info = ecs.describe_clusters(clusters=[cluster_name]).get("clusters", [])
    cluster_status = cluster_info[0].get("status") if cluster_info else None
    if cluster_status != "ACTIVE":
        ecs.create_cluster(
            clusterName=cluster_name,
            settings=[{"name": "containerInsights", "value": container_insights}],
        )

    for _ in range(12):
        current = ecs.describe_clusters(clusters=[cluster_name]).get("clusters", [])
        if current and current[0].get("status") == "ACTIVE":
            break
        time.sleep(2)

    ecs.update_cluster_settings(
        cluster=cluster_name,
        settings=[{"name": "containerInsights", "value": container_insights}],
    )


def ensure_security_group(ec2, vpc_id, name, description):
    groups = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("SecurityGroups", [])
    if groups:
        return groups[0]["GroupId"]
    return ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc_id)["GroupId"]


def allow_http(ec2, group_id, source_group_id=None):
    permission = {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80}
    if source_group_id:
        permission["UserIdGroupPairs"] = [{"GroupId": source_group_id}]
    else:
        permission["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[permission])
    except ec2.exceptions.ClientError as error:
        if "InvalidPermission.Duplicate" not in str(error):
            raise


def get_or_create_target_group(elbv2, vpc_id, target_group_name: str):
    try:
        return elbv2.create_target_group(
            Name=target_group_name,
            Protocol="HTTP",
            Port=80,
            VpcId=vpc_id,
            HealthCheckPath="/health",
            TargetType="ip",
        )["TargetGroups"][0]["TargetGroupArn"]
    except elbv2.exceptions.DuplicateTargetGroupNameException:
        return elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]


def get_or_create_load_balancer(elbv2, alb_sg_id, subnets, load_balancer_name: str):
    try:
        return elbv2.create_load_balancer(
            Name=load_balancer_name,
            Subnets=subnets,
            SecurityGroups=[alb_sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
        )["LoadBalancers"][0]["LoadBalancerArn"]
    except elbv2.exceptions.DuplicateLoadBalancerNameException:
        lb = elbv2.describe_load_balancers(Names=[load_balancer_name])["LoadBalancers"][0]
        if alb_sg_id not in lb.get("SecurityGroups", []):
            elbv2.set_security_groups(LoadBalancerArn=lb["LoadBalancerArn"], SecurityGroups=[alb_sg_id])
        return lb["LoadBalancerArn"]


def build_alb_resource_label(load_balancer_arn: str, target_group_arn: str) -> str:
    lb_suffix = load_balancer_arn.split("loadbalancer/", 1)[1]
    tg_suffix = target_group_arn.split("targetgroup/", 1)[1]
    return f"{lb_suffix}/targetgroup/{tg_suffix}"


def ensure_listener(elbv2, lb_arn, tg_arn):
    listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn).get("Listeners", [])
    actions = [{"Type": "forward", "TargetGroupArn": tg_arn}]
    listener = next((item for item in listeners if item.get("Port") == 80), None)
    if listener:
        elbv2.modify_listener(ListenerArn=listener["ListenerArn"], DefaultActions=actions)
    else:
        elbv2.create_listener(LoadBalancerArn=lb_arn, Protocol="HTTP", Port=80, DefaultActions=actions)


def wait_service_gone(ecs, cluster_name, service_name):
    for _ in range(18):
        services = ecs.describe_services(cluster=cluster_name, services=[service_name]).get("services", [])
        if not services or services[0].get("status") == "INACTIVE":
            return
        time.sleep(5)


def ensure_service_autoscaling(
    autoscaling,
    cluster_name,
    service_name,
    resource_label=None,
    min_capacity=2,
    max_capacity=20,
    request_target=None,
    cpu_target=70.0,
    memory_target=75.0,
    scale_out_cooldown=30,
    scale_in_cooldown=300,
):
    """Configura autoscaling para um servico ECS com politicas de CPU e memoria."""
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


def setup_worker_infrastructure(region, cluster_name, service_name, table_name, bucket_name, execution_role_arn):
    if not execution_role_arn:
        raise ValueError("execution_role_arn is required")

    sts = boto3.client("sts", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    dynamodb = boto3.client("dynamodb", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    logs = boto3.client("logs", region_name=region)
    autoscaling = boto3.client("application-autoscaling", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, ECR_REPO, "IMAGE_URI")
    graph_file_key = resolve_graph_file_key()
    db_host = resolve_db_host()
    db_password = resolve_db_password()

    alb_sg_id = ensure_security_group(ec2, vpc_id, ALB_SG_NAME, "Ingress publico HTTP para ALB do worker")
    task_sg_id = ensure_security_group(ec2, vpc_id, TASK_SG_NAME, "Ingress HTTP do ALB para tasks ECS")
    allow_http(ec2, alb_sg_id)
    allow_http(ec2, task_sg_id, source_group_id=alb_sg_id)

    ensure_cluster_active(ecs, cluster_name)

    try:
        logs.create_log_group(logGroupName=LOG_GROUP_NAME)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

    try:
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}, {"AttributeName": "status", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[
                {"IndexName": "StatusIndex", "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except dynamodb.exceptions.ResourceInUseException:
        pass

    tg_arn = get_or_create_target_group(elbv2, vpc_id, TARGET_GROUP_NAME)
    lb_arn = get_or_create_load_balancer(elbv2, alb_sg_id, subnets, LOAD_BALANCER_NAME)
    ensure_listener(elbv2, lb_arn, tg_arn)
    resource_label = build_alb_resource_label(lb_arn, tg_arn)

    task_arn = ecs.register_task_definition(
        family=TASK_FAMILY,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="1024",
        memory="4096",
        executionRoleArn=execution_role_arn,
        taskRoleArn=execution_role_arn,
        containerDefinitions=[
            {
                "name": "worker-container",
                "image": image_uri,
                "portMappings": [{"containerPort": 80}],
                "environment": [
                    {"name": "MAPAS_BUCKET", "value": bucket_name},
                    {"name": "MAPAS_FILE", "value": graph_file_key},
                    {"name": "DB_HOST", "value": db_host},
                    {"name": "DB_USER", "value": "postgres"},
                    {"name": "DB_NAME", "value": "postgres"},
                    {"name": "DB_PASSWORD", "value": db_password},
                    {"name": "TABLE_NAME", "value": table_name},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": LOG_GROUP_NAME, "awslogs-region": region, "awslogs-stream-prefix": "ecs"},
                },
            }
        ],
    )["taskDefinition"]["taskDefinitionArn"]

    services = ecs.describe_services(cluster=cluster_name, services=[service_name]).get("services", [])
    status = services[0].get("status") if services else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        status = None

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": 2,
        "launchType": "FARGATE",
        "networkConfiguration": {"awsvpcConfiguration": {"subnets": subnets, "securityGroups": [task_sg_id], "assignPublicIp": "ENABLED"}},
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "worker-container", "containerPort": 80}],
    }

    if status == "ACTIVE":
        update_kwargs = dict(service_kwargs)
        update_kwargs.pop("launchType", None)
        ecs.update_service(service=service_name, forceNewDeployment=True, **update_kwargs)
    else:
        ecs.create_service(serviceName=service_name, **service_kwargs)

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        min_capacity=2,
        max_capacity=20,
        request_target=1000.0,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=30,
        scale_in_cooldown=300,
    )
    return True


def setup_api_infrastructure(
    region,
    cluster_name,
    service_name,
    worker_base_url,
    location_base_url,
    db_host,
    db_password,
    execution_role_arn,
):
    if not execution_role_arn:
        raise ValueError("execution_role_arn is required")

    sts = boto3.client("sts", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    logs = boto3.client("logs", region_name=region)
    autoscaling = boto3.client("application-autoscaling", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, API_ECR_REPO, "API_IMAGE_URI")

    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")
    location_url = location_base_url or os.getenv("LOCATION_URL", "").strip()

    alb_sg_id = ensure_security_group(ec2, vpc_id, API_ALB_SG_NAME, "Ingress publico HTTP para ALB da API")
    task_sg_id = ensure_security_group(ec2, vpc_id, API_TASK_SG_NAME, "Ingress HTTP do ALB para tasks ECS da API")
    allow_http(ec2, alb_sg_id)
    allow_http(ec2, task_sg_id, source_group_id=alb_sg_id)

    ensure_cluster_active(ecs, cluster_name)

    try:
        logs.create_log_group(logGroupName=API_LOG_GROUP_NAME)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

    tg_arn = get_or_create_target_group(elbv2, vpc_id, API_TARGET_GROUP_NAME)
    lb_arn = get_or_create_load_balancer(elbv2, alb_sg_id, subnets, API_LOAD_BALANCER_NAME)
    ensure_listener(elbv2, lb_arn, tg_arn)
    resource_label = build_alb_resource_label(lb_arn, tg_arn)

    task_arn = ecs.register_task_definition(
        family=API_TASK_FAMILY,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="1024",
        memory="2048",
        executionRoleArn=execution_role_arn,
        taskRoleArn=execution_role_arn,
        containerDefinitions=[
            {
                "name": "api-container",
                "image": image_uri,
                "portMappings": [{"containerPort": 80}],
                "environment": [
                    {"name": "WORKER_URL", "value": worker_base_url},
                    {"name": "LOCATION_URL", "value": location_url},
                    {"name": "DB_USERNAME", "value": "postgres"},
                    {"name": "DB_PASSWORD", "value": db_password},
                    {"name": "DB_HOST", "value": db_host},
                    {"name": "DB_PORT", "value": "5432"},
                    {"name": "DB_NAME", "value": "postgres"},
                    {"name": "DYNAMODB_REGION", "value": region},
                    {"name": "ADMIN_USERNAME", "value": admin_username},
                    {"name": "ADMIN_PASSWORD", "value": admin_password},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": API_LOG_GROUP_NAME,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "ecs",
                    },
                },
            }
        ],
    )["taskDefinition"]["taskDefinitionArn"]

    services = ecs.describe_services(cluster=cluster_name, services=[service_name]).get("services", [])
    status = services[0].get("status") if services else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        status = None

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": 2,
        "launchType": "FARGATE",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "api-container", "containerPort": 80}],
    }

    if status == "ACTIVE":
        update_kwargs = dict(service_kwargs)
        update_kwargs.pop("launchType", None)
        ecs.update_service(service=service_name, forceNewDeployment=True, **update_kwargs)
    else:
        ecs.create_service(serviceName=service_name, **service_kwargs)

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        min_capacity=2,
        max_capacity=20,
        request_target=120.0,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=30,
        scale_in_cooldown=300,
    )
    return True


def setup_location_infrastructure(region, cluster_name, service_name, execution_role_arn):
    if not execution_role_arn:
        raise ValueError("execution_role_arn is required")

    sts = boto3.client("sts", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    logs = boto3.client("logs", region_name=region)
    autoscaling = boto3.client("application-autoscaling", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, LOCATION_ECR_REPO, "LOCATION_IMAGE_URI")

    alb_sg_id = ensure_security_group(ec2, vpc_id, LOCATION_ALB_SG_NAME, "Ingress publico HTTP para ALB da location API")
    task_sg_id = ensure_security_group(ec2, vpc_id, LOCATION_TASK_SG_NAME, "Ingress HTTP do ALB para tasks ECS da location API")
    allow_http(ec2, alb_sg_id)
    allow_http(ec2, task_sg_id, source_group_id=alb_sg_id)

    ensure_cluster_active(ecs, cluster_name)

    try:
        logs.create_log_group(logGroupName=LOCATION_LOG_GROUP_NAME)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

    tg_arn = get_or_create_target_group(elbv2, vpc_id, LOCATION_TARGET_GROUP_NAME)
    lb_arn = get_or_create_load_balancer(elbv2, alb_sg_id, subnets, LOCATION_LOAD_BALANCER_NAME)
    ensure_listener(elbv2, lb_arn, tg_arn)
    resource_label = build_alb_resource_label(lb_arn, tg_arn)

    task_arn = ecs.register_task_definition(
        family=LOCATION_TASK_FAMILY,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="512",
        memory="1024",
        executionRoleArn=execution_role_arn,
        taskRoleArn=execution_role_arn,
        containerDefinitions=[
            {
                "name": "location-container",
                "image": image_uri,
                "portMappings": [{"containerPort": 80}],
                "environment": [
                    {"name": "DYNAMODB_REGION", "value": region},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": LOCATION_LOG_GROUP_NAME,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "ecs",
                    },
                },
            }
        ],
    )["taskDefinition"]["taskDefinitionArn"]

    services = ecs.describe_services(cluster=cluster_name, services=[service_name]).get("services", [])
    status = services[0].get("status") if services else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        status = None

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": 2,
        "launchType": "FARGATE",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "location-container", "containerPort": 80}],
    }

    if status == "ACTIVE":
        update_kwargs = dict(service_kwargs)
        update_kwargs.pop("launchType", None)
        ecs.update_service(service=service_name, forceNewDeployment=True, **update_kwargs)
    else:
        ecs.create_service(serviceName=service_name, **service_kwargs)

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        min_capacity=2,
        max_capacity=20,
        request_target=200.0,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=30,
        scale_in_cooldown=300,
    )
    return True
