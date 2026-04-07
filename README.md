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

## 2) Checklist funcional

### Já entregue

- [x] Cadastro/listagem de customers, merchants e couriers alinhados nos scripts locais.
- [x] `local/sim_client.py` alinhado com o contrato atual de pedidos (`merchant_id`, `item_ids`, leitura por `/orders/{order_id}/events`).
- [x] `local/sim_delivery.py` adaptado para detectar contrato novo ou legado de entregador.
- [x] Deploy automatizado com credenciais AWS locais, bucket por conta, schema idempotente e teardown completo.
- [x] Simulador configurável com `--graph-file` e `--graph-location`.

### Ainda aberto

- [ ] Contrato final de couriers/me/location definido no OpenAPI.
- [ ] Contrato final de picked_up e delivered definido no OpenAPI.
- [ ] Confirmar campo de retorno do pedido criado (`id` vs `order_id`).
- [ ] Confirmar regra final de `item_ids` no pedido.
- [ ] Fechar o fluxo final de atribuição de courier ao pedido.

### Publicação esperada no OpenAPI

- [ ] POST /orders/ (criar pedido)
- [ ] GET /orders/{id} (consulta pedido)
- [ ] GET /orders?status=... (fila por status)
- [ ] PUT /couriers/me/location (telemetria do courier)
- [ ] POST /orders/{order_id}/picked_up (transicao para retirada)
- [ ] POST /orders/{order_id}/delivered (finalizacao da entrega)

## 3) Checklist não-funcional

### Já entregue

- [x] População do sistema com customers, merchants e couriers através da API.
- [x] Execução do teste de carga com taxa configurável de pedidos por segundo.
- [x] Teardown remove bucket S3, RDS, DynamoDB e ECR por padrão.
- [x] Teardown faz retry simples para DependencyViolation em security groups.
- [x] Pipeline de deploy/teste/simulação/limpeza em um único script.

### Ainda aberto

- [ ] Validar formalmente os três cenários do simulador: 10, 50 e 200 RPS.
- [ ] Confirmar que entregadores reportam posição a cada 100ms no fluxo final.
- [ ] Demonstrar p95 abaixo de 500ms para consultas e registros.
- [ ] Demonstrar que a saturação de um componente não degrada os demais.
- [ ] Demonstrar escalabilidade horizontal automática em apresentação.
- [ ] Demonstrar tolerância a falhas encerrando uma instância ao vivo.
- [ ] Fechar o modelo de custos mensal em us-east-1 para o relatório.

## 4) Checklist de infraestrutura

- [ ] Automatizar policy/versioning/lifecycle do bucket S3 do grafo.
- [ ] Esperar remocao completa de tarefas/ENIs antes de delete de cluster e SG.
- [x] Remoção completa de service, load balancer, target group, tabela DynamoDB, RDS e bucket S3 já automatizada.

## 5) Entregáveis e apresentação

### Ainda aberto

- [ ] Relatório PDF com diagrama de arquitetura.
- [ ] Relatório PDF com decisões de projeto e justificativas.
- [ ] Relatório PDF com resultados experimentais dos cenários de carga.
- [ ] Relatório PDF com análise de custos mensais.
- [ ] Demonstração ao vivo da criação de pedido, cálculo de rota, histórico e eventos.
- [ ] Demonstração ao vivo de escalabilidade e tolerância a falhas.

## 6) Fluxo recomendado de execução

1. Validar credenciais AWS em .aws/credentials e .aws/config na raiz.
2. Rodar deploy com smoke tests:
	uv run --project local local/deploy.py
3. Rodar experimento com carga:
	uv run --project local local/deploy.py --with-simulation --no-delete
4. Encerrar e limpar recursos:
	uv run --project local local/deploy.py --only-delete

Observacao:

- O modo `--with-simulation` exige o contrato final de entrega:
	- `/orders/{order_id}/accept` + `/orders/{order_id}/ready` + `/couriers/me/location` + `/orders/{order_id}/picked_up` + `/orders/{order_id}/delivered`
- O endpoint `accept` nao foi removido: ele continua coexistindo com `ready`, `picked_up` e `delivered` no contrato final.
- Se o ALB apontar para o worker de smoke tests, a simulacao completa nao roda; apenas o fluxo de carga inicial e populacao pode ser validado.

Estado resumido do projeto:

- Já entregue: deploy, smoke tests, populacao de dados, teste de carga, teardown completo e adaptacao inicial do courier worker.
- Falta fechar: contrato final do courier no OpenAPI e a logica de roteamento/entrega que dependa dele.

## 7) Arquivos chave

- local/deploy.py: orquestrador unico do ciclo (deploy, smoke, simulacao, teardown).
- local/load.py: populacao de customers, merchants e couriers.
- local/sim_client.py: gerador de carga para pedidos.
- local/sim_delivery.py: simulacao de entregadores e telemetria.
- local/create_infra.py: provisionamento principal da infraestrutura.
- local/delete.py: teardown dos recursos.

## 8) Runbook de autoscaling (ECS)

Objetivo: validar escala horizontal para todos os servicos de conteiner (os atuais e os novos).

1. Garantir autoscaling CPU + memoria para todos os servicos ECS:

	uv run --project local local/autoscaling_demo.py \
	  --cluster DijkFood-Cluster \
	  --services Routing-Worker-Service,<SERVICO_2>,<SERVICO_NOVO> \
	  --min-capacity 2 \
	  --max-capacity 20 \
	  --cpu-target 70 \
	  --memory-target 75

2. Inspecionar configuracao aplicada (sem alterar):

	uv run --project local local/autoscaling_demo.py \
	  --cluster DijkFood-Cluster \
	  --services Routing-Worker-Service,<SERVICO_2>,<SERVICO_NOVO> \
	  --show-only

3. Rodar carga para disparar escala:

	uv run --project local local/deploy.py --with-simulation --no-delete --rps-scenarios 10,50,200

4. Evidencias recomendadas para apresentar:

- DesiredTaskCount, RunningTaskCount, CPUUtilization e MemoryUtilization por servico ECS.
- RequestCountPerTarget e TargetResponseTime no ALB.
- p95 das operacoes de escrita/leitura durante 10, 50 e 200 RPS.

## 9) Runbook de tolerancia a falhas

Objetivo: provar continuidade de servico ao vivo para ECS e RDS.

1. Iniciar carga continua (para observar impacto real durante falha).

2. Parar 1 task por servico ECS e aguardar recuperacao automatica:

	uv run --project local local/fault_tolerance_demo.py \
	  --cluster DijkFood-Cluster \
	  --services Routing-Worker-Service,<SERVICO_2>,<SERVICO_NOVO> \
	  --dynamodb-table Couriers \
	  --skip-rds

3. Forcar failover do RDS (requer Multi-AZ habilitado):

	uv run --project local local/fault_tolerance_demo.py \
	  --skip-ecs \
	  --dynamodb-table Couriers \
	  --db-instance-id dijkfood-postgres

4. Executar teste combinado ECS + RDS:

	uv run --project local local/fault_tolerance_demo.py \
	  --cluster DijkFood-Cluster \
	  --services Routing-Worker-Service,<SERVICO_2>,<SERVICO_NOVO> \
	  --dynamodb-table Couriers \
	  --db-instance-id dijkfood-postgres

Notas importantes:

- DynamoDB nao tem "instancia" para encerrar (servico gerenciado regional).
- O script `fault_tolerance_demo.py` faz check de disponibilidade de DynamoDB com put/get antes e depois dos testes de falha.
- Para o relatorio, manter tambem a evidencia de retry/backoff da aplicacao para erros transitorios.
