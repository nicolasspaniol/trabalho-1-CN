#!/usr/bin/env bash
set -euo pipefail

# =====================================
# Pre-setup para deploy ECS (AWS Academy)
# =====================================

aws configure

# Preencha os campos abaixo antes de executar.

AWS_REGION="us-east-1"
AWS_PROFILE="default"
AWS_ACCOUNT_ID=""    
ECR_REPO="worker"
IMAGE_TAG="latest"
EXECUTION_ROLE_NAME="LabRole"
EXECUTION_ROLE_ARN=""

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE_PATH="services/worker/Dockerfile"
BUILDER_NAME="xbuilder"


log() {
  printf "\n[setup] %s\n" "$1"
}

fail() {
  printf "\n[erro] %s\n" "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Comando obrigatorio nao encontrado: $1"
}

run_aws() {
  if [[ -n "$AWS_PROFILE" ]]; then
    AWS_PAGER="" aws --profile "$AWS_PROFILE" "$@"
  else
    AWS_PAGER="" aws "$@"
  fi
}

log "Validando comandos obrigatorios"
require_cmd aws
require_cmd docker
require_cmd python3

log "Entrando na raiz do projeto"
cd "$WORKDIR"

log "Validando credenciais AWS"
run_aws sts get-caller-identity >/dev/null

if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  log "Descobrindo AWS_ACCOUNT_ID via STS"
  AWS_ACCOUNT_ID="$(run_aws sts get-caller-identity --query Account --output text)"
fi

if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  fail "Nao foi possivel descobrir AWS_ACCOUNT_ID"
fi

IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
if [[ -z "$EXECUTION_ROLE_ARN" ]]; then
  EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${EXECUTION_ROLE_NAME}"
fi

log "AWS_ACCOUNT_ID: $AWS_ACCOUNT_ID"
log "IMAGE_URI: $IMAGE_URI"
log "EXECUTION_ROLE_ARN: $EXECUTION_ROLE_ARN"

log "Garantindo venv local"
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

log "Instalando boto3 na venv (se necessario)"
./.venv/bin/pip install --quiet --upgrade pip boto3

log "Garantindo repositorio ECR"
if ! run_aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1; then
  run_aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null
fi

log "Login no ECR"
run_aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

log "Configurando Docker Buildx"
if docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  log "Builder existente encontrado ($BUILDER_NAME). Recriando para evitar estado quebrado"
  docker buildx rm "$BUILDER_NAME" >/dev/null 2>&1 || true
fi

docker buildx create --name "$BUILDER_NAME" --use >/dev/null

docker buildx inspect --bootstrap >/dev/null

log "Build + push da imagem (linux/amd64)"
docker buildx build \
  --platform linux/amd64 \
  -f "$DOCKERFILE_PATH" \
  -t "$IMAGE_URI" \
  --push \
  .

log "Exportando variaveis para esta sessao"
export EXECUTION_ROLE_ARN="$EXECUTION_ROLE_ARN"
export PYTHONPATH="."