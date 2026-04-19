# DijkFood - Trabalho A1 (Computacao em Nuvem)

Este repositorio implementa a plataforma DijkFood, um sistema de delivery com:

- API REST para clientes, restaurantes, entregadores e pedidos.
- Calculo de rotas no grafo viario de Sao Paulo.
- Ciclo de vida completo do pedido.
- Deploy automatizado na AWS com simulacao de carga.

O foco do trabalho (conforme enunciado) e validar disponibilidade, escalabilidade horizontal e latencia P95 abaixo de 500 ms nos cenarios de carga.

## Integrantes do Grupo
- [Gabrielly Chacara](https://github.com/gabriellyepc)
- [Henrique Coelho](https://github.com/riqueu)
- [Henrique Gasparelo](https://github.com/HenryGasparelo)
- [Nícolas Spaniol](https://github.com/nicolasspaniol)

## Resumo rapido

- Entrada principal: local/deploy.py
- Servicos: worker, api, location
- Infra principal: ECS/Fargate, ALB, RDS, DynamoDB, S3, CloudWatch
- Simulacao de carga: local/load.py + local/sim_delivery.py

## Arquitetura

### Componentes

- worker:
Responsavel por calcular rota e selecionar courier.

- api:
Servico de dominio (clientes, restaurantes, couriers, pedidos e transicoes de status).

- location:
Servico para atualizacao/consulta de localizacao de courier durante entrega.

### Dados

- RDS (PostgreSQL)
Persistencia transacional do dominio: usuarios, restaurantes, couriers, pedidos e eventos.

- DynamoDB
Dados operacionais de rota e localizacao em tempo real.

- S3
Armazena o arquivo de grafo usado no calculo de rota.

### Fluxo simplificado

1. Cliente cria pedido na API.
2. Restaurante aceita e marca pedido como pronto.
3. API aciona busca assincrona de courier (worker).
4. Courier executa eventos de entrega e atualiza localizacao.
5. Cliente consulta estado e progresso do pedido.

## Estrutura do repositorio

- local/: scripts de deploy, setup, validacao e simulacao
- services/api/: API de dominio (FastAPI)
- services/worker/: servico de roteamento
- services/location/: servico de localizacao
- shared/: modelos compartilhados

## Pre-requisitos

### 1) Ferramentas locais

- Python 3.13+
- uv
- Docker (daemon ativo)
- AWS CLI v2

### 2) Credenciais AWS (obrigatorio)

Voce precisa de credenciais validas com permissao para ECS, ECR, IAM (uso de role), ELBv2, EC2, RDS, DynamoDB, S3 e CloudWatch.

Formas aceitas no projeto:

- Variaveis de ambiente AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN
- Arquivos locais .aws/credentials e .aws/config na raiz do repo
- Configuracao padrao do usuario em ~/.aws

Observacao importante para AWS Academy:

- Use credenciais temporarias com session token.
- Se expirar, o deploy falha logo no pre-setup.

### 3) Role de execucao

Por padrao o deploy usa a role LabRole.
Se necessario, ajuste por variavel de ambiente EXECUTION_ROLE_NAME.

## Execucao

### Fluxo recomendado (com simulacao)

```bash
uv run --project local local/deploy.py --with-simulation
```

Esse comando faz:

1. Pre-setup (ECR login/build/push).
2. Provisionamento/atualizacao da infraestrutura.
3. Readiness checks dos 3 servicos.
4. Preflight de endpoints.
5. Populacao da base e simulacao de carga.
6. Smoke tests finais.
7. Teardown (delete) dos serviços AWS.

### Outros modos uteis

```bash
# Deploy + testes sem simulacao + sem teardown
uv run --project local local/deploy.py --no-delete

# Executa apenas teardown
uv run --project local local/deploy.py --only-delete

# Simulacao com cenarios customizados
uv run --project local local/deploy.py \
	--with-simulation \
	--num-users 1000 \
	--rps 25 \
	--sim-duration 120 \
	--skip-pre-setup \
	--no-delete
```

### Parametros frequentes

- --with-simulation: liga validacao + simulador
- --no-delete: mantem recursos ativos ao final
- --only-delete: remove recursos
- --skip-pre-setup: reaproveita imagens ja publicadas no ECR (evita build/push no inicio)
- --simulation-only: nao faz deploy; usa servicos ja ativos e executa apenas a simulacao
- --api-username e --api-password: credenciais Basic Auth da API
- --graph-file e --graph-location: arquivo/regiao do grafo
- --simulation-api-url: URL alternativa da API para simulacao

### Escopo geografico da simulacao (bairro vs cidade inteira)

O padrao atual do projeto usa Sao Paulo inteira:

- graph-location: "Sao Paulo, Sao Paulo, Brazil"
- graph-file: sp_cidade.pkl

Voce pode customizar a localizacao por CLI ou variaveis de ambiente (GRAPH_LOCATION, GRAPH_FILE, MAPAS_FILE).

Exemplo com padrao (cidade inteira):

```bash
uv run --project local local/deploy.py \
	--with-simulation \
	--graph-location "Sao Paulo, Sao Paulo, Brazil" \
	--graph-file sp_cidade.pkl \
	--no-delete
```

Exemplo customizado para um bairro (mais rapido e barato):

```bash
uv run --project local local/deploy.py \
	--with-simulation \
	--graph-location "Alto de Pinheiros, Sao Paulo, Brazil" \
	--graph-file sp_altodepinheiros.pkl \
	--no-delete
```

Aviso:

- O grafo da cidade inteira pode aumentar bastante tempo de extração/upload e custo de execução.
- Se o arquivo ja existir no S3 com a mesma chave, o deploy reaproveita o objeto para acelerar o ciclo.
- Para forcar regeneracao/upload do grafo, execute com `FORCE_GRAPH_REBUILD=1`.
- Para testes de carga repetidos no mesmo ambiente, voce pode pular reaplicacao do schema SQL com `DB_SKIP_SCHEMA_LOAD=1`.
- Para testes pesados que batem quota de vCPU do ECS/Fargate durante rollout, use `DEPLOY_SERIAL_ROLLOUT=1` para atualizar os servicos em serie (worker -> location -> api).
- Para reduzir conexoes ociosas ao RDS no worker, use `WORKER_DB_POOL_MIN_CONNECTIONS=0` (padrao atual) e ajuste `WORKER_DB_POOL_MAX_CONNECTIONS` conforme o limite do banco.
- Se o ambiente estiver instavel no preflight (ex.: banco saturado) e voce quiser mesmo assim medir carga, use `SIM_SKIP_PREFLIGHT_ON_FAILURE=1`.

### Rodar apenas simulacao (sem novo deploy)

Quando a infraestrutura ja estiver ativa, use `--simulation-only` para evitar o tempo de rollout completo:

```bash
SKIP_PRE_SETUP=1 uv run --active --project local local/deploy.py \
	--simulation-only \
	--with-simulation \
	--num-users 5000 \
	--rps 200 \
	--sim-duration 300
```

## API na interface web

Quando o deploy finalizar, abra o DNS do ALB da API:

- Swagger UI: /docs
- ReDoc: /redoc
- OpenAPI: /openapi.json

Exemplo real de endpoint:

- http://alb-dijkfood-api-896780006.us-east-1.elb.amazonaws.com/docs

## Observacoes de comportamento

- O endpoint /couriers/me/order existe e e usado no preflight.
- O aviso de preflight sobre courier sem pedido ativo pode acontecer por assincronia na atribuicao do courier.
- Esse aviso, isoladamente, nao significa endpoint removido.

## Execucao local com Docker Compose (ambiente de desenvolvimento)

Opcionalmente, voce pode subir componentes locais para desenvolvimento:

```bash
docker compose up --build
```

Servicos expostos localmente:

- API: http://localhost:8000/docs
- Location: http://localhost:8050/docs
- DynamoDB local: http://localhost:8020

## Criterios de validacao do trabalho

Os scripts de simulacao ajudam a demonstrar os pontos do enunciado:

- throughput alvo por cenario
- latencia P95 de leitura/escrita
- isolamento de degradacao entre componentes
- ciclo de entrega com transicoes de status validas

## Troubleshooting rapido

- Erro de credencial AWS
Verifique token expirado, perfil e permissao de acesso.

- Falha no build/push
Confirme Docker ativo e permissao para ECR.

- Erro de readiness no ALB
Verifique logs no CloudWatch e health checks dos target groups.

- Falha em transicao de status
Confirme sequencia valida: CONFIRMED -> PREPARING -> READY_FOR_PICKUP -> PICKED_UP -> IN_TRANSIT -> DELIVERED.
