-- Carrega os CSVs gerados no primeiro estágio da imagem (src/delivery_agent/seed.py).
-- Os CHECK do schema valem durante a carga: se um dado violar a integridade, a inicialização falha.

COPY restaurants FROM '/seed/restaurants.csv' WITH (FORMAT csv, HEADER true);
COPY customers FROM '/seed/customers.csv' WITH (FORMAT csv, HEADER true);
COPY couriers FROM '/seed/couriers.csv' WITH (FORMAT csv, HEADER true);
COPY menu_items FROM '/seed/menu_items.csv' WITH (FORMAT csv, HEADER true);
COPY orders FROM '/seed/orders.csv' WITH (FORMAT csv, HEADER true);
COPY order_items FROM '/seed/order_items.csv' WITH (FORMAT csv, HEADER true);
COPY reviews FROM '/seed/reviews.csv' WITH (FORMAT csv, HEADER true);

ANALYZE;
