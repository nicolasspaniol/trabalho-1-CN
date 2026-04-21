# DijkFood - Trabalho de Computacao em Nuvem

Projeto de simulacao de um app de delivery com arquitetura em microservicos na AWS.

Objetivo principal do trabalho:
- validar escalabilidade horizontal com ECS/Fargate
- manter latencia P95 de leitura e escrita abaixo de 500ms em steady-state
- demonstrar resiliencia e fluxo fim a fim de pedidos

## Integrantes
- [Gabrielly Chacara](https://github.com/gabriellyepc)
- [Henrique Coelho](https://github.com/riqueu)
- [Henrique Gasparelo](https://github.com/HenryGasparelo)
- [Nícolas Spaniol](https://github.com/nicolasspaniol)

## Visao geral

Servicos:
- `Routing-Worker-Service` (worker): escolhe courier e calcula rota no grafo
- `DijkFood-Api-Service` (api): dominio (clientes, merchants, couriers, orders)
- `DijkFood-Location-Service` (location): atualizacao de localizacao do courier

Dados:
- RDS PostgreSQL: dados transacionais (`user`, `customer`, `merchant`, `courier`, `order`, `order_event`)
- DynamoDB: dados de rota/localizacao em tempo real
- Grafo viário: arquivo `sp_cidade.pkl` já incluído na imagem do worker (não depende mais de S3)

Infra:
- ECS Fargate + ALB por servico
- Autoscaling com CPU/Mem + alarmes manuais de ALB RequestCountPerTarget
- CloudWatch logs/metricas

## Estrutura do repositorio

- `local/deploy.py`: orquestrador principal (deploy, simulacao, teardown)
- `local/create_infra.py`: provisionamento ECS/ALB/autoscaling
- `local/load.py`: carga inicial de customers/merchants/couriers
- `local/sim_client.py`: gerador de carga de pedidos/leituras
- `local/sim_delivery.py`: simulador dos couriers
- `local/validate_endpoints.py`: preflight de contrato
- `local/pre_deploy_setup.sh`: build/push das imagens no ECR
- `local/delete.py`: teardown
- `local/diagnose_autoscaling_alarms.py`: diagnostico de alarmes/policies
- `services/api`, `services/worker`, `services/location`: codigo dos servicos
- `local/schema.sql`: schema aplicado no RDS

## Pre-requisitos

- Python `>=3.13`
- `uv`
- Docker ativo
- AWS CLI v2
- Credenciais AWS validas com permissao para IAM, ECS, ECR, EC2/ELBv2, RDS, DynamoDB, S3 e CloudWatch

Observacoes:
- Regiao padrao: `us-east-1` (ver `local/constants.py`)
- Role padrao: `LabRole` (override por `EXECUTION_ROLE_ARN` ou `EXECUTION_ROLE_NAME`)
- Credenciais podem vir de `.aws/credentials` na raiz, `~/.aws`, ou variaveis `AWS_*`

## Execucao rapida

Fluxo completo com simulacao:

```bash
uv run --project local local/deploy.py --with-simulation
```

Fluxo completo sem teardown:

```bash
uv run --project local local/deploy.py --with-simulation --no-delete
```

Apenas teardown:

```bash
uv run --project local local/deploy.py --only-delete
```

Simulacao apenas (infra ja ativa):

```bash
uv run --project local local/deploy.py \
  --simulation-only \
  --with-simulation \
  --num-users 1000 \
  --rps 50 \
  --sim-duration 300
```

## Fluxo real do `deploy.py`

Quando roda com deploy:
1. pre-setup opcional (`local/pre_deploy_setup.sh`) para build/push no ECR
2. cria/atualiza DynamoDB, RDS e schema SQL
3. cria/atualiza worker, location e api no ECS/ALB
4. espera DNS + health dos target groups
5. (opcional) simulacao de carga
6. testes finais de health/contrato
7. teardown, exceto com `--no-delete`

Quando roda com `--simulation-only`:
1. resolve ALBs existentes
2. reaplica policies/alarms de autoscaling (por padrao)
3. executa clean start (min/desired baseline + estabilidade)
4. roda simulacao e coleta resultados

## Flags principais de CLI

`local/deploy.py`:
- `--with-simulation`
- `--simulation-only`
- `--no-delete`
- `--only-delete`
- `--skip-pre-setup`
- `--num-users`
- `--rps`
- `--sim-duration`
- `--api-username`, `--api-password`
- `--simulation-api-url`
- `--graph-file`, `--graph-location`
- `--with-autoscaling-demo`, `--autoscaling-wait-seconds`
- `--with-fault-demo`

## Simulacao e metricas

Durante `--with-simulation` o fluxo e:
1. `load.py` popula base (customers/couriers/merchants)
2. `sim_delivery.py` roda em background
3. `sim_client.py` executa carga de pedidos/leitura
4. `simulation_reporter` escreve CSV periodico em `simulation_results/`

Caracteristicas atuais do `sim_client.py`:
- timeouts curtos por task de escrita (`SIM_WRITE_TASK_TIMEOUT`)
- controle de concorrencia (`SIM_MAX_IN_FLIGHT_WRITES`, `SIM_MAX_IN_FLIGHT_READS`)
- ramp-up progressivo de RPS (`SIM_RAMP_STEP_RPS`, `SIM_RAMP_STEP_SECONDS`, `SIM_RAMP_MAX_SECONDS`)
- steady-state configuravel (`SIM_STEADY_START_SECONDS`)
- metricas exportadas em JSON (`SIM_METRICS_PATH`) e agregadas no CSV

## Variáveis de ambiente mais úteis

Infra/deploy:
- `EXECUTION_ROLE_ARN`, `EXECUTION_ROLE_NAME`
- `DB_PASSWORD`, `DB_INSTANCE_ID`, `DB_SCHEMA_FILE`
- `FORCE_GRAPH_REBUILD=1`
- `DB_SKIP_SCHEMA_LOAD=1`
- `DEPLOY_SERIAL_ROLLOUT=1`

Autoscaling:
- `*_AUTOSCALING_MIN_CAPACITY`, `*_AUTOSCALING_MAX_CAPACITY`
- `*_REQUEST_TARGET`
- `*_SCALE_OUT_COOLDOWN`, `*_SCALE_IN_COOLDOWN`
- `*_REQUEST_SCALE_OUT_STEP`, `*_REQUEST_SCALE_IN_STEP`
- `*_REQUEST_SCALE_OUT_MULTIPLIER`, `*_REQUEST_SCALE_IN_MULTIPLIER`
- `SIM_REAPPLY_AUTOSCALING_POLICIES=0|1`

Health check ALB/ECS:
- `ALB_HEALTHCHECK_PATH`
- `ALB_HEALTHCHECK_INTERVAL_SECONDS`
- `ALB_HEALTHCHECK_TIMEOUT_SECONDS`
- `ALB_HEALTHCHECK_HEALTHY_THRESHOLD`
- `ALB_HEALTHCHECK_UNHEALTHY_THRESHOLD`
- `ALB_DEREGISTRATION_DELAY_SECONDS`
- `WORKER_HEALTHCHECK_GRACE_SECONDS`
- `API_HEALTHCHECK_GRACE_SECONDS`
- `LOCATION_HEALTHCHECK_GRACE_SECONDS`

Simulacao:
- `SIM_MIN_DURATION_SECONDS`
- `SIM_WARMUP_SECONDS`
- `SIM_STEADY_START_SECONDS`
- `SIM_RAMP_STEP_RPS`, `SIM_RAMP_STEP_SECONDS`, `SIM_RAMP_MAX_SECONDS`
- `SIM_MAX_IN_FLIGHT_WRITES`, `SIM_MAX_IN_FLIGHT_READS`
- `SIM_HTTP_CONN_LIMIT`, `SIM_HTTP_CONN_LIMIT_PER_HOST`
- `SIM_WRITE_TASK_TIMEOUT`
- `SIM_CLEAN_START_TIMEOUT_SECONDS`
- `SIM_SKIP_PREFLIGHT_ON_FAILURE=1`

## Contrato de API esperado pela simulacao

Carga inicial:
- `GET/POST /customers`
- `GET/POST /merchants`
- `GET/POST /couriers`
- `POST /orders`
- `GET /orders/{order_id}`

Fluxo de entrega:
- `GET /couriers/me/order`
- `POST /orders/{order_id}/accept`
- `POST /orders/{order_id}/ready`
- `POST /orders/{order_id}/picked_up`
- `POST /orders/{order_id}/in_transit`
- `PUT /couriers/me/location`
- opcional: `POST /orders/{order_id}/delivered`

## Diagnostico rapido

Estado de autoscaling:

```bash
uv run --project local local/autoscaling_demo.py --show-only
```

Diagnostico de alarmes/policies gerados:

```bash
uv run --project local local/diagnose_autoscaling_alarms.py \
  --region us-east-1 \
  --cluster DijkFood-Cluster \
  --services Routing-Worker-Service,DijkFood-Api-Service,DijkFood-Location-Service \
  --with-metric-points
```

## Troubleshooting

- `Credenciais AWS invalidas/expiradas`: renove token e valide com `aws sts get-caller-identity`
- `Falha no pre-setup`: confirme Docker ativo e permissao de push no ECR
- `Readiness nao converge`: veja eventos ECS e health do target group no ELB
- `Simulacao sem contrato`: use `--simulation-api-url` apontando para a API de dominio correta
- `Latencia alta no inicio`: esperado enquanto ECS escala; use janela de steady-state

## Execucao local com Docker Compose (dev)

```bash
docker compose up --build
```

Endpoints locais:
- API: `http://localhost:8000/docs`
- Location: `http://localhost:8050/docs`
- DynamoDB local: `http://localhost:8020`

