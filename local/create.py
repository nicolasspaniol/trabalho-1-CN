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
