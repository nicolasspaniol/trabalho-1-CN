# Etapa de build ----------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim as builder
WORKDIR /app

# Copia apenas os arquivos de dependências
COPY services/worker/pyproject.toml services/worker/uv.lock ./

# Gera um requirements.txt limpo sem dependências de dev
RUN uv export --format requirements-txt --no-dev > requirements.txt

# Etapa de execução ------------------------------------------
FROM python:3.14-slim
WORKDIR /app

# Instala bibliotecas de sistema necessárias e limpa o cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copia o requirements.txt gerado no estágio de build
COPY --from=builder /app/requirements.txt .

# Instala as dependências diretamente no sistema do container
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código do worker e a pasta shared
COPY services/worker/app ./app
COPY shared ./shared

# Define o PYTHONPATH para que os módulos em 'shared' sejam encontrados
ENV PYTHONPATH="/app"
EXPOSE 80

# Roda o servidor na porta 80
CMD ["fastapi", "run", "app/main.py", "--port", "80"]