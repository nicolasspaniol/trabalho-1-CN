# Trabalho 1 - Tracker Temporario

Este arquivo serve como guia rapido de execucao e checklist de alinhamento entre os grupos.

## 1) Comando unico de deploy

Entrada oficial:

- uv run --project local local/deploy.py

Variacoes uteis:

- uv run --project local local/deploy.py --no-delete
- uv run --project local local/deploy.py --only-delete
- uv run --project local local/deploy.py --with-simulation
- uv run --project local local/deploy.py --with-simulation --graph-file <arquivo.pkl> --graph-location "<regiao>"

Para simulacao com Basic Auth (sem export manual):

- usar credenciais do services/api/.env automaticamente:
	uv run --project local local/deploy.py --with-simulation
- ou informar no comando:
	uv run --project local local/deploy.py --with-simulation --api-username <usuario> --api-password <senha>

## 2) Checklist de contrato API (tracker)

Status atual:

- [x] Endpoints de cadastro/listagem de customers, merchants e couriers alinhados nos scripts locais.
- [ ] Contrato final de orders definido no OpenAPI (paths, payload e respostas).
- [ ] Contrato final de locations definido no OpenAPI (payload e respostas).
- [ ] Confirmar campo de retorno do pedido criado (id vs order_id).
- [ ] Confirmar regra final de item_ids no pedido.

Checklist para o grupo API publicar no OpenAPI:

- [ ] POST /orders/ (criar pedido)
- [ ] GET /orders/{id} (consulta pedido)
- [ ] GET /orders?status=... (fila por status)
- [ ] PATCH /orders/{id}/status (transicao de estado)
- [ ] POST /locations (telemetria do courier)

## 3) Checklist de infraestrutura (tracker)

- [ ] Automatizar policy/versioning/lifecycle do bucket S3 do grafo.
- [x] Teardown remove bucket S3, RDS, DynamoDB e ECR por padrão.
- [x] Teardown faz retry simples para DependencyViolation em security groups.
- [ ] Esperar remocao completa de tarefas/ENIs antes de delete de cluster e SG.

## 4) Fluxo recomendado de execucao

1. Validar credenciais AWS em .aws/credentials e .aws/config na raiz.
2. Rodar deploy com smoke tests:
	uv run --project local local/deploy.py
3. Rodar experimento com carga:
	uv run --project local local/deploy.py --with-simulation --no-delete
4. Encerrar e limpar recursos:
	uv run --project local local/deploy.py --only-delete

Observacao:

- O modo `--with-simulation` exige uma API com `/customers/`, `/merchants/`, `/couriers/`, `/orders/`, `/orders/{id}`, `/orders/{id}/status` e `/locations`.
- Se o ALB apontar para o worker de smoke tests, a simulacao completa nao roda; apenas o fluxo de carga inicial e populacao pode ser validado.

## 5) Arquivos chave

- local/deploy.py: orquestrador unico do ciclo (deploy, smoke, simulacao, teardown).
- local/load.py: populacao de customers, merchants e couriers.
- local/sim_client.py: gerador de carga para pedidos.
- local/sim_delivery.py: simulacao de entregadores e telemetria.
- local/create_infra.py: provisionamento principal da infraestrutura.
- local/delete.py: teardown dos recursos.
