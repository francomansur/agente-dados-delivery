"""Generate a fictional but realistic dataset of a food delivery marketplace.

Nothing here comes from a real company. The constants below are the recipe of a random draw:
how many restaurants each city has, how popular each cuisine is at lunch or at dinner, menu
prices, delivery fees, how long deliveries take, how often orders are canceled, and so on.
Following that recipe, the draw creates restaurants, customers, couriers, menus, about 28
thousand orders and their reviews.

The flow is: this generator, then one CSV file per table, then Postgres loads the files the
first time its container starts (see db/Dockerfile and db/init/).

The seed is fixed, so the dataset is always the same, row by row, and the expected answers of
the evaluation stay stable. The module only uses the standard library because it also runs in
the first stage of the Postgres image build.

Usage:
    python -m delivery_agent.seed --out data/seed
    python seed.py --out /seed --seed 7
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import random
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_SEED = 42
START = date(2026, 1, 1)
END = date(2026, 10, 31)

Value = str | int | float | bool | None
Row = tuple[Value, ...]

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "restaurants": ("id", "name", "category", "city", "neighborhood", "base_delivery_fee", "signup_date"),
    "customers": (
        "id",
        "full_name",
        "email",
        "phone",
        "street_address",
        "neighborhood",
        "city",
        "signup_date",
        "is_subscriber",
    ),
    "couriers": ("id", "city", "vehicle", "signup_date"),
    "menu_items": ("id", "restaurant_id", "name", "item_type", "price"),
    "orders": (
        "id",
        "customer_id",
        "restaurant_id",
        "courier_id",
        "created_at",
        "status",
        "cancellation_reason",
        "payment_method",
        "subtotal",
        "delivery_fee",
        "discount",
        "total",
        "distance_km",
        "promised_minutes",
        "delivered_at",
        "delivery_minutes",
    ),
    "order_items": ("id", "order_id", "menu_item_id", "quantity", "unit_price"),
    "reviews": ("id", "order_id", "rating", "comment", "created_at"),
}


@dataclass(frozen=True, kw_only=True)
class City:
    """Parameters that shape the orders of one city."""

    demand: float  # share of all orders
    restaurants: int  # how many restaurants the city has
    price_factor: float  # multiplies menu prices
    traffic_factor: float  # multiplies travel time
    extra_cancellation: float  # added to the chance that an order is canceled
    neighborhoods: tuple[str, ...]
    area_code: str  # phone area code, used only for the fictional customer data


CITIES: dict[str, City] = {
    "São Paulo": City(
        demand=0.34,
        restaurants=48,
        price_factor=1.15,
        traffic_factor=1.25,
        extra_cancellation=0.0,
        neighborhoods=(
            "Pinheiros",
            "Moema",
            "Vila Mariana",
            "Tatuapé",
            "Santana",
            "Itaim Bibi",
            "Perdizes",
            "Butantã",
        ),
        area_code="11",
    ),
    "Rio de Janeiro": City(
        demand=0.2,
        restaurants=30,
        price_factor=1.12,
        traffic_factor=1.2,
        extra_cancellation=0.008,
        neighborhoods=("Copacabana", "Botafogo", "Tijuca", "Barra da Tijuca", "Leblon", "Méier"),
        area_code="21",
    ),
    "Belo Horizonte": City(
        demand=0.12,
        restaurants=18,
        price_factor=1.0,
        traffic_factor=1.05,
        extra_cancellation=0.0,
        neighborhoods=("Savassi", "Funcionários", "Pampulha", "Buritis", "Lourdes"),
        area_code="31",
    ),
    "Curitiba": City(
        demand=0.1,
        restaurants=15,
        price_factor=1.0,
        traffic_factor=1.0,
        extra_cancellation=0.0,
        neighborhoods=("Batel", "Água Verde", "Centro", "Bigorrilho", "Portão"),
        area_code="41",
    ),
    "Porto Alegre": City(
        demand=0.08,
        restaurants=12,
        price_factor=0.98,
        traffic_factor=1.0,
        extra_cancellation=0.0,
        neighborhoods=("Moinhos de Vento", "Cidade Baixa", "Menino Deus", "Petrópolis"),
        area_code="51",
    ),
    "Recife": City(
        demand=0.09,
        restaurants=13,
        price_factor=0.92,
        traffic_factor=1.1,
        extra_cancellation=0.012,
        neighborhoods=("Boa Viagem", "Casa Forte", "Espinheiro", "Graças"),
        area_code="81",
    ),
    "Uberaba": City(
        demand=0.07,
        restaurants=10,
        price_factor=0.85,
        traffic_factor=0.85,
        extra_cancellation=0.0,
        neighborhoods=("Centro", "Mercês", "Fabrício", "Santa Maria"),
        area_code="34",
    ),
}


@dataclass(frozen=True, kw_only=True)
class Category:
    """A cuisine: its menu, its restaurant names and when people order it."""

    name: str
    restaurant_share: float  # share of the restaurants that serve this cuisine
    prep_minutes: float  # average preparation time
    shift_demand: tuple[float, float, float]  # relative demand at (lunch, afternoon, dinner)
    mains: tuple[tuple[str, float], ...]  # (name, base price); the first one is the flagship
    drinks: tuple[tuple[str, float], ...]
    desserts: tuple[tuple[str, float], ...]
    restaurant_names: tuple[str, ...]
    main_item_type: str = "dish"  # dish, or dessert for sweets and açaí


CATEGORIES: tuple[Category, ...] = (
    Category(
        name="Pizza",
        restaurant_share=0.16,
        prep_minutes=28,
        shift_demand=(0.4, 0.5, 1.7),  # lunch, afternoon, dinner
        mains=(
            ("Pizza Calabresa", 50),
            ("Pizza Margherita", 52),
            ("Pizza Quatro Queijos", 58),
            ("Pizza Portuguesa", 56),
            ("Pizza Frango com Catupiry", 57),
            ("Pizza Pepperoni", 62),
        ),
        drinks=(("Refrigerante 2L", 14), ("Refrigerante lata", 6.5), ("Água mineral", 4)),
        desserts=(("Pizza de Chocolate", 46), ("Brownie", 12)),
        restaurant_names=(
            "Pizzaria Bella Napoli",
            "Forno da Vila",
            "Pizza Nostra",
            "Cantinho da Pizza",
            "Pizzaria Dom Vito",
            "La Bufala",
            "Pizzaria Primo",
            "Massa Fina Pizzas",
        ),
    ),
    Category(
        name="Hambúrguer",
        restaurant_share=0.15,
        prep_minutes=18,
        shift_demand=(0.8, 0.8, 1.4),  # lunch, afternoon, dinner
        mains=(
            ("Smash Burger Duplo", 36),
            ("X-Bacon", 32),
            ("X-Burguer", 28),
            ("Cheeseburger Clássico", 30),
            ("Burger Vegano", 34),
            ("Batata Frita Média", 16),
            ("Onion Rings", 18),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Milkshake de Chocolate", 19), ("Água mineral", 4)),
        desserts=(("Brownie", 12), ("Petit Gâteau", 18)),
        restaurant_names=(
            "Burger do Zé",
            "Smash Point",
            "Brasa Burger",
            "Chapa Quente",
            "Burger Lab",
            "Hamburgueria 1987",
            "Big Bull Burger",
            "Rancho Burger",
        ),
    ),
    Category(
        name="Japonesa",
        restaurant_share=0.12,
        prep_minutes=30,
        shift_demand=(0.8, 0.5, 1.4),  # lunch, afternoon, dinner
        mains=(
            ("Combinado 20 peças", 68),
            ("Temaki de Salmão", 32),
            ("Hot Roll 10 peças", 34),
            ("Yakisoba Tradicional", 42),
            ("Sashimi de Salmão", 58),
            ("Uramaki Filadélfia", 30),
            ("Poke de Salmão", 44),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Chá verde gelado", 9), ("Água mineral", 4)),
        desserts=(("Harumaki de Banana", 16), ("Tempurá de Sorvete", 22)),
        restaurant_names=(
            "Sushi Kenzo",
            "Temakeria Hana",
            "Sakura Sushi",
            "Nikkei House",
            "Kaizen Sushi",
            "Tokyo Express",
            "Sushi Yama",
            "Hiro Sushi Bar",
        ),
    ),
    Category(
        name="Brasileira",
        restaurant_share=0.14,
        prep_minutes=22,
        shift_demand=(2.0, 0.6, 0.6),  # lunch, afternoon, dinner
        mains=(
            ("Marmita Executiva", 26),
            ("Feijoada Completa", 39),
            ("Picanha com Fritas", 62),
            ("Frango Grelhado com Salada", 31),
            ("Parmegiana de Carne", 45),
            ("Strogonoff de Frango", 34),
            ("Baião de Dois", 36),
        ),
        drinks=(("Suco natural", 9), ("Refrigerante lata", 6.5), ("Guaraná 2L", 12), ("Água mineral", 4)),
        desserts=(("Pudim de Leite", 12), ("Mousse de Maracujá", 11)),
        restaurant_names=(
            "Sabor de Minas",
            "Cozinha da Vó",
            "Tempero Caseiro",
            "Panela de Barro",
            "Bom Prato Caseiro",
            "Fogão a Lenha",
            "Casa do Feijão",
            "Quintal Nordestino",
        ),
    ),
    Category(
        name="Italiana",
        restaurant_share=0.08,
        prep_minutes=25,
        shift_demand=(1.1, 0.5, 1.1),  # lunch, afternoon, dinner
        mains=(
            ("Lasanha à Bolonhesa", 46),
            ("Espaguete ao Sugo", 38),
            ("Nhoque ao Molho Branco", 42),
            ("Risoto de Cogumelos", 54),
            ("Fettuccine Alfredo", 48),
            ("Ravioli de Ricota", 50),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Suco de uva integral", 10), ("Água mineral", 4)),
        desserts=(("Tiramisù", 19), ("Panna Cotta", 16)),
        restaurant_names=(
            "Cantina Nonna Rosa",
            "Trattoria Bellini",
            "Casa di Pasta",
            "Osteria Toscana",
            "Massas da Mamma",
            "Piccola Italia",
        ),
    ),
    Category(
        name="Árabe",
        restaurant_share=0.06,
        prep_minutes=20,
        shift_demand=(1.0, 1.0, 1.0),  # lunch, afternoon, dinner
        mains=(
            ("Esfiha de Carne", 6),
            ("Esfiha de Queijo", 6),
            ("Kibe Frito", 7),
            ("Shawarma de Frango", 29),
            ("Beirute de Rosbife", 38),
            ("Prato Árabe Completo", 49),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Suco natural", 9), ("Água mineral", 4)),
        desserts=(("Baklava", 14),),
        restaurant_names=(
            "Empório Árabe",
            "Esfiharia Damasco",
            "Sultan Shawarma",
            "Casa Libanesa",
            "Cedro do Líbano",
        ),
    ),
    Category(
        name="Saudável",
        restaurant_share=0.07,
        prep_minutes=15,
        shift_demand=(1.6, 0.9, 0.6),  # lunch, afternoon, dinner
        mains=(
            ("Bowl de Quinoa", 38),
            ("Salada Caesar com Frango", 34),
            ("Wrap Integral de Frango", 29),
            ("Poke Vegano", 36),
            ("Frango com Batata-Doce", 32),
        ),
        drinks=(("Suco verde", 14), ("Água de coco", 9), ("Kombucha", 16)),
        desserts=(("Salada de Frutas", 12),),
        restaurant_names=("Green Bowl", "Vida Leve", "Fit Kitchen", "Salad Point", "Natural Mente"),
    ),
    Category(
        name="Doces",
        restaurant_share=0.05,
        prep_minutes=12,
        shift_demand=(0.5, 2.5, 0.9),  # lunch, afternoon, dinner
        mains=(
            ("Bolo de Pote", 15),
            ("Brigadeiro Gourmet (6 un)", 24),
            ("Torta de Limão (fatia)", 16),
            ("Cookie Recheado", 10),
            ("Cheesecake de Frutas Vermelhas", 19),
        ),
        drinks=(("Café expresso", 7), ("Chocolate quente", 12)),
        desserts=(),
        restaurant_names=(
            "Doce Encanto",
            "Confeitaria Flor de Açúcar",
            "Brigaderia da Ana",
            "Ateliê do Bolo",
            "Cacau & Cia",
        ),
        main_item_type="dessert",
    ),
    Category(
        name="Açaí",
        restaurant_share=0.06,
        prep_minutes=8,
        shift_demand=(0.6, 2.8, 0.8),  # lunch, afternoon, dinner
        mains=(
            ("Açaí 500ml", 24),
            ("Açaí 300ml", 18),
            ("Açaí 700ml", 30),
            ("Cupuaçu 500ml", 24),
            ("Tigela de Açaí com Frutas", 28),
        ),
        drinks=(("Água mineral", 4), ("Suco natural", 9)),
        desserts=(),
        restaurant_names=("Açaí da Praia", "Point do Açaí", "Açaí Tropical", "Amazônia Açaí", "Açaí Mania"),
        main_item_type="dessert",
    ),
    Category(
        name="Lanches",
        restaurant_share=0.07,
        prep_minutes=14,
        shift_demand=(0.9, 2.0, 0.8),  # lunch, afternoon, dinner
        mains=(
            ("Coxinha (6 un)", 18),
            ("Pastel de Carne", 12),
            ("Pastel de Queijo", 11),
            ("Misto Quente", 14),
            ("Cachorro-Quente Completo", 19),
            ("Pão de Queijo (10 un)", 16),
        ),
        drinks=(("Caldo de cana", 9), ("Refrigerante lata", 6.5), ("Café com leite", 7)),
        desserts=(("Churros", 9),),
        restaurant_names=(
            "Lanchonete do Bairro",
            "Pastelaria Imperial",
            "Cantina da Esquina",
            "Lanches Express",
            "Cachorrão do Tio",
        ),
    ),
    Category(
        name="Chinesa",
        restaurant_share=0.04,
        prep_minutes=22,
        shift_demand=(1.0, 0.6, 1.2),  # lunch, afternoon, dinner
        mains=(
            ("Yakisoba de Frango", 39),
            ("Frango Xadrez", 42),
            ("Rolinho Primavera (4 un)", 16),
            ("Arroz Chop Suey", 34),
            ("Carne com Brócolis", 44),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Chá gelado", 8), ("Água mineral", 4)),
        desserts=(("Banana Caramelada", 14),),
        restaurant_names=(
            "Dragão Dourado",
            "China Wok",
            "Jardim de Pequim",
            "Muralha Chinesa",
            "Lótus Oriental",
        ),
    ),
    Category(
        name="Mexicana",
        restaurant_share=0.03,
        prep_minutes=20,
        shift_demand=(0.7, 0.6, 1.4),  # lunch, afternoon, dinner
        mains=(
            ("Burrito de Carne", 36),
            ("Tacos (3 un)", 32),
            ("Nachos com Guacamole", 34),
            ("Quesadilla de Frango", 31),
            ("Chili com Carne", 38),
        ),
        drinks=(("Refrigerante lata", 6.5), ("Limonada", 10), ("Água mineral", 4)),
        desserts=(("Churros com Doce de Leite", 15),),
        restaurant_names=("Taco Loco", "Cantina Mexicana", "El Sombrero", "Mi Tierra Tex-Mex", "La Calaca"),
    ),
)

# Standard delivery fee of a restaurant, in R$, and the chance of each value. The chances add up to 1.
DELIVERY_FEE_ODDS = (
    (0.0, 0.12),
    (4.99, 0.15),
    (5.99, 0.20),
    (6.99, 0.20),
    (7.99, 0.15),
    (8.99, 0.10),
    (9.99, 0.08),
)
# Vehicle of a courier and the chance of each one. The chances add up to 1.
VEHICLE_ODDS = (("motorcycle", 0.68), ("bicycle", 0.22), ("car", 0.10))

WEEKDAY_FACTOR = (0.82, 0.84, 0.90, 0.96, 1.18, 1.32, 1.24)  # Monday to Sunday
SPECIAL_DATES = {
    date(2026, 2, 14): 1.15,  # Carnival
    date(2026, 2, 15): 1.15,
    date(2026, 2, 16): 1.15,
    date(2026, 2, 17): 1.15,
    date(2026, 5, 10): 1.30,  # Mother's Day
    date(2026, 6, 12): 1.45,  # Brazilian Valentine's Day
}
SPEED_KMH = {"motorcycle": 24.0, "bicycle": 14.0, "car": 19.0}

REVIEW_COMMENTS: dict[int, tuple[str, ...]] = {
    5: (
        "Chegou quentinho e bem embalado!",
        "Perfeito, como sempre.",
        "Entrega rápida, recomendo.",
        "Comida deliciosa!",
        "Melhor pedido da semana.",
    ),
    4: (
        "Muito bom, só demorou um pouco.",
        "Gostei, mas a porção podia ser maior.",
        "Bom custo-benefício.",
        "Tudo certo com o pedido.",
    ),
    3: ("Ok, nada demais.", "Chegou meio frio.", "Sabor bom, embalagem fraca.", "Esperava mais."),
    2: ("Chegou frio.", "Veio faltando item.", "Embalagem chegou vazando.", "Não era o que eu esperava."),
    1: (
        "Péssima experiência.",
        "Pedido chegou errado.",
        "Comida revirada na embalagem.",
        "Entregador não encontrou meu endereço.",
    ),
}
LATE_COMMENTS = (
    "Demorou muito mais que o prazo.",
    "Atrasou quase uma hora.",
    "Prazo estourado de novo.",
    "Chegou frio de tanto que demorou.",
)

FIRST_NAMES = (
    "Ana", "Beatriz", "Bruno", "Camila", "Daniel", "Diego", "Eduarda", "Felipe", "Gabriela", "Gustavo",
    "Helena", "Igor", "João", "Juliana", "Larissa", "Letícia", "Lucas", "Marcos", "Mariana", "Matheus",
    "Natália", "Otávio", "Patrícia", "Pedro", "Rafael", "Renata", "Sofia", "Thiago", "Valentina", "Vinícius",
)  # fmt: skip
SURNAMES = (
    "Alves", "Andrade", "Barbosa", "Carvalho", "Costa", "Dias", "Fernandes", "Ferreira", "Freitas", "Gomes",
    "Lima", "Lopes", "Machado", "Marques", "Martins", "Mendes", "Moreira", "Nascimento", "Nunes", "Oliveira",
    "Pereira", "Ribeiro", "Rocha", "Rodrigues", "Santos", "Silva", "Soares", "Souza", "Vieira", "Almeida",
)  # fmt: skip
STREETS = (
    "Rua das Flores", "Rua dos Ipês", "Rua São José", "Rua Sete de Setembro", "Rua das Palmeiras",
    "Rua Tiradentes", "Rua XV de Novembro", "Rua Dom Pedro II", "Avenida Getúlio Vargas", "Rua Santos Dumont",
    "Rua da Paz", "Rua das Acácias", "Travessa Esperança", "Avenida Brasil", "Rua do Comércio",
)  # fmt: skip


@dataclass
class MenuItem:
    """A dish, drink or dessert sold by one restaurant."""

    id: int
    name: str
    item_type: str  # dish, drink or dessert
    role: str  # main, drink or dessert: how the item enters an order
    weight: float  # popularity within the menu
    price: float  # current price
    old_price: float  # price before the restaurant's price change


@dataclass
class Restaurant:
    """A restaurant and the hidden traits that shape its orders."""

    id: int
    name: str
    category: Category
    city: str
    neighborhood: str
    base_fee: float
    signup: date
    popularity: float
    quality: float
    efficiency: float  # multiplies preparation time (higher is slower)
    price_change: date | None
    items: list[MenuItem] = field(default_factory=list)


@dataclass
class Customer:
    """A customer and how often they order."""

    id: int
    city: str
    signup: date
    is_subscriber: bool
    activity: float


@dataclass
class Courier:
    """A courier who delivers in one city."""

    id: int
    city: str
    vehicle: str
    signup: date


def _round_half_up(value: float) -> int:
    """Round half up (Python's round() rounds half to even)."""
    return math.floor(value + 0.5)


def _menu_price(value: float) -> float:
    """Turn a raw price into a menu-like one: 52.90 instead of 53.17."""
    return max(round(_round_half_up(value) - 0.10, 2), 0.90)


def _draw[T](rng: random.Random, odds: tuple[tuple[T, float], ...]) -> T:
    """Draw one value from (value, chance) pairs."""
    return rng.choices([value for value, _ in odds], weights=[chance for _, chance in odds])[0]


def _random_date(rng: random.Random, first: date, last: date) -> date:
    return first + timedelta(days=rng.randint(0, (last - first).days))


def _weighted_sample(rng: random.Random, items: list[MenuItem], k: int) -> list[MenuItem]:
    """Draw up to k distinct items, weighted by popularity."""
    remaining = list(items)
    chosen = []

    for _ in range(min(k, len(remaining))):
        item = rng.choices(remaining, weights=[i.weight for i in remaining])[0]
        remaining.remove(item)
        chosen.append(item)

    return chosen


def _generate_restaurants(rng: random.Random) -> list[Restaurant]:
    shares = [category.restaurant_share for category in CATEGORIES]
    free_names = {c.name: rng.sample(c.restaurant_names, len(c.restaurant_names)) for c in CATEGORIES}
    used_names: set[str] = set()
    restaurants: list[Restaurant] = []
    next_item_id = 1

    for city_name, city in CITIES.items():
        for _ in range(city.restaurants):
            category = rng.choices(CATEGORIES, weights=shares)[0]
            neighborhood = rng.choice(city.neighborhoods)
            available = free_names[category.name]

            if available:
                name = available.pop()
            else:  # a chain with more than one unit: the neighborhood tells them apart
                name = f"{rng.choice(category.restaurant_names)} - {neighborhood}"

                while name in used_names:
                    neighborhood = rng.choice(city.neighborhoods)
                    name = f"{rng.choice(category.restaurant_names)} - {neighborhood}"

            used_names.add(name)

            if rng.random() < 0.88:
                signup = _random_date(rng, date(2019, 3, 1), date(2025, 12, 31))
            else:
                signup = _random_date(rng, date(2026, 1, 15), date(2026, 9, 30))

            price_change = (
                _random_date(rng, date(2026, 3, 1), date(2026, 9, 30)) if rng.random() < 0.6 else None
            )
            price_change_factor = rng.uniform(1.04, 1.10)
            price_factor = city.price_factor * rng.lognormvariate(0, 0.12)

            # The keyword arguments run in order, and that order is part of the random sequence.
            restaurant = Restaurant(
                id=len(restaurants) + 1,
                name=name,
                category=category,
                city=city_name,
                neighborhood=neighborhood,
                base_fee=_draw(rng, DELIVERY_FEE_ODDS),
                signup=signup,
                popularity=rng.lognormvariate(0, 0.75),
                quality=rng.gauss(0, 0.35),
                efficiency=max(0.7, rng.gauss(1.0, 0.15)),
                price_change=price_change,
            )
            mains = list(category.mains)

            # Each restaurant drops one or two dishes from the base menu, never the flagship.
            for _ in range(rng.randint(0, 2)):
                if len(mains) > 4:
                    mains.pop(rng.randrange(1, len(mains)))

            options = [(item_name, price, "main", category.main_item_type) for item_name, price in mains]
            options += [(item_name, price, "drink", "drink") for item_name, price in category.drinks]
            options += [
                (item_name, price, "dessert", "dessert")
                for item_name, price in category.desserts
                if rng.random() < 0.7
            ]

            for position, (item_name, base_price, role, item_type) in enumerate(options):
                price = _menu_price(base_price * price_factor)
                old_price = _menu_price(price / price_change_factor) if price_change else price
                weight = 1.8 if position == 0 else rng.uniform(0.6, 1.2)
                restaurant.items.append(
                    MenuItem(next_item_id, item_name, item_type, role, weight, price, old_price)
                )
                next_item_id += 1

            restaurants.append(restaurant)

    return restaurants


def _generate_customers(rng: random.Random, count: int = 4200) -> list[Customer]:
    city_names = list(CITIES)
    city_weights = [city.demand for city in CITIES.values()]
    customers = []

    for customer_id in range(1, count + 1):
        if rng.random() < 0.72:
            signup = _random_date(rng, date(2021, 1, 1), date(2025, 12, 31))
        else:
            signup = _random_date(rng, date(2026, 1, 1), date(2026, 10, 25))

        is_subscriber = rng.random() < 0.22
        activity = rng.lognormvariate(0, 0.9) * (1.8 if is_subscriber else 1.0)
        city = rng.choices(city_names, weights=city_weights)[0]
        customers.append(Customer(customer_id, city, signup, is_subscriber, activity))

    return customers


def _generate_couriers(rng: random.Random, count: int = 460) -> list[Courier]:
    city_names = list(CITIES)
    city_weights = [city.demand for city in CITIES.values()]
    couriers = []

    for courier_id in range(1, count + 1):
        if rng.random() < 0.85:
            signup = _random_date(rng, date(2021, 1, 1), date(2025, 12, 31))
        else:
            signup = _random_date(rng, date(2026, 1, 1), date(2026, 9, 30))

        vehicle = _draw(rng, VEHICLE_ODDS)
        city = rng.choices(city_names, weights=city_weights)[0]
        couriers.append(Courier(courier_id, city, vehicle, signup))

    return couriers


class _CustomerPicker:
    """Pick customers who had already signed up on a given day, weighted by activity."""

    def __init__(self, customers: list[Customer]) -> None:
        self.customers = sorted(customers, key=lambda c: (c.signup, c.id))
        self.signups = [c.signup for c in self.customers]
        self.cumulative: list[float] = []
        total = 0.0

        for customer in self.customers:
            total += customer.activity
            self.cumulative.append(total)

    def pick(self, rng: random.Random, day: date) -> Customer:
        eligible = bisect.bisect_right(self.signups, day)
        target = rng.random() * self.cumulative[eligible - 1]

        return self.customers[bisect.bisect_left(self.cumulative, target, 0, eligible)]


def _random_minute(rng: random.Random) -> int:
    draw = rng.random()

    if draw < 0.36:
        minute = rng.gauss(12.6 * 60, 50)  # lunch
    elif draw < 0.44:
        minute = rng.uniform(15 * 60, 18 * 60)  # afternoon
    elif draw < 0.95:
        minute = rng.gauss(20.1 * 60, 75)  # dinner
    else:
        minute = rng.uniform(22 * 60, 23 * 60 + 59)  # late night

    return int(min(max(minute, 10 * 60 + 30), 23 * 60 + 59))


def _shift(minute: int) -> int:
    if minute < 15 * 60:
        return 0  # lunch

    if minute < 18 * 60:
        return 1  # afternoon

    return 2  # dinner


def _slug(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _customer_rows(customers: list[Customer], seed: int) -> list[Row]:
    """Build the customer rows, with fictional personal data from a separate random generator.

    The separate generator keeps the main random sequence, and so every other table, unchanged.
    """
    rng = random.Random(f"{seed}:customers")
    rows: list[Row] = []

    for customer in customers:
        first_name = rng.choice(FIRST_NAMES)
        surnames = rng.sample(SURNAMES, 2)
        city = CITIES[customer.city]
        rows.append(
            (
                customer.id,
                f"{first_name} {' '.join(surnames)}",
                f"{_slug(first_name)}.{_slug(surnames[-1])}.{customer.id}@example.com",
                f"({city.area_code}) 90000-{customer.id:04d}",
                f"{rng.choice(STREETS)}, {rng.randint(10, 2999)}",
                rng.choice(city.neighborhoods),
                customer.city,
                customer.signup.isoformat(),
                customer.is_subscriber,
            )
        )

    return rows


def generate(seed: int = DEFAULT_SEED) -> dict[str, list[Row]]:
    """Generate every table of the synthetic dataset.

    Args:
        seed: Seed of the random generator. The same seed always produces the same rows.

    Returns:
        The rows of each table, keyed by table name, in the column order of ``TABLE_COLUMNS``.
    """
    rng = random.Random(seed)
    restaurants = _generate_restaurants(rng)
    customers = _generate_customers(rng)
    couriers = _generate_couriers(rng)

    customers_by_city = {name: _CustomerPicker([c for c in customers if c.city == name]) for name in CITIES}
    restaurants_by_city = {name: [r for r in restaurants if r.city == name] for name in CITIES}
    couriers_by_city = {name: [c for c in couriers if c.city == name] for name in CITIES}
    city_names = list(CITIES)
    city_weights = [city.demand for city in CITIES.values()]

    orders: list[Row] = []
    order_items: list[Row] = []
    reviews: list[Row] = []
    ordered_before: set[int] = set()
    day = START

    while day <= END:
        days_elapsed = (day - START).days
        mean = 72 * (1 + 0.045 * days_elapsed / 30.4)  # about 4.5% growth per month
        mean *= WEEKDAY_FACTOR[day.weekday()] * SPECIAL_DATES.get(day, 1.0)
        count = max(0, _round_half_up(rng.gauss(mean, math.sqrt(mean))))
        moments = sorted(_random_minute(rng) * 60 + rng.randint(0, 59) for _ in range(count))

        for seconds in moments:
            minute = seconds // 60
            shift = _shift(minute)
            peak = 19 * 60 <= minute < 21 * 60 + 30
            created_at = datetime.combine(day, datetime.min.time()) + timedelta(seconds=seconds)

            city_name = rng.choices(city_names, weights=city_weights)[0]
            city = CITIES[city_name]
            open_restaurants = [r for r in restaurants_by_city[city_name] if r.signup <= day]
            restaurant = rng.choices(
                open_restaurants,
                weights=[r.popularity * r.category.shift_demand[shift] for r in open_restaurants],
            )[0]
            customer = customers_by_city[city_name].pick(rng, day)
            changed = restaurant.price_change is not None and day < restaurant.price_change

            # Order lines.
            mains = [i for i in restaurant.items if i.role == "main"]
            drinks = [i for i in restaurant.items if i.role == "drink"]
            desserts = [i for i in restaurant.items if i.role == "dessert"]
            distinct = rng.choices([1, 2, 3, 4], weights=[0.70, 0.22, 0.06, 0.02])[0]
            lines: list[tuple[MenuItem, int, float]] = []

            for item in _weighted_sample(rng, mains, distinct):
                price = item.old_price if changed else item.price

                if price < 10:
                    quantity = rng.randint(2, 6)
                else:
                    quantity = rng.choices([1, 2, 3], weights=[0.84, 0.13, 0.03])[0]

                lines.append((item, quantity, price))

            if drinks and rng.random() < 0.45:
                item = rng.choice(drinks)
                price = item.old_price if changed else item.price
                lines.append((item, rng.choices([1, 2], weights=[0.8, 0.2])[0], price))

            if desserts and rng.random() < 0.14:
                item = rng.choice(desserts)
                price = item.old_price if changed else item.price
                lines.append((item, 1, price))

            subtotal = round(sum(quantity * price for _, quantity, price in lines), 2)

            # Charged amounts.
            distance = round(min(max(rng.lognormvariate(math.log(2.7), 0.55), 0.3), 14.0), 2)
            first_order = customer.id not in ordered_before
            ordered_before.add(customer.id)
            discount = 0.0

            if rng.random() < (0.45 if first_order else 0.12):
                discount = round(min(subtotal * rng.uniform(0.10, 0.25), 25.0), 2)

            if restaurant.base_fee == 0 or (customer.is_subscriber and subtotal >= 30):
                fee = 0.0
            else:
                fee = restaurant.base_fee + (1.0 if distance > 5 else 0.0) + (1.5 if distance > 8 else 0.0)

            total = round(subtotal + fee - discount, 2)
            weekday_lunch = shift == 0 and day.weekday() < 5
            payment = rng.choices(
                ["pix", "credit_card", "debit_card", "meal_voucher", "cash"],
                weights=[0.33, 0.38, 0.10, 0.26 if weekday_lunch else 0.04, 0.05],
            )[0]

            # Cancellation or delivery.
            promised = 5 * _round_half_up(
                (restaurant.category.prep_minutes * 1.15 + distance / 22 * 60 * city.traffic_factor + 12) / 5
            )
            cancel_chance = (
                0.04
                + (0.025 if peak else 0.0)
                + 0.06 * max(0.0, restaurant.efficiency - 1.0)
                + city.extra_cancellation
                + (0.015 if payment == "cash" else 0.0)
            )
            order_id = len(orders) + 1

            if rng.random() < cancel_chance:
                card = payment in ("credit_card", "debit_card")
                reason = rng.choices(
                    [
                        "restaurant_declined",
                        "customer_gave_up",
                        "no_courier",
                        "kitchen_delay",
                        "payment_declined",
                    ],
                    weights=[
                        0.30,
                        0.25,
                        0.30 if peak else 0.12,
                        0.17 * restaurant.efficiency,
                        0.10 if card else 0.0,
                    ],
                )[0]
                orders.append(
                    (
                        order_id,
                        customer.id,
                        restaurant.id,
                        None,
                        created_at.isoformat(sep=" "),
                        "canceled",
                        reason,
                        payment,
                        subtotal,
                        round(fee, 2),
                        discount,
                        total,
                        distance,
                        promised,
                        None,
                        None,
                    )
                )
            else:
                category = restaurant.category
                peak_factor = 1.28 if peak else (1.12 if 12 * 60 <= minute < 13 * 60 + 30 else 1.0)
                prep = (
                    category.prep_minutes * restaurant.efficiency * peak_factor * rng.lognormvariate(0, 0.18)
                )
                wait = rng.expovariate(1 / (6.0 if peak else 3.5))
                candidates = [
                    c
                    for c in couriers_by_city[city_name]
                    if c.signup <= day and (distance <= 4.0 or c.vehicle != "bicycle")
                ]
                courier = rng.choice(candidates)
                traffic = city.traffic_factor * (1.15 if peak else 1.0)
                travel = distance / SPEED_KMH[courier.vehicle] * 60 * traffic * rng.lognormvariate(0, 0.15)
                minutes = max(8, _round_half_up(prep + wait + travel))
                delivered_at = created_at + timedelta(minutes=minutes)
                orders.append(
                    (
                        order_id,
                        customer.id,
                        restaurant.id,
                        courier.id,
                        created_at.isoformat(sep=" "),
                        "delivered",
                        None,
                        payment,
                        subtotal,
                        round(fee, 2),
                        discount,
                        total,
                        distance,
                        promised,
                        delivered_at.isoformat(sep=" "),
                        minutes,
                    )
                )
                delay = minutes - promised

                if rng.random() < (0.55 if delay > 10 else 0.40):
                    expected_rating = 4.55 + restaurant.quality

                    if delay > 0:
                        expected_rating -= min(2.2, 0.3 + 0.06 * delay)

                    rating = int(min(5, max(1, _round_half_up(rng.gauss(expected_rating, 0.65)))))
                    comment = None

                    if rng.random() < 0.35:
                        if delay > 15 and rating <= 3 and rng.random() < 0.6:
                            comment = rng.choice(LATE_COMMENTS)
                        else:
                            comment = rng.choice(REVIEW_COMMENTS[rating])

                    reviewed_at = delivered_at + timedelta(minutes=rng.randint(10, 2880))
                    reviews.append(
                        (len(reviews) + 1, order_id, rating, comment, reviewed_at.isoformat(sep=" "))
                    )

            for item, quantity, price in lines:
                order_items.append((len(order_items) + 1, order_id, item.id, quantity, price))

        day += timedelta(days=1)

    return {
        "restaurants": [
            (r.id, r.name, r.category.name, r.city, r.neighborhood, r.base_fee, r.signup.isoformat())
            for r in restaurants
        ],
        "customers": _customer_rows(customers, seed),
        "couriers": [(c.id, c.city, c.vehicle, c.signup.isoformat()) for c in couriers],
        "menu_items": [(i.id, r.id, i.name, i.item_type, i.price) for r in restaurants for i in r.items],
        "orders": orders,
        "order_items": order_items,
        "reviews": reviews,
    }


def _csv_value(value: Value) -> str:
    if value is None:
        return ""  # an unquoted empty field is NULL for COPY ... CSV

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, float):
        return f"{value:.2f}"

    return str(value)


def write_csv(dataset: dict[str, list[Row]], out_dir: Path) -> None:
    """Write one CSV file per table, with a header, ready for ``COPY ... WITH (FORMAT csv, HEADER)``.

    Args:
        dataset: Rows of each table, as returned by ``generate``.
        out_dir: Folder for the files. It is created if needed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    for table, columns in TABLE_COLUMNS.items():
        with (out_dir / f"{table}.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows([_csv_value(value) for value in row] for row in dataset[table])


def main(argv: list[str] | None = None) -> None:
    """Generate the dataset and write the CSV files.

    Args:
        argv: Command line arguments; ``None`` reads them from ``sys.argv``.
    """
    parser = argparse.ArgumentParser(description="Gera os CSVs da base fictícia do marketplace de delivery.")
    parser.add_argument("--out", type=Path, required=True, help="pasta de saída dos arquivos CSV")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="semente do gerador aleatório")
    args = parser.parse_args(argv)

    dataset = generate(args.seed)
    write_csv(dataset, args.out)

    for table, rows in dataset.items():
        print(f"{table:<12} {len(rows):>7} linhas")

    print(f"CSVs gravados em {args.out}")


if __name__ == "__main__":
    main()
