"""Executa testes de tolerancia a falhas para ECS e RDS."""

import argparse
import time
import uuid

import boto3

from local.constants import CLUSTER_NAME, REGION, SERVICE_NAME


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_services(raw_services: str | None) -> list[str]:
    from_arg = parse_csv(raw_services)
    if from_arg:
        return from_arg
    return [SERVICE_NAME]


def stop_one_task_per_service(ecs, cluster_name: str, services: list[str], reason: str) -> None:
    for service_name in services:
        response = ecs.list_tasks(cluster=cluster_name, serviceName=service_name, desiredStatus="RUNNING")
        task_arns = response.get("taskArns", [])
        if not task_arns:
            print(f"[fault] {service_name}: nenhuma task RUNNING para parar")
            continue

        target_task = task_arns[0]
        print(f"[fault] {service_name}: parando task {target_task}")
        ecs.stop_task(cluster=cluster_name, task=target_task, reason=reason)


def wait_service_recovery(ecs, cluster_name: str, services: list[str], timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    pending = set(services)

    while time.time() < deadline and pending:
        for service_name in list(pending):
            response = ecs.describe_services(cluster=cluster_name, services=[service_name])
            entries = response.get("services", [])
            if not entries:
                print(f"[fault] {service_name}: servico nao encontrado")
                pending.remove(service_name)
                continue

            service = entries[0]
            desired = service.get("desiredCount", 0)
            running = service.get("runningCount", 0)
            pending_count = service.get("pendingCount", 0)
            status = service.get("status")

            print(
                f"[fault] {service_name}: status={status} desired={desired} running={running} pending={pending_count}"
            )

            if status == "ACTIVE" and running >= max(1, desired):
                pending.remove(service_name)

        if pending:
            time.sleep(10)

    if pending:
        raise RuntimeError(f"Servicos sem recuperacao confirmada no timeout: {', '.join(sorted(pending))}")


def force_rds_failover(rds, db_instance_id: str) -> None:
    print(f"[fault] forçando failover no RDS {db_instance_id}")
    rds.reboot_db_instance(DBInstanceIdentifier=db_instance_id, ForceFailover=True)


def wait_rds_available(rds, db_instance_id: str, timeout_seconds: int = 900) -> None:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        response = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        db = response.get("DBInstances", [])[0]
        status = db.get("DBInstanceStatus")
        endpoint = db.get("Endpoint", {}).get("Address")
        multi_az = db.get("MultiAZ")
        print(f"[fault] rds={db_instance_id} status={status} multi_az={multi_az} endpoint={endpoint}")

        if status == "available":
            return

        time.sleep(20)

    raise RuntimeError(f"RDS {db_instance_id} nao voltou para status available dentro do timeout")


def check_dynamodb_rw(dynamodb, table_name: str, label: str) -> None:
    item_id = f"fault-demo-{uuid.uuid4()}"
    status = f"OK-{int(time.time())}"

    print(f"[fault] dynamodb check ({label}) table={table_name}")
    dynamodb.put_item(
        TableName=table_name,
        Item={
            "id": {"S": item_id},
            "status": {"S": status},
        },
    )

    response = dynamodb.get_item(
        TableName=table_name,
        Key={
            "id": {"S": item_id},
        },
        ConsistentRead=True,
    )
    item = response.get("Item", {})
    found_status = item.get("status", {}).get("S")
    if found_status != status:
        raise RuntimeError(
            f"DynamoDB inconsistente no check {label}: esperado={status} atual={found_status}"
        )
    print(f"[fault] dynamodb check ({label}) OK")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Demo de tolerancia a falhas (ECS + RDS)")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--cluster", default=CLUSTER_NAME)
    parser.add_argument(
        "--services",
        default=None,
        help="Lista CSV de servicos ECS para teste de falha (ex: service-a,service-b)",
    )
    parser.add_argument(
        "--db-instance-id",
        default=None,
        help="Identificador do RDS para failover (ex: dijkfood-postgres)",
    )
    parser.add_argument(
        "--dynamodb-table",
        default=None,
        help="Tabela DynamoDB para check de disponibilidade (put/get) before/after",
    )
    parser.add_argument("--skip-ecs", action="store_true", help="Nao executa stop de tasks ECS")
    parser.add_argument("--skip-rds", action="store_true", help="Nao executa failover de RDS")
    parser.add_argument("--reason", default="Chaos test demo")
    args = parser.parse_args(argv)

    session = boto3.Session(region_name=args.region)
    ecs = session.client("ecs")
    rds = session.client("rds")
    dynamodb = session.client("dynamodb")

    services = resolve_services(args.services)

    if args.dynamodb_table:
        check_dynamodb_rw(dynamodb, args.dynamodb_table, label="before")

    if not args.skip_ecs:
        stop_one_task_per_service(ecs, args.cluster, services, args.reason)
        wait_service_recovery(ecs, args.cluster, services)

    if not args.skip_rds:
        if not args.db_instance_id:
            raise ValueError("--db-instance-id e obrigatorio quando --skip-rds nao for usado")
        force_rds_failover(rds, args.db_instance_id)
        wait_rds_available(rds, args.db_instance_id)

    if args.dynamodb_table:
        check_dynamodb_rw(dynamodb, args.dynamodb_table, label="after")

    print("[fault] demo de tolerancia a falhas concluida")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
