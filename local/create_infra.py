import time
import os

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


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"", "0", "false", "no", "off"}


def _read_int_env(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _read_float_env(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def resolve_desired_count(configured_desired: int, service_description: dict | None, prefix: str) -> int:
    # Sempre usa o valor padrão/configurado, nunca preserva o valor anterior
    return configured_desired


def build_service_deployment_settings(prefix: str, default_grace_seconds: int) -> tuple[int, dict]:
    grace_seconds = _read_int_env(f"{prefix}_HEALTHCHECK_GRACE_SECONDS", default_grace_seconds, minimum=0)
    min_healthy_percent = _read_int_env(f"{prefix}_DEPLOYMENT_MIN_HEALTHY_PERCENT", 50, minimum=0, maximum=100)
    max_percent = _read_int_env(f"{prefix}_DEPLOYMENT_MAX_PERCENT", 300, minimum=1)

    # ECS com Availability Zone Rebalancing habilitado rejeita maximumPercent <= 100
    # em update_service. Mantemos o valor o mais proximo possivel do solicitado.
    if max_percent <= 100:
        print(
            f"[infra] {prefix}_DEPLOYMENT_MAX_PERCENT={max_percent} ajustado para 101 "
            "(ECS requer maximumPercent > 100 no update_service).",
            flush=True,
        )
        max_percent = 101

    deployment_configuration = {
        "minimumHealthyPercent": min_healthy_percent,
        "maximumPercent": max_percent,
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
    }
    return grace_seconds, deployment_configuration


def resolve_account_id(sts):
    return sts.get_caller_identity()["Account"]


def resolve_image_uri(account_id, region, repo_name: str, env_var: str):
    if env_var in os.environ and os.environ[env_var].strip():
        return os.environ[env_var].strip()
    return f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{IMAGE_TAG}"


def resolve_graph_file_key():
    return os.getenv("MAPAS_FILE", "").strip() or os.getenv("GRAPH_FILE", "").strip() or "sp_cidade.pkl"


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
    health_check_path = os.getenv("ALB_HEALTHCHECK_PATH", "/health").strip() or "/health"
    health_check_interval = _read_int_env("ALB_HEALTHCHECK_INTERVAL_SECONDS", 5, minimum=5)
    health_check_timeout = _read_int_env("ALB_HEALTHCHECK_TIMEOUT_SECONDS", 2, minimum=2)
    healthy_threshold = _read_int_env("ALB_HEALTHCHECK_HEALTHY_THRESHOLD", 2, minimum=2)
    unhealthy_threshold = _read_int_env("ALB_HEALTHCHECK_UNHEALTHY_THRESHOLD", 2, minimum=2)
    deregistration_delay = _read_int_env("ALB_DEREGISTRATION_DELAY_SECONDS", 15, minimum=0)

    # O timeout precisa ser menor que o intervalo para ser aceito pelo ELBv2.
    health_check_timeout = min(health_check_timeout, max(2, health_check_interval - 1))

    try:
        tg_arn = elbv2.create_target_group(
            Name=target_group_name,
            Protocol="HTTP",
            Port=80,
            VpcId=vpc_id,
            HealthCheckPath=health_check_path,
            HealthCheckIntervalSeconds=health_check_interval,
            HealthCheckTimeoutSeconds=health_check_timeout,
            HealthyThresholdCount=healthy_threshold,
            UnhealthyThresholdCount=unhealthy_threshold,
            Matcher={"HttpCode": "200-399"},
            TargetType="ip",
        )["TargetGroups"][0]["TargetGroupArn"]
    except elbv2.exceptions.DuplicateTargetGroupNameException:
        tg_arn = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]

    elbv2.modify_target_group(
        TargetGroupArn=tg_arn,
        HealthCheckPath=health_check_path,
        HealthCheckIntervalSeconds=health_check_interval,
        HealthCheckTimeoutSeconds=health_check_timeout,
        HealthyThresholdCount=healthy_threshold,
        UnhealthyThresholdCount=unhealthy_threshold,
        Matcher={"HttpCode": "200-399"},
    )
    elbv2.modify_target_group_attributes(
        TargetGroupArn=tg_arn,
        Attributes=[
            {
                "Key": "deregistration_delay.timeout_seconds",
                "Value": str(deregistration_delay),
            }
        ],
    )
    return tg_arn


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


ECS_SERVICE_LINKED_ROLE_NAME = "AWSServiceRoleForECS"
ECS_SERVICE_LINKED_AWS_SERVICE_NAME = "ecs.amazonaws.com"


def _is_missing_ecs_service_linked_role_error(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    message = str(error.response.get("Error", {}).get("Message", "")).lower()
    if code != "InvalidParameterException":
        return False
    return (
        "service linked role" in message
        or "service-linked role" in message
        or "assume the service linked role" in message
        or "assume the service-linked role" in message
    )


def ensure_ecs_service_linked_role(region: str) -> None:
    iam = boto3.client("iam", region_name=region)

    try:
        iam.get_role(RoleName=ECS_SERVICE_LINKED_ROLE_NAME)
        return
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in {"NoSuchEntity", "NoSuchEntityException"}:
            if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
                print(
                    "[infra] Aviso: sem permissao para consultar AWSServiceRoleForECS; "
                    "seguindo com deploy e fallback por retry.",
                    flush=True,
                )
                return
            raise

    print("[infra] AWSServiceRoleForECS ausente; criando service-linked role do ECS...", flush=True)
    try:
        iam.create_service_linked_role(AWSServiceName=ECS_SERVICE_LINKED_AWS_SERVICE_NAME)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        message = str(error.response.get("Error", {}).get("Message", "")).lower()
        if code in {"EntityAlreadyExists", "EntityAlreadyExistsException"}:
            pass
        elif code in {"InvalidInput", "InvalidInputException"} and "has been taken in this account" in message:
            pass
        elif code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            raise RuntimeError(
                "Sem permissao IAM para criar AWSServiceRoleForECS. "
                "Solicite permissao iam:CreateServiceLinkedRole para ecs.amazonaws.com "
                "ou crie a role manualmente uma vez na conta."
            ) from error
        else:
            raise

    for _ in range(12):
        try:
            iam.get_role(RoleName=ECS_SERVICE_LINKED_ROLE_NAME)
            print("[infra] AWSServiceRoleForECS pronto.", flush=True)
            return
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"NoSuchEntity", "NoSuchEntityException"}:
                raise
            time.sleep(5)

    raise TimeoutError("Timeout aguardando AWSServiceRoleForECS ficar disponivel no IAM.")


def upsert_ecs_service_with_slr_retry(
    ecs,
    *,
    region: str,
    service_name: str,
    status: str | None,
    service_kwargs: dict,
) -> None:
    def _call() -> None:
        if status == "ACTIVE":
            update_kwargs = dict(service_kwargs)
            update_kwargs.pop("launchType", None)
            ecs.update_service(service=service_name, forceNewDeployment=True, **update_kwargs)
        else:
            ecs.create_service(serviceName=service_name, **service_kwargs)

    try:
        _call()
    except ClientError as error:
        if not _is_missing_ecs_service_linked_role_error(error):
            raise
        print(
            "[infra] ECS nao conseguiu assumir service-linked role; "
            "tentando criar AWSServiceRoleForECS e repetir operacao...",
            flush=True,
        )
        ensure_ecs_service_linked_role(region)
        _call()


def ensure_service_autoscaling(
    autoscaling,
    cluster_name,
    service_name,
    resource_label=None,
    cloudwatch=None,
    min_capacity=2,
    max_capacity=20,
    request_target=None,
    cpu_target=70.0,
    memory_target=75.0,
    scale_out_cooldown=12,
    scale_in_cooldown=180,
    request_scale_out_step=2,
    request_scale_in_step=1,
    request_alarm_period_seconds=15,
    request_alarm_evaluation_periods=1,
    request_scale_out_multiplier=1.02,
    request_scale_in_multiplier=0.75,
):
    """Configura autoscaling para um servico ECS com politicas de CPU/memoria e requests do ALB."""
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
        if cloudwatch is None:
            autoscaling.put_scaling_policy(
                PolicyName="AlbRequestScaling",
                ServiceNamespace="ecs",
                ResourceId=resource_id,
                ScalableDimension="ecs:service:DesiredCount",
                PolicyType="TargetTrackingScaling",
                TargetTrackingScalingPolicyConfiguration={
                    "TargetValue": float(request_target),
                    "PredefinedMetricSpecification": {
                        "PredefinedMetricType": "ALBRequestCountPerTarget",
                        "ResourceLabel": resource_label,
                    },
                    "ScaleOutCooldown": max(5, int(scale_out_cooldown)),
                    "ScaleInCooldown": max(30, int(scale_in_cooldown)),
                },
            )
            return

        try:
            load_balancer_dimension, target_group_tail = str(resource_label).split("/targetgroup/", 1)
            target_group_dimension = f"targetgroup/{target_group_tail}"
        except ValueError:
            print(
                f"[infra] Aviso: resource_label invalido para {service_name}: {resource_label!r}. "
                "Pulando step scaling por ALB.",
                flush=True,
            )
            return

        # Limpa policy antiga de target tracking para evitar conflito na interpretação de escala.
        for obsolete_policy_name in ("AlbRequestScaling",):
            try:
                autoscaling.delete_scaling_policy(
                    PolicyName=obsolete_policy_name,
                    ServiceNamespace="ecs",
                    ResourceId=resource_id,
                    ScalableDimension="ecs:service:DesiredCount",
                )
            except Exception:
                pass

        scale_out_step = _read_int_env(
            f"{service_name.upper().replace('-', '_')}_REQUEST_SCALE_OUT_STEP",
            request_scale_out_step,
            minimum=1,
        )
        scale_in_step = _read_int_env(
            f"{service_name.upper().replace('-', '_')}_REQUEST_SCALE_IN_STEP",
            request_scale_in_step,
            minimum=1,
        )

        step_out_1 = float(max(1.0, request_target * 0.10))
        step_out_2 = float(max(step_out_1 + 1.0, request_target * 0.30))
        step_in_1 = float(max(1.0, request_target * 0.20))
        step_in_2 = float(max(step_in_1 + 1.0, request_target * 0.60))
        burst_step_1 = max(4, int(scale_out_step))
        burst_step_2 = max(8, burst_step_1 * 2)
        burst_step_3 = max(12, burst_step_2 + 4)

        out_policy = autoscaling.put_scaling_policy(
            PolicyName="AlbRequestScaleOut",
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="StepScaling",
            StepScalingPolicyConfiguration={
                "AdjustmentType": "ChangeInCapacity",
                "Cooldown": max(5, int(scale_out_cooldown)),
                "MetricAggregationType": "Average",
                "StepAdjustments": [
                    {
                        "MetricIntervalLowerBound": 0.0,
                        "MetricIntervalUpperBound": step_out_1,
                        "ScalingAdjustment": burst_step_1,
                    },
                    {
                        "MetricIntervalLowerBound": step_out_1,
                        "MetricIntervalUpperBound": step_out_2,
                        "ScalingAdjustment": burst_step_2,
                    },
                    {
                        "MetricIntervalLowerBound": step_out_2,
                        "ScalingAdjustment": burst_step_3,
                    },
                ],
            },
        )

        in_policy = autoscaling.put_scaling_policy(
            PolicyName="AlbRequestScaleIn",
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="StepScaling",
            StepScalingPolicyConfiguration={
                "AdjustmentType": "ChangeInCapacity",
                "Cooldown": max(30, int(scale_in_cooldown)),
                "MetricAggregationType": "Average",
                "StepAdjustments": [
                    {
                        "MetricIntervalLowerBound": -step_in_1,
                        "MetricIntervalUpperBound": 0.0,
                        "ScalingAdjustment": -scale_in_step,
                    },
                    {
                        "MetricIntervalLowerBound": -step_in_2,
                        "MetricIntervalUpperBound": -step_in_1,
                        "ScalingAdjustment": -max(1, scale_in_step + 1),
                    },
                    {
                        "MetricIntervalUpperBound": -step_in_2,
                        "ScalingAdjustment": -max(1, scale_in_step + 2),
                    },
                ],
            },
        )

        high_threshold = max(1.0, float(request_target) * max(1.01, float(request_scale_out_multiplier)))
        low_threshold = max(1.0, float(request_target) * max(0.1, float(request_scale_in_multiplier)))
        if low_threshold >= high_threshold:
            low_threshold = max(1.0, high_threshold * 0.8)

        # Força reação em 1 minuto (period=60, eval=1), como solicitado.
        period_seconds = 60
        evaluation_periods = 1
        alarm_prefix = service_name.replace("/", "-")
        dimensions = [
            {"Name": "TargetGroup", "Value": target_group_dimension},
            {"Name": "LoadBalancer", "Value": load_balancer_dimension},
        ]

        cloudwatch.put_metric_alarm(
            AlarmName=f"{alarm_prefix}-AlbRequestHigh",
            AlarmDescription=(
                f"Scale out {service_name} quando RequestCountPerTarget >= {high_threshold:.2f}"
            ),
            Namespace="AWS/ApplicationELB",
            MetricName="RequestCountPerTarget",
            Dimensions=dimensions,
            # RequestCountPerTarget representa contagem de requests por período.
            # Para capturar pressão real no minuto, usamos Sum (não Average).
            Statistic="Sum",
            # Janela curta para reação rápida: 1 minuto e 1 avaliação.
            Period=period_seconds,
            EvaluationPeriods=evaluation_periods,
            DatapointsToAlarm=1,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            Threshold=high_threshold,
            TreatMissingData="notBreaching",
            AlarmActions=[str(out_policy["PolicyARN"])],
        )
        print(
            f"[infra] Alarme criado: {alarm_prefix}-AlbRequestHigh "
            f"threshold={high_threshold:.2f} period=60 eval=1 "
            f"dimensions={dimensions}",
            flush=True,
        )

        cloudwatch.put_metric_alarm(
            AlarmName=f"{alarm_prefix}-AlbRequestLow",
            AlarmDescription=(
                f"Scale in {service_name} quando RequestCountPerTarget <= {low_threshold:.2f}"
            ),
            Namespace="AWS/ApplicationELB",
            MetricName="RequestCountPerTarget",
            Dimensions=dimensions,
            # Mantemos Sum pela mesma razão do alarme de scale-out:
            # comparar volume por período evita leituras "sem datapoint" na prática.
            Statistic="Sum",
            # 1 minuto / 1 avaliação para detectar rapidamente queda sustentada de tráfego.
            Period=period_seconds,
            EvaluationPeriods=evaluation_periods,
            DatapointsToAlarm=1,
            ComparisonOperator="LessThanOrEqualToThreshold",
            Threshold=low_threshold,
            TreatMissingData="notBreaching",
            AlarmActions=[str(in_policy["PolicyARN"])],
        )
        print(
            f"[infra] Alarme criado: {alarm_prefix}-AlbRequestLow "
            f"threshold={low_threshold:.2f} period=60 eval=1 "
            f"dimensions={dimensions}",
            flush=True,
        )


def setup_worker_infrastructure(region, cluster_name, service_name, table_name, execution_role_arn):
    if not execution_role_arn:
        raise ValueError("execution_role_arn is required")

    sts = boto3.client("sts", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    dynamodb = boto3.client("dynamodb", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    logs = boto3.client("logs", region_name=region)
    autoscaling = boto3.client("application-autoscaling", region_name=region)
    cloudwatch = boto3.client("cloudwatch", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, ECR_REPO, "IMAGE_URI")
    graph_file_key = resolve_graph_file_key()
    db_host = resolve_db_host()
    db_password = resolve_db_password()
    # Defaults mais "parrudos" para simulação: evita fila no worker ao consultar/reservar couriers.
    worker_db_pool_min_connections = os.getenv("WORKER_DB_POOL_MIN_CONNECTIONS", "0").strip() or "0"
    worker_db_pool_max_connections = os.getenv("WORKER_DB_POOL_MAX_CONNECTIONS", "8").strip() or "8"
    worker_route_max_available_couriers = os.getenv("WORKER_ROUTE_MAX_AVAILABLE_COURIERS", "500").strip() or "500"
    worker_route_queue_wait_seconds = os.getenv("WORKER_ROUTE_QUEUE_WAIT_SECONDS", "0.8").strip() or "0.8"
    worker_route_queue_retry_interval_seconds = os.getenv("WORKER_ROUTE_QUEUE_RETRY_INTERVAL_SECONDS", "0.2").strip() or "0.2"
    worker_desired = _read_int_env("WORKER_DESIRED_COUNT", 2, minimum=1)
    worker_min_capacity = _read_int_env("WORKER_AUTOSCALING_MIN_CAPACITY", 2, minimum=1)
    worker_max_capacity = _read_int_env("WORKER_AUTOSCALING_MAX_CAPACITY", 18, minimum=worker_min_capacity)
    worker_request_target = _read_float_env("WORKER_REQUEST_TARGET", 180.0, minimum=1.0)
    worker_scale_out_cooldown = _read_int_env("WORKER_SCALE_OUT_COOLDOWN", 12, minimum=5)
    worker_scale_in_cooldown = _read_int_env("WORKER_SCALE_IN_COOLDOWN", 180, minimum=30)
    worker_request_scale_out_step = _read_int_env("WORKER_REQUEST_SCALE_OUT_STEP", 2, minimum=1)
    worker_request_scale_in_step = _read_int_env("WORKER_REQUEST_SCALE_IN_STEP", 1, minimum=1)
    worker_request_alarm_period_seconds = _read_int_env("WORKER_REQUEST_ALARM_PERIOD_SECONDS", 15, minimum=15)
    worker_request_alarm_evaluation_periods = _read_int_env("WORKER_REQUEST_ALARM_EVALUATION_PERIODS", 1, minimum=1)
    worker_request_scale_out_multiplier = _read_float_env("WORKER_REQUEST_SCALE_OUT_MULTIPLIER", 1.02, minimum=1.01)
    worker_request_scale_in_multiplier = _read_float_env("WORKER_REQUEST_SCALE_IN_MULTIPLIER", 0.75, minimum=0.1, maximum=0.99)
    worker_health_grace_seconds, worker_deployment_configuration = build_service_deployment_settings(
        "WORKER",
        default_grace_seconds=20,
    )

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
                    {"name": "MAPAS_FILE", "value": graph_file_key},
                    {"name": "DB_HOST", "value": db_host},
                    {"name": "DB_USER", "value": "postgres"},
                    {"name": "DB_NAME", "value": "postgres"},
                    {"name": "DB_PASSWORD", "value": db_password},
                    {"name": "DB_POOL_MIN_CONNECTIONS", "value": worker_db_pool_min_connections},
                    {"name": "DB_POOL_MAX_CONNECTIONS", "value": worker_db_pool_max_connections},
                    {"name": "ROUTE_MAX_AVAILABLE_COURIERS", "value": worker_route_max_available_couriers},
                    {"name": "ROUTE_QUEUE_WAIT_SECONDS", "value": worker_route_queue_wait_seconds},
                    {"name": "ROUTE_QUEUE_RETRY_INTERVAL_SECONDS", "value": worker_route_queue_retry_interval_seconds},
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
    service_description = services[0] if services else None
    status = service_description.get("status") if service_description else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        service_description = None
        status = None

    resolved_worker_desired = resolve_desired_count(worker_desired, service_description, "WORKER")
    if resolved_worker_desired != worker_desired:
        print(
            f"[infra] Worker desired preservado: {worker_desired} -> {resolved_worker_desired}",
            flush=True,
        )

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": resolved_worker_desired,
        "launchType": "FARGATE",
        "healthCheckGracePeriodSeconds": worker_health_grace_seconds,
        "deploymentConfiguration": worker_deployment_configuration,
        "networkConfiguration": {"awsvpcConfiguration": {"subnets": subnets, "securityGroups": [task_sg_id], "assignPublicIp": "ENABLED"}},
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "worker-container", "containerPort": 80}],
    }

    upsert_ecs_service_with_slr_retry(
        ecs,
        region=region,
        service_name=service_name,
        status=status,
        service_kwargs=service_kwargs,
    )

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        cloudwatch=cloudwatch,
        min_capacity=worker_min_capacity,
        max_capacity=worker_max_capacity,
        request_target=worker_request_target,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=worker_scale_out_cooldown,
        scale_in_cooldown=worker_scale_in_cooldown,
        request_scale_out_step=worker_request_scale_out_step,
        request_scale_in_step=worker_request_scale_in_step,
        request_alarm_period_seconds=worker_request_alarm_period_seconds,
        request_alarm_evaluation_periods=worker_request_alarm_evaluation_periods,
        request_scale_out_multiplier=worker_request_scale_out_multiplier,
        request_scale_in_multiplier=worker_request_scale_in_multiplier,
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
    cloudwatch = boto3.client("cloudwatch", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, API_ECR_REPO, "API_IMAGE_URI")

    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")
    location_url = location_base_url or os.getenv("LOCATION_URL", "").strip()
    # Defaults conservadores para evitar esgotar conexoes do RDS em conta/lab pequena.
    api_db_pool_size = os.getenv("API_DB_POOL_SIZE", "8").strip() or "8"
    api_db_max_overflow = os.getenv("API_DB_MAX_OVERFLOW", "8").strip() or "8"
    api_db_pool_timeout = os.getenv("API_DB_POOL_TIMEOUT", "10").strip() or "10"
    api_worker_connect_timeout = os.getenv("API_WORKER_CONNECT_TIMEOUT_SECONDS", "1.0").strip() or "1.0"
    api_worker_read_timeout = os.getenv("API_WORKER_READ_TIMEOUT_SECONDS", "4.0").strip() or "4.0"
    api_dispatch_workers = os.getenv("API_DISPATCH_WORKERS", "6").strip() or "6"
    api_auth_cache_ttl_seconds = os.getenv("API_AUTH_CACHE_TTL_SECONDS", "30").strip() or "30"
    api_auth_cache_max_size = os.getenv("API_AUTH_CACHE_MAX_SIZE", "50000").strip() or "50000"
    api_desired = _read_int_env("API_DESIRED_COUNT", 2, minimum=1)
    api_min_capacity = _read_int_env("API_AUTOSCALING_MIN_CAPACITY", 2, minimum=1)
    api_max_capacity = _read_int_env("API_AUTOSCALING_MAX_CAPACITY", 14, minimum=api_min_capacity)
    api_request_target = _read_float_env("API_REQUEST_TARGET", 220.0, minimum=1.0)
    api_scale_out_cooldown = _read_int_env("API_SCALE_OUT_COOLDOWN", 12, minimum=5)
    api_scale_in_cooldown = _read_int_env("API_SCALE_IN_COOLDOWN", 180, minimum=30)
    api_request_scale_out_step = _read_int_env("API_REQUEST_SCALE_OUT_STEP", 2, minimum=1)
    api_request_scale_in_step = _read_int_env("API_REQUEST_SCALE_IN_STEP", 1, minimum=1)
    api_request_alarm_period_seconds = _read_int_env("API_REQUEST_ALARM_PERIOD_SECONDS", 15, minimum=15)
    api_request_alarm_evaluation_periods = _read_int_env("API_REQUEST_ALARM_EVALUATION_PERIODS", 1, minimum=1)
    api_request_scale_out_multiplier = _read_float_env("API_REQUEST_SCALE_OUT_MULTIPLIER", 1.02, minimum=1.01)
    api_request_scale_in_multiplier = _read_float_env("API_REQUEST_SCALE_IN_MULTIPLIER", 0.75, minimum=0.1, maximum=0.99)
    api_health_grace_seconds, api_deployment_configuration = build_service_deployment_settings(
        "API",
        default_grace_seconds=15,
    )

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
        memory="4096",
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
                    {"name": "DB_POOL_SIZE", "value": api_db_pool_size},
                    {"name": "DB_MAX_OVERFLOW", "value": api_db_max_overflow},
                    {"name": "DB_POOL_TIMEOUT", "value": api_db_pool_timeout},
                    {"name": "WORKER_CONNECT_TIMEOUT_SECONDS", "value": api_worker_connect_timeout},
                    {"name": "WORKER_READ_TIMEOUT_SECONDS", "value": api_worker_read_timeout},
                    {"name": "DISPATCH_WORKERS", "value": api_dispatch_workers},
                    {"name": "AUTH_CACHE_TTL_SECONDS", "value": api_auth_cache_ttl_seconds},
                    {"name": "AUTH_CACHE_MAX_SIZE", "value": api_auth_cache_max_size},
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
    service_description = services[0] if services else None
    status = service_description.get("status") if service_description else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        service_description = None
        status = None

    resolved_api_desired = resolve_desired_count(api_desired, service_description, "API")
    if resolved_api_desired != api_desired:
        print(
            f"[infra] API desired preservado: {api_desired} -> {resolved_api_desired}",
            flush=True,
        )

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": resolved_api_desired,
        "launchType": "FARGATE",
        "healthCheckGracePeriodSeconds": api_health_grace_seconds,
        "deploymentConfiguration": api_deployment_configuration,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "api-container", "containerPort": 80}],
    }

    upsert_ecs_service_with_slr_retry(
        ecs,
        region=region,
        service_name=service_name,
        status=status,
        service_kwargs=service_kwargs,
    )

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        cloudwatch=cloudwatch,
        min_capacity=api_min_capacity,
        max_capacity=api_max_capacity,
        request_target=api_request_target,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=api_scale_out_cooldown,
        scale_in_cooldown=api_scale_in_cooldown,
        request_scale_out_step=api_request_scale_out_step,
        request_scale_in_step=api_request_scale_in_step,
        request_alarm_period_seconds=api_request_alarm_period_seconds,
        request_alarm_evaluation_periods=api_request_alarm_evaluation_periods,
        request_scale_out_multiplier=api_request_scale_out_multiplier,
        request_scale_in_multiplier=api_request_scale_in_multiplier,
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
    cloudwatch = boto3.client("cloudwatch", region_name=region)

    account_id = resolve_account_id(sts)
    vpc_id = resolve_vpc_id(ec2)
    subnets = resolve_subnets(ec2, vpc_id)
    image_uri = resolve_image_uri(account_id, region, LOCATION_ECR_REPO, "LOCATION_IMAGE_URI")
    location_desired = _read_int_env("LOCATION_DESIRED_COUNT", 1, minimum=1)
    location_min_capacity = _read_int_env("LOCATION_AUTOSCALING_MIN_CAPACITY", 1, minimum=1)
    location_max_capacity = _read_int_env("LOCATION_AUTOSCALING_MAX_CAPACITY", 10, minimum=location_min_capacity)
    location_request_target = _read_float_env("LOCATION_REQUEST_TARGET", 180.0, minimum=1.0)
    location_scale_out_cooldown = _read_int_env("LOCATION_SCALE_OUT_COOLDOWN", 12, minimum=5)
    location_scale_in_cooldown = _read_int_env("LOCATION_SCALE_IN_COOLDOWN", 180, minimum=30)
    location_request_scale_out_step = _read_int_env("LOCATION_REQUEST_SCALE_OUT_STEP", 2, minimum=1)
    location_request_scale_in_step = _read_int_env("LOCATION_REQUEST_SCALE_IN_STEP", 1, minimum=1)
    location_request_alarm_period_seconds = _read_int_env("LOCATION_REQUEST_ALARM_PERIOD_SECONDS", 15, minimum=15)
    location_request_alarm_evaluation_periods = _read_int_env("LOCATION_REQUEST_ALARM_EVALUATION_PERIODS", 1, minimum=1)
    location_request_scale_out_multiplier = _read_float_env("LOCATION_REQUEST_SCALE_OUT_MULTIPLIER", 1.02, minimum=1.01)
    location_request_scale_in_multiplier = _read_float_env("LOCATION_REQUEST_SCALE_IN_MULTIPLIER", 0.75, minimum=0.1, maximum=0.99)
    location_health_grace_seconds, location_deployment_configuration = build_service_deployment_settings(
        "LOCATION",
        default_grace_seconds=10,
    )

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
    service_description = services[0] if services else None
    status = service_description.get("status") if service_description else None
    if status == "DRAINING":
        ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
        wait_service_gone(ecs, cluster_name, service_name)
        service_description = None
        status = None

    resolved_location_desired = resolve_desired_count(location_desired, service_description, "LOCATION")
    if resolved_location_desired != location_desired:
        print(
            f"[infra] Location desired preservado: {location_desired} -> {resolved_location_desired}",
            flush=True,
        )

    service_kwargs = {
        "cluster": cluster_name,
        "taskDefinition": task_arn,
        "desiredCount": resolved_location_desired,
        "launchType": "FARGATE",
        "healthCheckGracePeriodSeconds": location_health_grace_seconds,
        "deploymentConfiguration": location_deployment_configuration,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        "loadBalancers": [{"targetGroupArn": tg_arn, "containerName": "location-container", "containerPort": 80}],
    }

    upsert_ecs_service_with_slr_retry(
        ecs,
        region=region,
        service_name=service_name,
        status=status,
        service_kwargs=service_kwargs,
    )

    ensure_service_autoscaling(
        autoscaling=autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        resource_label=resource_label,
        cloudwatch=cloudwatch,
        min_capacity=location_min_capacity,
        max_capacity=location_max_capacity,
        request_target=location_request_target,
        cpu_target=70.0,
        memory_target=75.0,
        scale_out_cooldown=location_scale_out_cooldown,
        scale_in_cooldown=location_scale_in_cooldown,
        request_scale_out_step=location_request_scale_out_step,
        request_scale_in_step=location_request_scale_in_step,
        request_alarm_period_seconds=location_request_alarm_period_seconds,
        request_alarm_evaluation_periods=location_request_alarm_evaluation_periods,
        request_scale_out_multiplier=location_request_scale_out_multiplier,
        request_scale_in_multiplier=location_request_scale_in_multiplier,
    )
    return True
