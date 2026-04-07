# Etapa de build ----------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim as builder
WORKDIR /app

# Copia o arquivo de dependências do worker
COPY services/worker/pyproject.toml ./pyproject.toml

# Desativa dependências de desenvolvimento
ENV UV_NO_DEV=1

# Cria a venv com as dependências do projeto
RUN uv sync

# Etapa de execução ------------------------------------------
FROM python:3.14-slim
WORKDIR /app

# Instala bibliotecas de sistema necessárias e limpa o cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copia a venv pronta da etapa de build
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copia o código do worker e o pacote shared (codigo Python)
COPY services/worker/app ./app
COPY shared/shared ./shared

# Define o PYTHONPATH para que os módulos em 'shared' sejam encontrados
ENV PYTHONPATH="/app"
EXPOSE 80

# Roda o servidor na porta 80 com mais de um worker para suportar carga e health check.
CMD ["fastapi", "run", "app/main.py", "--port", "80", "--workers", "2"]