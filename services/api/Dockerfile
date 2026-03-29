# Etapa de build ----------------------------------------------
# Essa imagem já vem com 'uv' instalado. Como esse programa só é necessário pra
# etapa de build, usamos uma imagem mais leve na hora da execução
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim as builder

WORKDIR /app

# Instala o PostgreSQL e o gcc
RUN apt-get update && \
    apt-get install -y libpq-dev gcc

# Copia a lista de dependências do projeto pro container
COPY pyproject.toml uv.lock ./

# Desativa as dependências de dev
ENV UV_NO_DEV=1

# Instala as dependências (dentro de um venv)
RUN uv sync --locked

# Etapa de execução ------------------------------------------
FROM python:3.14-slim

WORKDIR /app

# Instala o PostgreSQL e depois remove alguns arquivos desnecessários do apt
RUN apt-get update && \
    apt-get install -y libpq-dev && \
    rm -fr /var/lib/apt/lists/*

# Copia o venv da etapa anterior pra cá
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copia o código do servidor pro container
COPY main.py ./

EXPOSE 80

# Roda o servidor na porta 80
CMD ["fastapi", "run", "main.py", "--port", "80"]
