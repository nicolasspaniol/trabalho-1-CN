import time
import os

import boto3

from local.constants import (
    ALB_SG_NAME,
    BUCKET_NAME,
    CLUSTER_NAME,
    ECR_REPO,
    IMAGE_TAG,
    LOAD_BALANCER_NAME,
    LOG_GROUP_NAME,
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


def resolve_image_uri(account_id, region):
    if "IMAGE_URI" in os.environ and os.environ["IMAGE_URI"].strip():
        return os.environ["IMAGE_URI"].strip()
    return f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO}:{IMAGE_TAG}"


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


def get_or_create_target_group(elbv2, vpc_id):
    try:
        return elbv2.create_target_group(
            Name=TARGET_GROUP_NAME,
            Protocol="HTTP",
            Port=80,
            VpcId=vpc_id,
            HealthCheckPath="/health",
            TargetType="ip",
        )["TargetGroups"][0]["TargetGroupArn"]
    except elbv2.exceptions.DuplicateTargetGroupNameException:
        return elbv2.describe_target_groups(Names=[TARGET_GROUP_NAME])["TargetGroups"][0]["TargetGroupArn"]


def get_or_create_load_balancer(elbv2, alb_sg_id, subnets):
    try:
        return elbv2.create_load_balancer(
            Name=LOAD_BALANCER_NAME,
            Subnets=subnets,
            SecurityGroups=[alb_sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
        )["LoadBalancers"][0]["LoadBalancerArn"]
    except elbv2.exceptions.DuplicateLoadBalancerNameException:
        lb = elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME])["LoadBalancers"][0]
        if alb_sg_id not in lb.get("SecurityGroups", []):
            elbv2.set_security_groups(LoadBalancerArn=lb["LoadBalancerArn"], SecurityGroups=[alb_sg_id])
        return lb["LoadBalancerArn"]


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
    image_uri = resolve_image_uri(account_id, region)

    alb_sg_id = ensure_security_group(ec2, vpc_id, ALB_SG_NAME, "Ingress publico HTTP para ALB do worker")
    task_sg_id = ensure_security_group(ec2, vpc_id, TASK_SG_NAME, "Ingress HTTP do ALB para tasks ECS")
    allow_http(ec2, alb_sg_id)
    allow_http(ec2, task_sg_id, source_group_id=alb_sg_id)

    cluster_info = ecs.describe_clusters(clusters=[cluster_name]).get("clusters", [])
    cluster_status = cluster_info[0].get("status") if cluster_info else None
    if cluster_status != "ACTIVE":
        ecs.create_cluster(clusterName=cluster_name)

    for _ in range(12):
        current = ecs.describe_clusters(clusters=[cluster_name]).get("clusters", [])
        if current and current[0].get("status") == "ACTIVE":
            break
        time.sleep(2)

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

    tg_arn = get_or_create_target_group(elbv2, vpc_id)
    lb_arn = get_or_create_load_balancer(elbv2, alb_sg_id, subnets)
    ensure_listener(elbv2, lb_arn, tg_arn)

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
                "environment": [{"name": "MAPAS_BUCKET", "value": bucket_name}, {"name": "TABLE_NAME", "value": table_name}],
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

    resource_id = f"service/{cluster_name}/{service_name}"
    autoscaling.register_scalable_target(ServiceNamespace="ecs", ResourceId=resource_id, ScalableDimension="ecs:service:DesiredCount", MinCapacity=2, MaxCapacity=20)
    autoscaling.put_scaling_policy(
        PolicyName="CpuScaling",
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": 70.0,
            "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
            "ScaleOutCooldown": 30,
            "ScaleInCooldown": 300,
        },
    )
    return True