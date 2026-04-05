REGION = "us-east-1"
CLUSTER_NAME = "DijkFood-Cluster"
SERVICE_NAME = "Routing-Worker-Service"
TABLE_NAME = "Couriers"
ECR_REPO = "worker"
IMAGE_TAG = "latest"
TASK_FAMILY = "routing-worker-task"
LOAD_BALANCER_NAME = "alb-routing-worker"
TARGET_GROUP_NAME = "tg-routing-worker"
LOG_GROUP_NAME = "/ecs/routing-worker"
ALB_SG_NAME = "routing-worker-alb"
TASK_SG_NAME = "routing-worker-task"
# Overrides (recomendado deixar automatico)
VPC_ID = None
SUBNETS = []