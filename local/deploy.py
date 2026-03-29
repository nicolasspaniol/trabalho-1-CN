"""Script Mestre de Deploy, Execução e Destruição"""

import subprocess
import time
import sys


##TODO: Funções de Infraestrutura do Deploy
# - Criar funções para criar recursos AWS (S3, RDS, DynamoDB, etc.)
# - Deploy dos Containers (Worker)
# - Destruição dos recursos criados


# EXPERIMENTO DE CARGA E SIMULAÇÃO
def run_simulation(bucket_name: str, alb_url: str):
    """Roda os scripts de carga e simulação de entregas para gerar dados reais de latência"""
    delivery_process = None
    try:
        # 1. Cria o grafo e joga pro S3 (subprocess.run bloqueia até terminar)
        print("1. Gerando mapa e subindo para o S3...")
        subprocess.run(
            ["uv", "run", "local/create.py", "--bucket", bucket_name], check=True
        )

        # 2. Popula o banco de dados via API
        print("\n2. Populando o banco de dados (RDS)...")
        subprocess.run(
            ["uv", "run", "local/load.py", "--api-url", alb_url, "--num-users", "100"],
            check=True,
        )

        # 3. Inicia os entregadores em BACKGROUND (Popen NÃO bloqueia o código)
        print("\n3. Iniciando a central de entregadores em background...")
        delivery_process = subprocess.Popen(
            ["uv", "run", "local/sim_delivery.py", "--api-url", alb_url]
        )

        # Dá um tempinho para os entregadores se conectarem
        time.sleep(3)

        # 4. Dispara o teste de carga (Cenário de Pico de Exemplo)
        print("\n4. Iniciando bombardeio de requisições (Load Test)...")
        # Aqui você pode fazer um loop para rodar os 3 cenários exigidos (10, 50, 200 RPS)
        for rps in [10, 50, 200]:
            print(f"\n--- Executando Cenário: {rps} RPS ---")
            subprocess.run(
                [
                    "uv",
                    "run",
                    "local/sim_client.py",
                    "--api-url",
                    alb_url,
                    "--rps",
                    str(rps),
                    "--duration",
                    "30",
                ],
                check=True,
            )
            time.sleep(
                5
            )  # Pausa dramática entre os cenários para o Auto Scaling respirar

    except subprocess.CalledProcessError as e:
        print(f"\n[ERRO CRÍTICO] A simulação falhou: {e}")
        sys.exit(1)
    finally:
        # 5. Mata o processo dos entregadores no final de tudo, independentemente de erro
        print("\n5. Encerrando o simulador de entregadores...")
        if delivery_process is not None:
            delivery_process.terminate()
            delivery_process.wait()


# MAIN
if __name__ == "__main__":
    bucket, api_url = "", ""
    try:
        # #TODO: Sobe tudo pra AWS (chama funções de criação de recursos, deploy dos containers, etc.)
        bucket, api_url = "", ""  # ( ... )

        # Simulação
        run_simulation(bucket, api_url)

    except KeyboardInterrupt:
        print("\nExecução interrompida pelo usuário.")
    finally:
        # TODO: Destruir tudo
        print("Experimento finalizado com sucesso!")
