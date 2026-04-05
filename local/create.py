"""Módulos com as funções de criação dos bancos que serão utilizados na AWS"""

import osmnx as ox
import pickle
import boto3
import argparse
import os
import time
import psycopg2
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()


def require_db_password():
    db_password = os.getenv('DB_PASSWORD')
    if not db_password:
        raise ValueError("DB_PASSWORD nao definido. Defina a variavel de ambiente ou deixe deploy.py configurar automaticamente.")
    return db_password

def get_session():
    return boto3.Session(
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
        aws_session_token=os.getenv('AWS_SESSION_TOKEN'),
        region_name='us-east-1'
    )

session = get_session()

# SECURITY GROUP
def setup_security_group(name):
    ec2 = session.client('ec2')
    vpc_id = ec2.describe_vpcs()['Vpcs'][0]['VpcId']
    try:
        sg = ec2.create_security_group(GroupName=name, Description='Acesso DijkFood', VpcId=vpc_id)
        sg_id = sg['GroupId']
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpProtocol='tcp', FromPort=5432, ToPort=5432, CidrIp='0.0.0.0/0')
        return sg_id
    except ClientError as e:
        if 'InvalidGroup.Duplicate' in str(e):
            return ec2.describe_security_groups(GroupNames=[name])['SecurityGroups'][0]['GroupId']
        

"""Função para extrair dados do OpenStreetMap e carregá-los na AWS"""

def fetch_and_store_graph(location_query: str, local_filename: str, s3_bucket: str):
    """
    Baixa grafo direcionado de ruas do OpenStreetMap, salva localmente
    e depois carrega para um bucket S3 da AWS no formato PKL.
    """
    # Baixa grafo para ruas de "drive" (carros) para o local especificado
    print(f"Baixando dados da rede viária para: {location_query}...")
    G = ox.graph_from_place(location_query, network_type="drive")

    print(f"Salvando o grafo localmente como {local_filename} (Formato Pickle)...")

    # Escreve em formato binário usando pickle
    with open(local_filename, "wb") as f:
        pickle.dump(G, f)

    print(f"Fazendo upload de {local_filename} para o bucket S3: {s3_bucket}...")
    s3_client = boto3.client("s3")

    # Faz o upload do arquivo local para o bucket S3
    try:
        s3_client.upload_file(local_filename, s3_bucket, local_filename)
        print("Upload concluído com sucesso!")
    except Exception as e:
        print(f"Erro ao fazer o upload para o S3: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extração de Grafo Viário para S3")
    parser.add_argument("--bucket", required=True, help="Nome do bucket S3 de destino")
    parser.add_argument(
        "--location", default="Alto de Pinheiros, São Paulo, Brazil", help="Região alvo"
    )
    parser.add_argument(
        "--file", default="sp_altodepinheiros.pkl", help="Nome do arquivo local"
    )
    args = parser.parse_args()

    fetch_and_store_graph(args.location, args.file, args.bucket)


"""Módulo de criação dos bancos RDS, DynamoDB e S3"""

# cria o bucket pro mapa
def setup_s3_bucket(bucket_name):
    """Cria o bucket no S3 caso ele não exista."""
    s3 = session.client('s3')
    print(f"☁️ Verificando/Criando Bucket S3: {bucket_name}...")
    try:
        # No us-east-1, não se passa LocationConstraint
        s3.create_bucket(Bucket=bucket_name)
        print(f"✅ Bucket {bucket_name} criado.")
    except ClientError as e:
        if e.response['Error']['Code'] == 'BucketAlreadyOwnedByYou':
            print(f"ℹ️ Você já é dono do bucket {bucket_name}.")
        elif e.response['Error']['Code'] == 'BucketAlreadyExists':
            print(f"❌ Erro: O nome {bucket_name} já está em uso por outro usuário AWS.")
        else:
            print(f"❌ Erro inesperado no S3: {e}")
    return bucket_name

# dynamodb para os entregadores
def setup_dynamo(table_name):
    dynamo = session.client('dynamodb')
    print(f"⚡ Criando Tabela DynamoDB: {table_name}...")
    try:
        dynamo.create_table(
            TableName=table_name,
            KeySchema=[
                {'AttributeName': 'Order_ID', 'KeyType': 'HASH'},
                {'AttributeName': 'Timestamp', 'KeyType': 'RANGE'}
            ],
            AttributeDefinitions=[
                {'AttributeName': 'Order_ID', 'AttributeType': 'S'},
                {'AttributeName': 'Timestamp', 'AttributeType': 'N'}
            ],
            ProvisionedThroughput={'ReadCapacityUnits': 10, 'WriteCapacityUnits': 100}
        )
        dynamo.get_waiter('table_exists').wait(TableName=table_name)
        print("✅ DynamoDB Ativo!")
    except dynamo.exceptions.ResourceInUseException:
        print("ℹ️ Tabela DynamoDB já existe.")

# RDS
def setup_rds(db_instance_id, sg_id):
    """
    Cria a instância do RDS e aguarda até que ela esteja disponível.
    Retorna o Endpoint (host) do banco.
    """
    rds = session.client('rds')
    db_password = require_db_password()
    
    print(f"🐘 Provisionando instância RDS: {db_instance_id}...")
    try:
        rds.create_db_instance(
            DBInstanceIdentifier=db_instance_id,
            AllocatedStorage=20,
            DBInstanceClass='db.t3.micro',
            Engine='postgres',
            MasterUsername='postgres',
            MasterUserPassword=db_password,
            VpcSecurityGroupIds=[sg_id],
            PubliclyAccessible=True
        )
        print("⏳ Aguardando RDS ficar disponível (aprox. 10 min)...")
        # O programa "trava" aqui propositalmente até o banco ligar
        rds.get_waiter('db_instance_available').wait(DBInstanceIdentifier=db_instance_id)
        
        # Busca o endereço DNS gerado pela AWS
        desc = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        endpoint = desc['DBInstances'][0]['Endpoint']['Address']
        print(f"✅ RDS está online em: {endpoint}")
        return endpoint

    except rds.exceptions.DBInstanceAlreadyExistsFault:
        print(f"ℹ️ Instância {db_instance_id} já existe. Recuperando endpoint...")
        desc = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        return desc['DBInstances'][0]['Endpoint']['Address']
    except Exception as e:
        print(f"❌ Erro ao provisionar RDS: {e}")
        return None

# SQL
def load_schema_to_rds(db_endpoint, sql_file_path):
    """
    Conecta ao RDS via psycopg2 e executa o script SQL.
    """
    db_password = require_db_password()
    print(f"🛠️ Conectando ao RDS para carregar o schema: {sql_file_path}...")
    
    try:
        # Conexão com o banco padrão 'postgres'
        conn = psycopg2.connect(
            host=db_endpoint,
            database='postgres',
            user='postgres',
            password=db_password
        )
        cur = conn.cursor()
        
        print(f"📖 Lendo arquivo SQL...")
        with open(sql_file_path, 'r') as f:
            schema_sql = f.read()
            
        print("🚀 Executando comandos SQL no banco...")
        cur.execute(schema_sql)
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Tabelas e índices criados com sucesso!")
        return True
    except Exception as e:
        print(f"❌ Erro ao carregar schema no RDS: {e}")
        return False