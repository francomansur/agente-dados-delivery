#!/bin/sh
# Papel somente leitura do agente (camadas 1 e 2 de segurança).
# - agent_reader só lê as tabelas sem dados pessoais e a view customers_public;
# - toda transação dele começa somente leitura, com tempo máximo de 5 s;
# - set_config fica revogada, para uma consulta não conseguir desligar essas proteções.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v agent_password="$AGENT_DB_PASSWORD" -v db_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE agent_reader LOGIN PASSWORD :'agent_password' CONNECTION LIMIT 20;
ALTER ROLE agent_reader SET default_transaction_read_only = on;
ALTER ROLE agent_reader SET statement_timeout = '5s';

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO agent_reader;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agent_reader;
GRANT SELECT ON restaurants, couriers, menu_items, orders, order_items, reviews, customers_public TO agent_reader;

REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
SQL
