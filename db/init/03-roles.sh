#!/bin/sh
# Roles of the agent (security layers 1 and 2).
# - agent_reader reads only the tables without personal data and the customers_public view;
# - each of its transactions starts read-only, with a 5 s time limit;
# - set_config is revoked, so a query cannot turn these protections off;
# - review_writer only inserts the reviews that a person approved (extra EXT-01): no SELECT, UPDATE
#   or DELETE on any table, and not even the id of the review, which the database generates.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v agent_password="$AGENT_DB_PASSWORD" -v review_password="$REVIEW_DB_PASSWORD" \
    -v db_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE agent_reader LOGIN PASSWORD :'agent_password' CONNECTION LIMIT 20;
ALTER ROLE agent_reader SET default_transaction_read_only = on;
ALTER ROLE agent_reader SET statement_timeout = '5s';

CREATE ROLE review_writer LOGIN PASSWORD :'review_password' CONNECTION LIMIT 5;
ALTER ROLE review_writer SET statement_timeout = '5s';

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO agent_reader, review_writer;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agent_reader, review_writer;
GRANT SELECT ON restaurants, couriers, menu_items, orders, order_items, reviews, customers_public TO agent_reader;
GRANT INSERT (order_id, rating, comment, created_at) ON reviews TO review_writer;

REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
SQL
