# Postgres with the fictional dataset, loaded on the first start.
# Stage 1: Python generates the CSV files. Stage 2: Postgres loads the schema, the data and the roles.
# The build context is the repository root (see docker-compose.yml).

FROM python:3.12-slim AS seed
WORKDIR /build
COPY src/delivery_agent/seed.py .
RUN python seed.py --out /build/seed

FROM postgres:18-alpine
COPY --from=seed /build/seed/ /seed/
COPY db/init/ /docker-entrypoint-initdb.d/
