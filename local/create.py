"""Módulo para extrair dados do OpenStreetMap e carregá-los na AWS"""

import osmnx as ox
import pickle
import boto3
import argparse


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


"Módulo de criação dos bancos RDS e DynamoDB"

import boto3
import time

dynamo = boto3.client('dynamodb', region_name='us-east-1')

#
def setup_dynamo():
    print("Criando tabela DynamoDB...")
    try:
        table = dynamo.create_table(
            TableName='Courier_Telemetry',
            KeySchema=[
                {'AttributeName': 'Order_ID', 'KeyType': 'HASH'},  # Partition Key
                {'AttributeName': 'Timestamp', 'KeyType': 'RANGE'} # Sort Key
            ],
            AttributeDefinitions=[
                {'AttributeName': 'Order_ID', 'AttributeType': 'S'},
                {'AttributeName': 'Timestamp', 'AttributeType': 'N'}
            ],
            ProvisionedThroughput={
                'ReadCapacityUnits': 10,
                'WriteCapacityUnits': 500 # Ajuste conforme a carga do trabalho
            }
        )
        # Waiter para garantir que a tabela existe antes de prosseguir
        dynamo.get_waiter('table_exists').wait(TableName='Courier_Telemetry')
        print("DynamoDB Ativo!")
    except dynamo.exceptions.ResourceInUseException:
        print("Tabela já existe.")


rds = boto3.client('rds', region_name='us-east-1')

def setup_rds():
    print("Instanciando RDS (isso pode levar alguns minutos)...")
    rds.create_db_instance(
        DBInstanceIdentifier='dijkfood-db',
        AllocatedStorage=20,
        DBInstanceClass='db.t3.micro',
        Engine='postgres', # ou 'mysql'
        MasterUsername='admin',
        MasterUserPassword='dijkfood',
        VpcSecurityGroupIds=['sg-xxxxxxxx'], # ID do seu Security Group
        PubliclyAccessible=True # Para você testar da sua máquina
    )
    
    # Waiter crucial: o script para aqui até o banco estar pronto
    waiter = rds.get_waiter('db_instance_available')
    waiter.wait(DBInstanceIdentifier='dijkfood-db')
    
    # Recupera o Endpoint (DNS) para conectar e rodar o SQL depois
    details = rds.describe_db_instances(DBInstanceIdentifier='dijkfood-db')
    endpoint = details['DBInstances'][0]['Endpoint']['Address']
    print(f"RDS Disponível em: {endpoint}")
    return endpoint

def cleanup_bancos():
    print("Iniciando destruição dos recursos...")
    
    # Deletar DynamoDB
    dynamo.delete_table(TableName='Courier_Telemetry')
    
    # Deletar RDS (Precisa desativar o snapshot final para ser rápido)
    rds.delete_db_instance(
        DBInstanceIdentifier='dijkfood-db',
        SkipFinalSnapshot=True
    )
    
    print("Aguardando exclusão completa...")
    # Waiters de exclusão
    dynamo.get_waiter('table_not_exists').wait(TableName='Courier_Telemetry')
    rds.get_waiter('db_instance_deleted').wait(DBInstanceIdentifier='dijkfood-db')
    print("Ambiente limpo com sucesso!")