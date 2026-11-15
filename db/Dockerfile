# Postgres com a base fictícia carregada na primeira inicialização.
# Estágio 1: o Python gera os CSVs. Estágio 2: o Postgres carrega schema, dados e papéis.
# O contexto do build é a raiz do repositório (veja o docker-compose.yml).

FROM python:3.12-slim AS seed
WORKDIR /build
COPY src/delivery_agent/seed.py .
RUN python seed.py --out /build/seed

FROM postgres:18-alpine
COPY --from=seed /build/seed/ /seed/
COPY db/init/ /docker-entrypoint-initdb.d/
