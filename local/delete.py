import argparse
import os
import time

import boto3
from botocore.exceptions import ClientError

from local.constants import (
	ALB_SG_NAME,
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


def log(message: str) -> None:
	print(f"[delete] {message}")


def warn(message: str) -> None:
	print(f"[warn] {message}")


def safe(callable_, label: str):
	try:
		return callable_()
	except ClientError as error:
		warn(f"{label}: {error.response.get('Error', {}).get('Code', 'ClientError')} - {error}")


def get_lb_arn(elbv2):
	load_balancers = safe(lambda: elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME]).get("LoadBalancers", []), "describe-load-balancers") or []
	return load_balancers[0]["LoadBalancerArn"] if load_balancers else None


def get_tg_arn(elbv2):
	target_groups = safe(lambda: elbv2.describe_target_groups(Names=[TARGET_GROUP_NAME]).get("TargetGroups", []), "describe-target-groups") or []
	return target_groups[0]["TargetGroupArn"] if target_groups else None


def resolve_vpc_id(ec2, elbv2):
	load_balancers = safe(lambda: elbv2.describe_load_balancers(Names=[LOAD_BALANCER_NAME]).get("LoadBalancers", []), "describe-load-balancers") or []
	if load_balancers:
		return load_balancers[0].get("VpcId")

	env_vpc = os.getenv("VPC_ID", "").strip()
	if env_vpc:
		return env_vpc

	default_vpcs = safe(lambda: ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", []), "describe-vpcs-default") or []
	return default_vpcs[0].get("VpcId") if default_vpcs else None


def get_sg_id(ec2, name, vpc_id=None):
	filters = [{"Name": "group-name", "Values": [name]}]
	if vpc_id:
		filters.append({"Name": "vpc-id", "Values": [vpc_id]})
	groups = safe(lambda: ec2.describe_security_groups(Filters=filters).get("SecurityGroups", []), f"describe-security-groups ({name})")
	return groups[0]["GroupId"] if groups else None


def get_session(region: str = REGION):
	return boto3.Session(region_name=region)


def run_cleanup(db_id, table_name, bucket_name, sg_name, region: str = REGION):
	"""Cleanup helper mantido para compatibilidade com a branch armazenamento."""
	session = get_session(region)

	rds = session.client("rds")
	dynamo = session.client("dynamodb")
	s3_res = session.resource("s3")
	ec2 = session.client("ec2")

	try:
		rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
		rds.get_waiter("db_instance_deleted").wait(DBInstanceIdentifier=db_id)
		log("RDS removido")
	except Exception:
		warn("RDS nao encontrado ou nao pode ser removido")

	try:
		dynamo.delete_table(TableName=table_name)
		log("DynamoDB removido")
	except Exception:
		warn("Tabela DynamoDB nao encontrada")

	try:
		bucket = s3_res.Bucket(bucket_name)
		bucket.objects.all().delete()
		bucket.delete()
		log("Bucket S3 limpo e removido")
	except Exception:
		warn("Bucket S3 nao encontrado")

	time.sleep(10)
	try:
		ec2.delete_security_group(GroupName=sg_name)
		log("Security Group removido")
	except Exception as error:
		warn(f"Erro ao deletar security group: {error}")


def main(argv=None):
	parser = argparse.ArgumentParser(description="Teardown de recursos AWS do projeto")
	parser.add_argument("--region", default=REGION)
	parser.add_argument("--keep-ecr", action="store_true", help="Nao remove repositorio ECR")
	args = parser.parse_args(argv)

	session = get_session(args.region)
	ecs = session.client("ecs")
	elbv2 = session.client("elbv2")
	autoscaling = session.client("application-autoscaling")
	ec2 = session.client("ec2")
	logs = session.client("logs")
	dynamodb = session.client("dynamodb")
	ecr = session.client("ecr")
	vpc_id = resolve_vpc_id(ec2, elbv2)

	log("1) Removendo service ECS")
	safe(lambda: ecs.update_service(cluster=CLUSTER_NAME, service=SERVICE_NAME, desiredCount=0), "update-service desired=0")
	safe(lambda: ecs.delete_service(cluster=CLUSTER_NAME, service=SERVICE_NAME, force=True), "delete-service")

	resource_id = f"service/{CLUSTER_NAME}/{SERVICE_NAME}"
	log("2) Removendo autoscaling policy/target")
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

	log("3) Removendo listener/ALB/TargetGroup")
	lb_arn = get_lb_arn(elbv2)
	tg_arn = get_tg_arn(elbv2)
	if lb_arn:
		for listener in safe(lambda: elbv2.describe_listeners(LoadBalancerArn=lb_arn).get("Listeners", []), "describe-listeners") or []:
			safe(lambda arn=listener["ListenerArn"]: elbv2.delete_listener(ListenerArn=arn), f"delete-listener {listener.get('ListenerArn')}")
		safe(lambda: elbv2.delete_load_balancer(LoadBalancerArn=lb_arn), "delete-load-balancer")
	if tg_arn:
		safe(lambda: elbv2.delete_target_group(TargetGroupArn=tg_arn), "delete-target-group")

	log("4) Removendo security groups customizados")
	for name in (TASK_SG_NAME, ALB_SG_NAME):
		sg_id = get_sg_id(ec2, name, vpc_id=vpc_id)
		if sg_id:
			safe(lambda gid=sg_id: ec2.delete_security_group(GroupId=gid), f"delete-sg {sg_id}")

	log("5) Removendo task definitions")
	for arn in safe(lambda: ecs.list_task_definitions(familyPrefix=TASK_FAMILY).get("taskDefinitionArns", []), "list-task-definitions") or []:
		safe(lambda task_definition=arn: ecs.deregister_task_definition(taskDefinition=task_definition), f"deregister {arn}")

	log("6) Removendo cluster ECS")
	safe(lambda: ecs.delete_cluster(cluster=CLUSTER_NAME), "delete-cluster")

	log("7) Removendo log group")
	safe(lambda: logs.delete_log_group(logGroupName=LOG_GROUP_NAME), "delete-log-group")

	log("8) Removendo tabela DynamoDB")
	safe(lambda: dynamodb.delete_table(TableName=TABLE_NAME), "delete-table")

	if args.keep_ecr:
		log("9) Mantendo ECR (flag --keep-ecr)")
	else:
		log("9) Removendo imagens/repo ECR")
		images = safe(lambda: ecr.list_images(repositoryName=ECR_REPO).get("imageIds", []), "list-images") or []
		if images:
			safe(lambda: ecr.batch_delete_image(repositoryName=ECR_REPO, imageIds=images), "batch-delete-image")
		safe(lambda: ecr.delete_repository(repositoryName=ECR_REPO, force=True), "delete-repository")

	log("Teardown finalizado.")


if __name__ == "__main__":
	main()
