-- Schema of the fictional dataset of a food delivery marketplace.
-- Identifiers are in English. The descriptions (COMMENT ON) are in Portuguese because the agent reads them.

CREATE TABLE restaurants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    city TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    base_delivery_fee NUMERIC(10, 2) NOT NULL CHECK (base_delivery_fee >= 0),
    signup_date DATE NOT NULL
);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL,
    street_address TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    city TEXT NOT NULL,
    signup_date DATE NOT NULL,
    is_subscriber BOOLEAN NOT NULL
);

CREATE TABLE couriers (
    id INTEGER PRIMARY KEY,
    city TEXT NOT NULL,
    vehicle TEXT NOT NULL CHECK (vehicle IN ('motorcycle', 'bicycle', 'car')),
    signup_date DATE NOT NULL
);

CREATE TABLE menu_items (
    id INTEGER PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants (id),
    name TEXT NOT NULL,
    item_type TEXT NOT NULL CHECK (item_type IN ('dish', 'drink', 'dessert')),
    price NUMERIC(10, 2) NOT NULL CHECK (price > 0)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    restaurant_id INTEGER NOT NULL REFERENCES restaurants (id),
    courier_id INTEGER REFERENCES couriers (id),
    created_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('delivered', 'canceled')),
    cancellation_reason TEXT CHECK (
        cancellation_reason IN (
            'restaurant_declined', 'customer_gave_up', 'no_courier', 'kitchen_delay', 'payment_declined'
        )
    ),
    payment_method TEXT NOT NULL CHECK (
        payment_method IN ('pix', 'credit_card', 'debit_card', 'meal_voucher', 'cash')
    ),
    subtotal NUMERIC(10, 2) NOT NULL CHECK (subtotal >= 0),
    delivery_fee NUMERIC(10, 2) NOT NULL CHECK (delivery_fee >= 0),
    discount NUMERIC(10, 2) NOT NULL CHECK (discount >= 0),
    total NUMERIC(10, 2) NOT NULL,
    distance_km NUMERIC(10, 2) NOT NULL CHECK (distance_km > 0),
    promised_minutes INTEGER NOT NULL CHECK (promised_minutes > 0),
    delivered_at TIMESTAMP,
    delivery_minutes INTEGER,
    CONSTRAINT orders_total_check CHECK (total = subtotal + delivery_fee - discount),
    CONSTRAINT orders_canceled_check CHECK (
        status <> 'canceled'
        OR (
            cancellation_reason IS NOT NULL
            AND courier_id IS NULL
            AND delivered_at IS NULL
            AND delivery_minutes IS NULL
        )
    ),
    CONSTRAINT orders_delivered_check CHECK (
        status <> 'delivered'
        OR (
            cancellation_reason IS NULL
            AND courier_id IS NOT NULL
            AND delivery_minutes > 0
            AND delivered_at = created_at + make_interval(mins => delivery_minutes)
        )
    )
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders (id),
    menu_item_id INTEGER NOT NULL REFERENCES menu_items (id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL CHECK (unit_price > 0)
);

CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders (id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX orders_restaurant_id_idx ON orders (restaurant_id);
CREATE INDEX orders_customer_id_idx ON orders (customer_id);
CREATE INDEX orders_created_at_idx ON orders (created_at);
CREATE INDEX order_items_order_id_idx ON order_items (order_id);
CREATE INDEX menu_items_restaurant_id_idx ON menu_items (restaurant_id);

-- What the agent sees of the customers: nothing that identifies a person.
CREATE VIEW customers_public AS
SELECT id, neighborhood, city, signup_date, is_subscriber
FROM customers;

COMMENT ON TABLE restaurants IS 'Restaurantes parceiros do marketplace (dados fictícios).';
COMMENT ON COLUMN restaurants.id IS 'Identificador do restaurante.';
COMMENT ON COLUMN restaurants.name IS 'Nome fantasia, único. Unidades da mesma rede trazem o bairro no nome, como ''Sushi Yama - Lourdes''.';
COMMENT ON COLUMN restaurants.category IS 'Tipo de cozinha, em português (por exemplo, ''Pizza'', ''Japonesa'' ou ''Hambúrguer'').';
COMMENT ON COLUMN restaurants.city IS 'Cidade do restaurante, que também é a cidade dos pedidos dele.';
COMMENT ON COLUMN restaurants.neighborhood IS 'Bairro do restaurante.';
COMMENT ON COLUMN restaurants.base_delivery_fee IS 'Taxa de entrega padrão do restaurante, em R$ (0 = entrega grátis).';
COMMENT ON COLUMN restaurants.signup_date IS 'Data de entrada do restaurante na plataforma.';

COMMENT ON TABLE customers IS 'Clientes, com dados pessoais fictícios. O agente não tem acesso a esta tabela: ele usa a view customers_public.';
COMMENT ON COLUMN customers.id IS 'Identificador do cliente.';
COMMENT ON COLUMN customers.full_name IS 'Nome completo (fictício).';
COMMENT ON COLUMN customers.email IS 'E-mail (fictício, no domínio example.com).';
COMMENT ON COLUMN customers.phone IS 'Telefone (fictício).';
COMMENT ON COLUMN customers.street_address IS 'Rua e número (fictícios).';
COMMENT ON COLUMN customers.neighborhood IS 'Bairro do cliente.';
COMMENT ON COLUMN customers.city IS 'Cidade do cliente.';
COMMENT ON COLUMN customers.signup_date IS 'Data de cadastro do cliente.';
COMMENT ON COLUMN customers.is_subscriber IS 'true se o cliente assina o clube de vantagens, que dá frete grátis em pedidos com subtotal a partir de R$ 30.';

COMMENT ON VIEW customers_public IS 'Clientes sem dados pessoais. orders.customer_id aponta para customers_public.id.';
COMMENT ON COLUMN customers_public.id IS 'Identificador do cliente.';
COMMENT ON COLUMN customers_public.neighborhood IS 'Bairro do cliente.';
COMMENT ON COLUMN customers_public.city IS 'Cidade do cliente, que é sempre a mesma do restaurante nos pedidos dele.';
COMMENT ON COLUMN customers_public.signup_date IS 'Data de cadastro do cliente.';
COMMENT ON COLUMN customers_public.is_subscriber IS 'true se o cliente assina o clube de vantagens, que dá frete grátis em pedidos com subtotal a partir de R$ 30.';

COMMENT ON TABLE couriers IS 'Entregadores, sem dados pessoais.';
COMMENT ON COLUMN couriers.id IS 'Identificador do entregador.';
COMMENT ON COLUMN couriers.city IS 'Cidade onde o entregador trabalha.';
COMMENT ON COLUMN couriers.vehicle IS 'Veículo usado nas entregas: motorcycle (moto), bicycle (bicicleta) ou car (carro).';
COMMENT ON COLUMN couriers.signup_date IS 'Data de cadastro do entregador.';

COMMENT ON TABLE menu_items IS 'Itens dos cardápios. O mesmo nome (por exemplo, ''Refrigerante lata'') aparece em vários restaurantes.';
COMMENT ON COLUMN menu_items.id IS 'Identificador do item.';
COMMENT ON COLUMN menu_items.restaurant_id IS 'Restaurante que vende o item (restaurants.id).';
COMMENT ON COLUMN menu_items.name IS 'Nome do item no cardápio.';
COMMENT ON COLUMN menu_items.item_type IS 'Tipo do item: dish (prato), drink (bebida) ou dessert (sobremesa).';
COMMENT ON COLUMN menu_items.price IS 'Preço atual no cardápio, em R$.';

COMMENT ON TABLE orders IS 'Pedidos feitos na plataforma de janeiro a outubro de 2026, inclusive os cancelados.';
COMMENT ON COLUMN orders.id IS 'Identificador do pedido.';
COMMENT ON COLUMN orders.customer_id IS 'Cliente que fez o pedido (customers_public.id).';
COMMENT ON COLUMN orders.restaurant_id IS 'Restaurante do pedido (restaurants.id).';
COMMENT ON COLUMN orders.courier_id IS 'Entregador do pedido (couriers.id). Vazio quando o pedido foi cancelado.';
COMMENT ON COLUMN orders.created_at IS 'Data e hora do pedido, no horário local.';
COMMENT ON COLUMN orders.status IS 'Situação do pedido: delivered (entregue) ou canceled (cancelado).';
COMMENT ON COLUMN orders.cancellation_reason IS 'Motivo do cancelamento, preenchido só quando status = ''canceled'': restaurant_declined (o restaurante recusou), customer_gave_up (o cliente desistiu), no_courier (faltou entregador), kitchen_delay (atraso no preparo) ou payment_declined (pagamento recusado).';
COMMENT ON COLUMN orders.payment_method IS 'Forma de pagamento: pix, credit_card (cartão de crédito), debit_card (cartão de débito), meal_voucher (vale-refeição) ou cash (dinheiro).';
COMMENT ON COLUMN orders.subtotal IS 'Soma dos itens (quantity x unit_price), em R$.';
COMMENT ON COLUMN orders.delivery_fee IS 'Taxa de entrega cobrada do cliente, em R$ (0 = frete grátis).';
COMMENT ON COLUMN orders.discount IS 'Desconto de cupom, em R$ (0 = sem cupom).';
COMMENT ON COLUMN orders.total IS 'Valor pago pelo cliente, em R$: subtotal + delivery_fee - discount.';
COMMENT ON COLUMN orders.distance_km IS 'Distância entre o restaurante e o cliente, em km.';
COMMENT ON COLUMN orders.promised_minutes IS 'Prazo de entrega prometido ao cliente, em minutos a partir de created_at.';
COMMENT ON COLUMN orders.delivered_at IS 'Data e hora da entrega. Vazio quando o pedido foi cancelado.';
COMMENT ON COLUMN orders.delivery_minutes IS 'Minutos entre created_at e delivered_at. Vazio quando o pedido foi cancelado.';

COMMENT ON TABLE order_items IS 'Itens de cada pedido.';
COMMENT ON COLUMN order_items.id IS 'Identificador da linha do pedido.';
COMMENT ON COLUMN order_items.order_id IS 'Pedido (orders.id).';
COMMENT ON COLUMN order_items.menu_item_id IS 'Item do cardápio (menu_items.id).';
COMMENT ON COLUMN order_items.quantity IS 'Quantidade pedida.';
COMMENT ON COLUMN order_items.unit_price IS 'Preço unitário cobrado na data do pedido, em R$. Pode ser diferente do preço atual (menu_items.price).';

COMMENT ON TABLE reviews IS 'Avaliações dos pedidos entregues. Cada pedido tem no máximo uma avaliação.';
COMMENT ON COLUMN reviews.id IS 'Identificador da avaliação.';
COMMENT ON COLUMN reviews.order_id IS 'Pedido avaliado (orders.id).';
COMMENT ON COLUMN reviews.rating IS 'Nota de 1 a 5 estrelas.';
COMMENT ON COLUMN reviews.comment IS 'Comentário opcional do cliente.';
COMMENT ON COLUMN reviews.created_at IS 'Data e hora da avaliação.';
