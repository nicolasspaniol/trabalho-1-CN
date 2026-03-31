import local.create as create

# Configurações Globais
REGION = "us-east-1"
CLUSTER_NAME = "DijkFood-Cluster"
SERVICE_NAME = "Routing-Worker-Service"
TABLE_NAME = "Couriers"
BUCKET_NAME = "dijkfood-assets-sp"


def run_deployment():
    create.setup_worker_infrastructure(
        region=REGION,
        cluster_name=CLUSTER_NAME,
        service_name=SERVICE_NAME,
        table_name=TABLE_NAME,
        bucket_name=BUCKET_NAME
    )

if __name__ == "__main__":
    run_deployment()