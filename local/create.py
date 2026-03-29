"""Módulo para extrair dados do OpenStreetMap e carregalos na AWS"""

import osmnx as ox
import pickle
import boto3


def fetch_and_store_graph(location_query: str, local_filename: str, s3_bucket: str):
    """
    Baixa grafo direcionado de ruas do OpenStreetMap, salva localmente
    e depois carrega para um bucket S3 da AWS no formato PKL
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
    # Teste com Alto de Pinheiros, São Paulo
    TARGET_LOCATION = "Alto de Pinheiros, São Paulo, Brazil"
    GRAPHML_FILE = "sp_altodepinheiros.pkl"
    BUCKET_NAME = "dijkfoods"  # TODO: Substituir pelo nome do bucket correto

    fetch_and_store_graph(TARGET_LOCATION, GRAPHML_FILE, BUCKET_NAME)
