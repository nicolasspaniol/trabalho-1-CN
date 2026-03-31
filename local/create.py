import boto3


def setup_worker_infrastructure(region, cluster_name, service_name, table_name, bucket_name):

    # Clientes AWS
    dynamodb = boto3.client('dynamodb', region_name=region)
    elbv2 = boto3.client('elbv2', region_name=region)
    ecs = boto3.client('ecs', region_name=region)
    autoscaling = boto3.client('application-autoscaling', region_name=region)

    # 1. Criar Tabela no DynamoDB com GSI (Essencial para o seu código de busca)
    try:
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{'AttributeName': 'id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'}  # Atributo para o GSI
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'StatusIndex',  # Nome que você usou no seu main.py
                'KeySchema': [{'AttributeName': 'status', 'KeyType': 'HASH'}],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        print(f"Tabela {table_name} criada com StatusIndex.")
    except dynamodb.exceptions.ResourceInUseException:
        print(f"Tabela {table_name} já existe.")

    # 2. Configurar o Target Group (ALB)
    target_group = elbv2.create_target_group(
        Name='tg-routing-worker',
        Protocol='HTTP',
        Port=80,
        VpcId='vpc-xxxxxx',  # Substitua pelo ID real da sua VPC
        HealthCheckPath='/health',
        TargetType='ip'
    )
    tg_arn = target_group['TargetGroups'][0]['TargetGroupArn']

    # 3. Registrar a Task Definition
    task_def = ecs.register_task_definition(
        family='routing-worker-task',
        networkMode='awsvpc',
        requiresCompatibilities=['FARGATE'],
        cpu='1024',
        memory='4096',  # 4GB RAM
        containerDefinitions=[{
            'name': 'worker-container',
            'image': 'sua-conta.dkr.ecr.us-east-1.amazonaws.com/worker:latest',
            'portMappings': [{'containerPort': 80}],
            'environment': [
                {'name': 'MAPAS_BUCKET', 'value': bucket_name},
                {'name': 'TABLE_NAME', 'value': table_name}
            ],
            'logConfiguration': {
                'logDriver': 'awslogs',
                'options': {
                    'awslogs-group': '/ecs/routing-worker',
                    'awslogs-region': region,
                    'awslogs-stream-prefix': 'ecs'
                }
            }
        }]
    )
    task_arn = task_def['TaskDefinition']['TaskDefinitionArn']

    # 4. Criar o Serviço no ECS
    ecs.create_service(
        cluster=cluster_name,
        serviceName=service_name,
        taskDefinition=task_arn,
        desiredCount=2,
        launchType='FARGATE',
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': ['subnet-xxxx', 'subnet-yyyy'],  # Substitua pelas suas subnets
                'assignPublicIp': 'ENABLED'
            }
        },
        loadBalancers=[{
            'targetGroupArn': tg_arn,
            'containerName': 'worker-container',
            'containerPort': 80
        }]
    )

    # 5. Configurar Auto Scaling
    resource_id = f"service/{cluster_name}/{service_name}"

    autoscaling.register_scalable_target(
        ServiceNamespace='ecs',
        ResourceId=resource_id,
        ScalableDimension='ecs:service:DesiredCount',
        MinCapacity=2,
        MaxCapacity=20
    )

    autoscaling.put_scaling_policy(
        PolicyName='CpuScaling',
        ServiceNamespace='ecs',
        ResourceId=resource_id,
        ScalableDimension='ecs:service:DesiredCount',
        PolicyType='TargetTrackingScaling',
        TargetTrackingScalingPolicyConfiguration={
            'TargetValue': 70.0,
            'PredefinedMetricSpecification': {
                'PredefinedMetricType': 'ECSServiceAverageCPUUtilization'
            },
            'ScaleOutCooldown': 30,
            'ScaleInCooldown': 300
        }
    )
    return True