import boto3


def get_or_create_target_group(elbv2, vpc_id):
    tg_name = 'tg-routing-worker'
    try:
        response = elbv2.create_target_group(
            Name=tg_name,
            Protocol='HTTP',
            Port=80,
            VpcId=vpc_id,
            HealthCheckPath='/health',
            TargetType='ip'
        )
        return response['TargetGroups'][0]['TargetGroupArn']
    except elbv2.exceptions.DuplicateTargetGroupNameException:
        response = elbv2.describe_target_groups(Names=[tg_name])
        return response['TargetGroups'][0]['TargetGroupArn']


def get_default_security_group(ec2, vpc_id):
    response = ec2.describe_security_groups(
        Filters=[
            {'Name': 'group-name', 'Values': ['default']},
            {'Name': 'vpc-id', 'Values': [vpc_id]}
        ]
    )
    groups = response.get('SecurityGroups', [])
    if not groups:
        raise ValueError(f'No default security group found for VPC {vpc_id}')
    return groups[0]['GroupId']


def ensure_security_groups(ec2, vpc_id):
    alb_sg_name = 'routing-worker-alb'
    task_sg_name = 'routing-worker-task'

    def get_or_create(name, description):
        existing = ec2.describe_security_groups(
            Filters=[
                {'Name': 'group-name', 'Values': [name]},
                {'Name': 'vpc-id', 'Values': [vpc_id]},
            ]
        ).get('SecurityGroups', [])
        if existing:
            return existing[0]['GroupId']
        return ec2.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=vpc_id,
        )['GroupId']

    alb_sg_id = get_or_create(alb_sg_name, 'Ingress publico HTTP para ALB do worker')
    task_sg_id = get_or_create(task_sg_name, 'Ingress HTTP do ALB para tasks ECS')

    # ALB: liberar HTTP publico
    try:
        ec2.authorize_security_group_ingress(
            GroupId=alb_sg_id,
            IpPermissions=[{
                'IpProtocol': 'tcp',
                'FromPort': 80,
                'ToPort': 80,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}],
            }],
        )
    except ec2.exceptions.ClientError as e:
        if 'InvalidPermission.Duplicate' not in str(e):
            raise

    # Tasks: liberar HTTP apenas do SG do ALB
    try:
        ec2.authorize_security_group_ingress(
            GroupId=task_sg_id,
            IpPermissions=[{
                'IpProtocol': 'tcp',
                'FromPort': 80,
                'ToPort': 80,
                'UserIdGroupPairs': [{'GroupId': alb_sg_id}],
            }],
        )
    except ec2.exceptions.ClientError as e:
        if 'InvalidPermission.Duplicate' not in str(e):
            raise

    return alb_sg_id, task_sg_id


def get_or_create_load_balancer(elbv2, ec2, subnets, vpc_id, alb_sg_id):
    lb_name = 'alb-routing-worker'
    try:
        response = elbv2.create_load_balancer(
            Name=lb_name,
            Subnets=subnets,
            SecurityGroups=[alb_sg_id],
            Scheme='internet-facing',
            Type='application',
            IpAddressType='ipv4'
        )
        return response['LoadBalancers'][0]['LoadBalancerArn']
    except elbv2.exceptions.DuplicateLoadBalancerNameException:
        response = elbv2.describe_load_balancers(Names=[lb_name])
        lb = response['LoadBalancers'][0]
        lb_arn = lb['LoadBalancerArn']
        current_sgs = set(lb.get('SecurityGroups', []))
        if alb_sg_id not in current_sgs:
            elbv2.set_security_groups(
                LoadBalancerArn=lb_arn,
                SecurityGroups=[alb_sg_id],
            )
        return lb_arn


def ensure_listener_forwarding_to_tg(elbv2, lb_arn, tg_arn):
    listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn).get('Listeners', [])
    http_listener = next((l for l in listeners if l.get('Port') == 80), None)

    if http_listener:
        elbv2.modify_listener(
            ListenerArn=http_listener['ListenerArn'],
            DefaultActions=[{'Type': 'forward', 'TargetGroupArn': tg_arn}]
        )
        return

    elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol='HTTP',
        Port=80,
        DefaultActions=[{'Type': 'forward', 'TargetGroupArn': tg_arn}]
    )


def setup_worker_infrastructure(region, cluster_name, service_name, table_name, bucket_name, execution_role_arn):

    if not execution_role_arn:
        raise ValueError(
            'execution_role_arn is required. Set EXECUTION_ROLE_ARN to an existing ECS task execution role ARN.'
        )

    # Clientes AWS
    ec2 = boto3.client('ec2', region_name=region)
    dynamodb = boto3.client('dynamodb', region_name=region)
    elbv2 = boto3.client('elbv2', region_name=region)
    ecs = boto3.client('ecs', region_name=region)
    logs = boto3.client('logs', region_name=region)
    autoscaling = boto3.client('application-autoscaling', region_name=region)
    vpc_id = 'vpc-0d006360e6cdf569d'
    subnets = ['subnet-00289e2de0daa7680', 'subnet-0a2121369f5ae05bd']
    alb_sg_id, task_sg_id = ensure_security_groups(ec2, vpc_id)

    # 0. Garantir que o cluster ECS exista
    cluster_check = ecs.describe_clusters(clusters=[cluster_name])
    clusters = cluster_check.get('clusters', [])
    if not clusters or clusters[0].get('status') == 'INACTIVE':
        ecs.create_cluster(clusterName=cluster_name)
        print(f"Cluster {cluster_name} criado.")
    else:
        print(f"Cluster {cluster_name} já existe.")

    # 0.1 Garantir que o CloudWatch Log Group exista
    log_group_name = '/ecs/routing-worker'
    try:
        logs.create_log_group(logGroupName=log_group_name)
        print(f"Log group {log_group_name} criado.")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"Log group {log_group_name} já existe.")

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

    # 2. Configurar Target Group + ALB + Listener
    tg_arn = get_or_create_target_group(elbv2, vpc_id)
    lb_arn = get_or_create_load_balancer(elbv2, ec2, subnets, vpc_id, alb_sg_id)
    ensure_listener_forwarding_to_tg(elbv2, lb_arn, tg_arn)

    # 3. Registrar a Task Definition
    task_def = ecs.register_task_definition(
        family='routing-worker-task',
        networkMode='awsvpc',
        requiresCompatibilities=['FARGATE'],
        cpu='1024',
        memory='4096',  # 4GB RAM
        executionRoleArn=execution_role_arn,
        taskRoleArn=execution_role_arn,
        containerDefinitions=[{
            'name': 'worker-container',
            'image': '821743068643.dkr.ecr.us-east-1.amazonaws.com/worker:latest',
            'portMappings': [{'containerPort': 80}],
            'environment': [
                {'name': 'MAPAS_BUCKET', 'value': bucket_name},
                {'name': 'TABLE_NAME', 'value': table_name}
            ],
            'logConfiguration': {
                'logDriver': 'awslogs',
                'options': {
                    'awslogs-group': log_group_name,
                    'awslogs-region': region,
                    'awslogs-stream-prefix': 'ecs'
                }
            }
        }]
    )
    task_arn = task_def['taskDefinition']['taskDefinitionArn']

    # 4. Criar ou atualizar o Serviço no ECS
    existing_services = ecs.describe_services(
        cluster=cluster_name,
        services=[service_name]
    ).get('services', [])

    if existing_services and existing_services[0].get('status') != 'INACTIVE':
        ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            taskDefinition=task_arn,
            desiredCount=2,
            forceNewDeployment=True
        )
        print(f"Serviço {service_name} atualizado.")
    else:
        ecs.create_service(
            cluster=cluster_name,
            serviceName=service_name,
            taskDefinition=task_arn,
            desiredCount=2,
            launchType='FARGATE',
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': subnets,
                    'securityGroups': [task_sg_id],
                    'assignPublicIp': 'ENABLED'
                }
            },
            loadBalancers=[{
                'targetGroupArn': tg_arn,
                'containerName': 'worker-container',
                'containerPort': 80
            }]
        )
        print(f"Serviço {service_name} criado.")

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