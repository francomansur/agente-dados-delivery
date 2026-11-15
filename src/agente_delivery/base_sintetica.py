"""Gera a base SQLite fictícia de um marketplace de delivery.

Os dados são sintéticos e determinísticos: a mesma semente gera sempre a
mesma base. Assim, as respostas esperadas da avaliação não mudam de uma
execução para outra.

Uso:
    python -m agente_delivery.base_sintetica                 # gera data/delivery.db
    python -m agente_delivery.base_sintetica --semente 7 --saida /tmp/outra.db
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import CAMINHO_BANCO

SEMENTE_PADRAO = 42
INICIO = date(2026, 1, 1)
FIM = date(2026, 10, 31)

# Os comentários ficam guardados no sqlite_master e viram a documentação
# que o descrever_schema entrega ao modelo.
DDL = """
CREATE TABLE restaurantes (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,                 -- nome fantasia (único)
    categoria TEXT NOT NULL,            -- tipo de cozinha
    cidade TEXT NOT NULL,
    bairro TEXT NOT NULL,
    taxa_entrega_base REAL NOT NULL,    -- taxa padrão do restaurante, em R$ (0 = entrega grátis)
    data_cadastro TEXT NOT NULL         -- entrada na plataforma (AAAA-MM-DD)
);

CREATE TABLE clientes (
    id INTEGER PRIMARY KEY,
    cidade TEXT NOT NULL,
    data_cadastro TEXT NOT NULL,        -- AAAA-MM-DD
    assinante INTEGER NOT NULL          -- 1 = assinante do clube (frete grátis com subtotal a partir de R$ 30); 0 = não assinante
);

CREATE TABLE entregadores (
    id INTEGER PRIMARY KEY,
    cidade TEXT NOT NULL,
    modal TEXT NOT NULL,                -- veículo usado nas entregas
    data_cadastro TEXT NOT NULL         -- AAAA-MM-DD
);

CREATE TABLE itens_cardapio (
    id INTEGER PRIMARY KEY,
    restaurante_id INTEGER NOT NULL REFERENCES restaurantes(id),
    nome TEXT NOT NULL,                 -- o mesmo nome (ex.: Refrigerante lata) aparece em vários restaurantes
    tipo TEXT NOT NULL,                 -- prato, bebida ou sobremesa
    preco REAL NOT NULL                 -- preço atual no cardápio, em R$
);

CREATE TABLE pedidos (
    id INTEGER PRIMARY KEY,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    restaurante_id INTEGER NOT NULL REFERENCES restaurantes(id),
    entregador_id INTEGER REFERENCES entregadores(id),  -- NULL quando o pedido foi cancelado
    criado_em TEXT NOT NULL,            -- data e hora do pedido (AAAA-MM-DD HH:MM:SS, horário local)
    status TEXT NOT NULL,               -- entregue ou cancelado
    motivo_cancelamento TEXT,           -- preenchido só quando status = 'cancelado'
    forma_pagamento TEXT NOT NULL,
    subtotal REAL NOT NULL,             -- soma dos itens (quantidade x preco_unitario), em R$
    taxa_entrega REAL NOT NULL,         -- taxa cobrada do cliente, em R$ (0 = frete grátis)
    desconto REAL NOT NULL,             -- desconto de cupom, em R$ (0 = sem cupom)
    total REAL NOT NULL,                -- valor pago pelo cliente: subtotal + taxa_entrega - desconto
    distancia_km REAL NOT NULL,         -- distância entre restaurante e cliente
    previsao_entrega_min INTEGER NOT NULL, -- prazo prometido ao cliente, em minutos desde criado_em
    entregue_em TEXT,                   -- data e hora da entrega; NULL se cancelado
    tempo_entrega_min INTEGER           -- minutos entre criado_em e entregue_em; NULL se cancelado
);

CREATE TABLE itens_pedido (
    id INTEGER PRIMARY KEY,
    pedido_id INTEGER NOT NULL REFERENCES pedidos(id),
    item_id INTEGER NOT NULL REFERENCES itens_cardapio(id),
    quantidade INTEGER NOT NULL,
    preco_unitario REAL NOT NULL        -- preço cobrado na data do pedido, em R$ (pode diferir do preço atual)
);

CREATE TABLE avaliacoes (
    id INTEGER PRIMARY KEY,
    pedido_id INTEGER NOT NULL UNIQUE REFERENCES pedidos(id),
    nota INTEGER NOT NULL,              -- 1 a 5 estrelas
    comentario TEXT,                    -- opcional
    criada_em TEXT NOT NULL             -- AAAA-MM-DD HH:MM:SS
);

CREATE INDEX idx_pedidos_restaurante ON pedidos(restaurante_id);
CREATE INDEX idx_pedidos_cliente ON pedidos(cliente_id);
CREATE INDEX idx_pedidos_criado_em ON pedidos(criado_em);
CREATE INDEX idx_itens_pedido_pedido ON itens_pedido(pedido_id);
CREATE INDEX idx_itens_cardapio_restaurante ON itens_cardapio(restaurante_id);
"""


@dataclass(frozen=True)
class Cidade:
    demanda: float  # participação nos pedidos
    restaurantes: int
    preco: float  # fator de preço do cardápio
    transito: float  # fator no tempo de deslocamento
    cancelamento_extra: float
    bairros: tuple[str, ...]


CIDADES: dict[str, Cidade] = {
    "São Paulo": Cidade(0.34, 48, 1.15, 1.25, 0.0, ("Pinheiros", "Moema", "Vila Mariana", "Tatuapé", "Santana", "Itaim Bibi", "Perdizes", "Butantã")),
    "Rio de Janeiro": Cidade(0.20, 30, 1.12, 1.20, 0.008, ("Copacabana", "Botafogo", "Tijuca", "Barra da Tijuca", "Leblon", "Méier")),
    "Belo Horizonte": Cidade(0.12, 18, 1.00, 1.05, 0.0, ("Savassi", "Funcionários", "Pampulha", "Buritis", "Lourdes")),
    "Curitiba": Cidade(0.10, 15, 1.00, 1.00, 0.0, ("Batel", "Água Verde", "Centro", "Bigorrilho", "Portão")),
    "Porto Alegre": Cidade(0.08, 12, 0.98, 1.00, 0.0, ("Moinhos de Vento", "Cidade Baixa", "Menino Deus", "Petrópolis")),
    "Recife": Cidade(0.09, 13, 0.92, 1.10, 0.012, ("Boa Viagem", "Casa Forte", "Espinheiro", "Graças")),
    "Uberaba": Cidade(0.07, 10, 0.85, 0.85, 0.0, ("Centro", "Mercês", "Fabrício", "Santa Maria")),
}


@dataclass(frozen=True)
class Categoria:
    nome: str
    peso: float  # participação entre os restaurantes
    preparo_min: float  # tempo médio de preparo
    turnos: tuple[float, float, float]  # demanda relativa no almoço, tarde e jantar
    principais: tuple[tuple[str, float], ...]  # (nome, preço base); o primeiro é o carro-chefe
    bebidas: tuple[tuple[str, float], ...]
    sobremesas: tuple[tuple[str, float], ...]
    nomes: tuple[str, ...]
    tipo_principal: str = "prato"


CATEGORIAS: tuple[Categoria, ...] = (
    Categoria(
        "Pizza", 0.16, 28, (0.4, 0.5, 1.7),
        (("Pizza Calabresa", 50), ("Pizza Margherita", 52), ("Pizza Quatro Queijos", 58),
         ("Pizza Portuguesa", 56), ("Pizza Frango com Catupiry", 57), ("Pizza Pepperoni", 62)),
        (("Refrigerante 2L", 14), ("Refrigerante lata", 6.5), ("Água mineral", 4)),
        (("Pizza de Chocolate", 46), ("Brownie", 12)),
        ("Pizzaria Bella Napoli", "Forno da Vila", "Pizza Nostra", "Cantinho da Pizza",
         "Pizzaria Dom Vito", "La Bufala", "Pizzaria Primo", "Massa Fina Pizzas"),
    ),
    Categoria(
        "Hambúrguer", 0.15, 18, (0.8, 0.8, 1.4),
        (("Smash Burger Duplo", 36), ("X-Bacon", 32), ("X-Burguer", 28), ("Cheeseburger Clássico", 30),
         ("Burger Vegano", 34), ("Batata Frita Média", 16), ("Onion Rings", 18)),
        (("Refrigerante lata", 6.5), ("Milkshake de Chocolate", 19), ("Água mineral", 4)),
        (("Brownie", 12), ("Petit Gâteau", 18)),
        ("Burger do Zé", "Smash Point", "Brasa Burger", "Chapa Quente", "Burger Lab",
         "Hamburgueria 1987", "Big Bull Burger", "Rancho Burger"),
    ),
    Categoria(
        "Japonesa", 0.12, 30, (0.8, 0.5, 1.4),
        (("Combinado 20 peças", 68), ("Temaki de Salmão", 32), ("Hot Roll 10 peças", 34),
         ("Yakisoba Tradicional", 42), ("Sashimi de Salmão", 58), ("Uramaki Filadélfia", 30),
         ("Poke de Salmão", 44)),
        (("Refrigerante lata", 6.5), ("Chá verde gelado", 9), ("Água mineral", 4)),
        (("Harumaki de Banana", 16), ("Tempurá de Sorvete", 22)),
        ("Sushi Kenzo", "Temakeria Hana", "Sakura Sushi", "Nikkei House", "Kaizen Sushi",
         "Tokyo Express", "Sushi Yama", "Hiro Sushi Bar"),
    ),
    Categoria(
        "Brasileira", 0.14, 22, (2.0, 0.6, 0.6),
        (("Marmita Executiva", 26), ("Feijoada Completa", 39), ("Picanha com Fritas", 62),
         ("Frango Grelhado com Salada", 31), ("Parmegiana de Carne", 45), ("Strogonoff de Frango", 34),
         ("Baião de Dois", 36)),
        (("Suco natural", 9), ("Refrigerante lata", 6.5), ("Guaraná 2L", 12), ("Água mineral", 4)),
        (("Pudim de Leite", 12), ("Mousse de Maracujá", 11)),
        ("Sabor de Minas", "Cozinha da Vó", "Tempero Caseiro", "Panela de Barro", "Bom Prato Caseiro",
         "Fogão a Lenha", "Casa do Feijão", "Quintal Nordestino"),
    ),
    Categoria(
        "Italiana", 0.08, 25, (1.1, 0.5, 1.1),
        (("Lasanha à Bolonhesa", 46), ("Espaguete ao Sugo", 38), ("Nhoque ao Molho Branco", 42),
         ("Risoto de Cogumelos", 54), ("Fettuccine Alfredo", 48), ("Ravioli de Ricota", 50)),
        (("Refrigerante lata", 6.5), ("Suco de uva integral", 10), ("Água mineral", 4)),
        (("Tiramisù", 19), ("Panna Cotta", 16)),
        ("Cantina Nonna Rosa", "Trattoria Bellini", "Casa di Pasta", "Osteria Toscana",
         "Massas da Mamma", "Piccola Italia"),
    ),
    Categoria(
        "Árabe", 0.06, 20, (1.0, 1.0, 1.0),
        (("Esfiha de Carne", 6), ("Esfiha de Queijo", 6), ("Kibe Frito", 7), ("Shawarma de Frango", 29),
         ("Beirute de Rosbife", 38), ("Prato Árabe Completo", 49)),
        (("Refrigerante lata", 6.5), ("Suco natural", 9), ("Água mineral", 4)),
        (("Baklava", 14),),
        ("Empório Árabe", "Esfiharia Damasco", "Sultan Shawarma", "Casa Libanesa", "Cedro do Líbano"),
    ),
    Categoria(
        "Saudável", 0.07, 15, (1.6, 0.9, 0.6),
        (("Bowl de Quinoa", 38), ("Salada Caesar com Frango", 34), ("Wrap Integral de Frango", 29),
         ("Poke Vegano", 36), ("Frango com Batata-Doce", 32)),
        (("Suco verde", 14), ("Água de coco", 9), ("Kombucha", 16)),
        (("Salada de Frutas", 12),),
        ("Green Bowl", "Vida Leve", "Fit Kitchen", "Salad Point", "Natural Mente"),
    ),
    Categoria(
        "Doces", 0.05, 12, (0.5, 2.5, 0.9),
        (("Bolo de Pote", 15), ("Brigadeiro Gourmet (6 un)", 24), ("Torta de Limão (fatia)", 16),
         ("Cookie Recheado", 10), ("Cheesecake de Frutas Vermelhas", 19)),
        (("Café expresso", 7), ("Chocolate quente", 12)),
        (),
        ("Doce Encanto", "Confeitaria Flor de Açúcar", "Brigaderia da Ana", "Ateliê do Bolo", "Cacau & Cia"),
        tipo_principal="sobremesa",
    ),
    Categoria(
        "Açaí", 0.06, 8, (0.6, 2.8, 0.8),
        (("Açaí 500ml", 24), ("Açaí 300ml", 18), ("Açaí 700ml", 30), ("Cupuaçu 500ml", 24),
         ("Tigela de Açaí com Frutas", 28)),
        (("Água mineral", 4), ("Suco natural", 9)),
        (),
        ("Açaí da Praia", "Point do Açaí", "Açaí Tropical", "Amazônia Açaí", "Açaí Mania"),
        tipo_principal="sobremesa",
    ),
    Categoria(
        "Lanches", 0.07, 14, (0.9, 2.0, 0.8),
        (("Coxinha (6 un)", 18), ("Pastel de Carne", 12), ("Pastel de Queijo", 11), ("Misto Quente", 14),
         ("Cachorro-Quente Completo", 19), ("Pão de Queijo (10 un)", 16)),
        (("Caldo de cana", 9), ("Refrigerante lata", 6.5), ("Café com leite", 7)),
        (("Churros", 9),),
        ("Lanchonete do Bairro", "Pastelaria Imperial", "Cantina da Esquina", "Lanches Express",
         "Cachorrão do Tio"),
    ),
    Categoria(
        "Chinesa", 0.04, 22, (1.0, 0.6, 1.2),
        (("Yakisoba de Frango", 39), ("Frango Xadrez", 42), ("Rolinho Primavera (4 un)", 16),
         ("Arroz Chop Suey", 34), ("Carne com Brócolis", 44)),
        (("Refrigerante lata", 6.5), ("Chá gelado", 8), ("Água mineral", 4)),
        (("Banana Caramelada", 14),),
        ("Dragão Dourado", "China Wok", "Jardim de Pequim", "Muralha Chinesa", "Lótus Oriental"),
    ),
    Categoria(
        "Mexicana", 0.03, 20, (0.7, 0.6, 1.4),
        (("Burrito de Carne", 36), ("Tacos (3 un)", 32), ("Nachos com Guacamole", 34),
         ("Quesadilla de Frango", 31), ("Chili com Carne", 38)),
        (("Refrigerante lata", 6.5), ("Limonada", 10), ("Água mineral", 4)),
        (("Churros com Doce de Leite", 15),),
        ("Taco Loco", "Cantina Mexicana", "El Sombrero", "Mi Tierra Tex-Mex", "La Calaca"),
    ),
)

FATOR_DIA_SEMANA = (0.82, 0.84, 0.90, 0.96, 1.18, 1.32, 1.24)  # segunda a domingo
DATAS_ESPECIAIS = {
    date(2026, 2, 14): 1.15, date(2026, 2, 15): 1.15, date(2026, 2, 16): 1.15, date(2026, 2, 17): 1.15,  # Carnaval
    date(2026, 5, 10): 1.30,  # Dia das Mães
    date(2026, 6, 12): 1.45,  # Dia dos Namorados
}
VELOCIDADE_KMH = {"moto": 24.0, "bicicleta": 14.0, "carro": 19.0}

COMENTARIOS = {
    5: ("Chegou quentinho e bem embalado!", "Perfeito, como sempre.", "Entrega rápida, recomendo.",
        "Comida deliciosa!", "Melhor pedido da semana."),
    4: ("Muito bom, só demorou um pouco.", "Gostei, mas a porção podia ser maior.", "Bom custo-benefício.",
        "Tudo certo com o pedido."),
    3: ("Ok, nada demais.", "Chegou meio frio.", "Sabor bom, embalagem fraca.", "Esperava mais."),
    2: ("Chegou frio.", "Veio faltando item.", "Embalagem chegou vazando.", "Não era o que eu esperava."),
    1: ("Péssima experiência.", "Pedido chegou errado.", "Comida revirada na embalagem.",
        "Entregador não encontrou meu endereço."),
}
COMENTARIOS_ATRASO = ("Demorou muito mais que o prazo.", "Atrasou quase uma hora.", "Prazo estourado de novo.",
                      "Chegou frio de tanto que demorou.")


@dataclass
class ItemCardapio:
    id: int
    nome: str
    tipo: str
    papel: str  # principal, bebida ou sobremesa
    peso: float  # popularidade dentro do cardápio
    preco: float  # preço atual
    preco_antigo: float  # preço antes do reajuste


@dataclass
class Restaurante:
    id: int
    nome: str
    categoria: Categoria
    cidade: str
    bairro: str
    taxa_base: float
    cadastro: date
    popularidade: float
    qualidade: float
    eficiencia: float  # multiplica o tempo de preparo (maior = mais lento)
    reajuste: date | None
    itens: list[ItemCardapio] = field(default_factory=list)


@dataclass
class Cliente:
    id: int
    cidade: str
    cadastro: date
    assinante: bool
    atividade: float


@dataclass
class Entregador:
    id: int
    cidade: str
    modal: str
    cadastro: date


def _arredondar(valor: float) -> int:
    """Arredonda meio para cima (o round() do Python arredonda para o par)."""
    return math.floor(valor + 0.5)


def _preco(valor: float) -> float:
    """Preço com cara de cardápio: 52,90 em vez de 53,17."""
    return max(round(_arredondar(valor) - 0.10, 2), 0.90)


def _data_aleatoria(rng: random.Random, inicio: date, fim: date) -> date:
    return inicio + timedelta(days=rng.randint(0, (fim - inicio).days))


def _sortear_sem_reposicao(rng: random.Random, itens: list[ItemCardapio], k: int) -> list[ItemCardapio]:
    restantes = list(itens)
    escolhidos = []
    for _ in range(min(k, len(restantes))):
        item = rng.choices(restantes, weights=[i.peso for i in restantes])[0]
        restantes.remove(item)
        escolhidos.append(item)
    return escolhidos


def _gerar_restaurantes(rng: random.Random) -> list[Restaurante]:
    pesos = [c.peso for c in CATEGORIAS]
    nomes_livres = {c.nome: rng.sample(c.nomes, len(c.nomes)) for c in CATEGORIAS}
    usados: set[str] = set()
    restaurantes: list[Restaurante] = []
    proximo_item = 1
    for cidade, info in CIDADES.items():
        for _ in range(info.restaurantes):
            categoria = rng.choices(CATEGORIAS, weights=pesos)[0]
            bairro = rng.choice(info.bairros)
            livres = nomes_livres[categoria.nome]
            if livres:
                nome = livres.pop()
            else:  # rede com mais de uma unidade: diferencia pelo bairro
                nome = f"{rng.choice(categoria.nomes)} - {bairro}"
                while nome in usados:
                    bairro = rng.choice(info.bairros)
                    nome = f"{rng.choice(categoria.nomes)} - {bairro}"
            usados.add(nome)

            if rng.random() < 0.88:
                cadastro = _data_aleatoria(rng, date(2019, 3, 1), date(2025, 12, 31))
            else:
                cadastro = _data_aleatoria(rng, date(2026, 1, 15), date(2026, 9, 30))
            reajuste = _data_aleatoria(rng, date(2026, 3, 1), date(2026, 9, 30)) if rng.random() < 0.6 else None
            fator_reajuste = rng.uniform(1.04, 1.10)
            fator_preco = info.preco * rng.lognormvariate(0, 0.12)

            restaurante = Restaurante(
                id=len(restaurantes) + 1,
                nome=nome,
                categoria=categoria,
                cidade=cidade,
                bairro=bairro,
                taxa_base=rng.choices([0.0, 4.99, 5.99, 6.99, 7.99, 8.99, 9.99],
                                      weights=[0.12, 0.15, 0.20, 0.20, 0.15, 0.10, 0.08])[0],
                cadastro=cadastro,
                popularidade=rng.lognormvariate(0, 0.75),
                qualidade=rng.gauss(0, 0.35),
                eficiencia=max(0.7, rng.gauss(1.0, 0.15)),
                reajuste=reajuste,
            )
            principais = list(categoria.principais)
            # Cada restaurante tira um ou dois pratos do cardápio-base (nunca o carro-chefe).
            for _ in range(rng.randint(0, 2)):
                if len(principais) > 4:
                    principais.pop(rng.randrange(1, len(principais)))
            opcoes = [(n, p, "principal", categoria.tipo_principal) for n, p in principais]
            opcoes += [(n, p, "bebida", "bebida") for n, p in categoria.bebidas]
            opcoes += [(n, p, "sobremesa", "sobremesa") for n, p in categoria.sobremesas if rng.random() < 0.7]
            for posicao, (nome_item, preco_base, papel, tipo) in enumerate(opcoes):
                preco = _preco(preco_base * fator_preco)
                preco_antigo = _preco(preco / fator_reajuste) if reajuste else preco
                peso = 1.8 if posicao == 0 else rng.uniform(0.6, 1.2)
                restaurante.itens.append(ItemCardapio(proximo_item, nome_item, tipo, papel, peso, preco, preco_antigo))
                proximo_item += 1
            restaurantes.append(restaurante)
    return restaurantes


def _gerar_clientes(rng: random.Random, quantidade: int = 4200) -> list[Cliente]:
    cidades = list(CIDADES)
    pesos = [c.demanda for c in CIDADES.values()]
    clientes = []
    for i in range(1, quantidade + 1):
        if rng.random() < 0.72:
            cadastro = _data_aleatoria(rng, date(2021, 1, 1), date(2025, 12, 31))
        else:
            cadastro = _data_aleatoria(rng, date(2026, 1, 1), date(2026, 10, 25))
        assinante = rng.random() < 0.22
        atividade = rng.lognormvariate(0, 0.9) * (1.8 if assinante else 1.0)
        clientes.append(Cliente(i, rng.choices(cidades, weights=pesos)[0], cadastro, assinante, atividade))
    return clientes


def _gerar_entregadores(rng: random.Random, quantidade: int = 460) -> list[Entregador]:
    cidades = list(CIDADES)
    pesos = [c.demanda for c in CIDADES.values()]
    entregadores = []
    for i in range(1, quantidade + 1):
        cadastro = (_data_aleatoria(rng, date(2021, 1, 1), date(2025, 12, 31)) if rng.random() < 0.85
                    else _data_aleatoria(rng, date(2026, 1, 1), date(2026, 9, 30)))
        modal = rng.choices(["moto", "bicicleta", "carro"], weights=[0.68, 0.22, 0.10])[0]
        entregadores.append(Entregador(i, rng.choices(cidades, weights=pesos)[0], modal, cadastro))
    return entregadores


class _SorteioClientes:
    """Sorteia clientes já cadastrados numa data, com peso pela atividade."""

    def __init__(self, clientes: list[Cliente]):
        self.clientes = sorted(clientes, key=lambda c: (c.cadastro, c.id))
        self.datas = [c.cadastro for c in self.clientes]
        self.acumulado = []
        soma = 0.0
        for c in self.clientes:
            soma += c.atividade
            self.acumulado.append(soma)

    def sortear(self, rng: random.Random, dia: date) -> Cliente:
        elegiveis = bisect.bisect_right(self.datas, dia)
        alvo = rng.random() * self.acumulado[elegiveis - 1]
        return self.clientes[bisect.bisect_left(self.acumulado, alvo, 0, elegiveis)]


def _sortear_minuto(rng: random.Random) -> int:
    sorteio = rng.random()
    if sorteio < 0.36:
        minuto = rng.gauss(12.6 * 60, 50)  # almoço
    elif sorteio < 0.44:
        minuto = rng.uniform(15 * 60, 18 * 60)  # tarde
    elif sorteio < 0.95:
        minuto = rng.gauss(20.1 * 60, 75)  # jantar
    else:
        minuto = rng.uniform(22 * 60, 23 * 60 + 59)  # fim de noite
    return int(min(max(minuto, 10 * 60 + 30), 23 * 60 + 59))


def _turno(minuto: int) -> int:
    if minuto < 15 * 60:
        return 0  # almoço
    if minuto < 18 * 60:
        return 1  # tarde
    return 2  # jantar


def gerar_base(caminho: Path | str = CAMINHO_BANCO, semente: int = SEMENTE_PADRAO) -> Path:
    """Gera a base do zero e devolve o caminho do arquivo."""
    rng = random.Random(semente)
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)

    restaurantes = _gerar_restaurantes(rng)
    clientes = _gerar_clientes(rng)
    entregadores = _gerar_entregadores(rng)

    clientes_por_cidade = {
        cidade: _SorteioClientes([c for c in clientes if c.cidade == cidade]) for cidade in CIDADES
    }
    restaurantes_por_cidade = {cidade: [r for r in restaurantes if r.cidade == cidade] for cidade in CIDADES}
    entregadores_por_cidade = {cidade: [e for e in entregadores if e.cidade == cidade] for cidade in CIDADES}
    cidades = list(CIDADES)
    pesos_cidades = [c.demanda for c in CIDADES.values()]

    pedidos, itens_pedido, avaliacoes = [], [], []
    ja_pediram: set[int] = set()
    dia = INICIO
    while dia <= FIM:
        dias_corridos = (dia - INICIO).days
        media = 72 * (1 + 0.045 * dias_corridos / 30.4)  # crescimento de ~4,5% ao mês
        media *= FATOR_DIA_SEMANA[dia.weekday()] * DATAS_ESPECIAIS.get(dia, 1.0)
        quantidade = max(0, _arredondar(rng.gauss(media, math.sqrt(media))))
        momentos = sorted(_sortear_minuto(rng) * 60 + rng.randint(0, 59) for _ in range(quantidade))

        for segundos in momentos:
            minuto = segundos // 60
            turno = _turno(minuto)
            pico = 19 * 60 <= minuto < 21 * 60 + 30
            criado_em = datetime.combine(dia, datetime.min.time()) + timedelta(seconds=segundos)

            cidade = rng.choices(cidades, weights=pesos_cidades)[0]
            info_cidade = CIDADES[cidade]
            abertos = [r for r in restaurantes_por_cidade[cidade] if r.cadastro <= dia]
            restaurante = rng.choices(abertos, weights=[r.popularidade * r.categoria.turnos[turno] for r in abertos])[0]
            cliente = clientes_por_cidade[cidade].sortear(rng, dia)

            # Itens do pedido.
            principais = [i for i in restaurante.itens if i.papel == "principal"]
            bebidas = [i for i in restaurante.itens if i.papel == "bebida"]
            sobremesas = [i for i in restaurante.itens if i.papel == "sobremesa"]
            distintos = rng.choices([1, 2, 3, 4], weights=[0.70, 0.22, 0.06, 0.02])[0]
            linhas = []
            for item in _sortear_sem_reposicao(rng, principais, distintos):
                preco = item.preco_antigo if restaurante.reajuste and dia < restaurante.reajuste else item.preco
                if preco < 10:
                    quantidade_item = rng.randint(2, 6)
                else:
                    quantidade_item = rng.choices([1, 2, 3], weights=[0.84, 0.13, 0.03])[0]
                linhas.append((item, quantidade_item, preco))
            if bebidas and rng.random() < 0.45:
                item = rng.choice(bebidas)
                preco = item.preco_antigo if restaurante.reajuste and dia < restaurante.reajuste else item.preco
                linhas.append((item, rng.choices([1, 2], weights=[0.8, 0.2])[0], preco))
            if sobremesas and rng.random() < 0.14:
                item = rng.choice(sobremesas)
                preco = item.preco_antigo if restaurante.reajuste and dia < restaurante.reajuste else item.preco
                linhas.append((item, 1, preco))
            subtotal = round(sum(q * p for _, q, p in linhas), 2)

            # Valores cobrados.
            distancia = round(min(max(rng.lognormvariate(math.log(2.7), 0.55), 0.3), 14.0), 2)
            primeiro_pedido = cliente.id not in ja_pediram
            ja_pediram.add(cliente.id)
            desconto = 0.0
            if rng.random() < (0.45 if primeiro_pedido else 0.12):
                desconto = round(min(subtotal * rng.uniform(0.10, 0.25), 25.0), 2)
            if restaurante.taxa_base == 0 or (cliente.assinante and subtotal >= 30):
                taxa = 0.0
            else:
                taxa = restaurante.taxa_base + (1.0 if distancia > 5 else 0.0) + (1.5 if distancia > 8 else 0.0)
            total = round(subtotal + taxa - desconto, 2)
            almoco_dia_util = turno == 0 and dia.weekday() < 5
            pagamento = rng.choices(
                ["pix", "cartao_credito", "cartao_debito", "vale_refeicao", "dinheiro"],
                weights=[0.33, 0.38, 0.10, 0.26 if almoco_dia_util else 0.04, 0.05],
            )[0]

            # Cancelamento ou entrega.
            previsao = 5 * _arredondar(
                (restaurante.categoria.preparo_min * 1.15 + distancia / 22 * 60 * info_cidade.transito + 12) / 5
            )
            chance_cancelar = (0.04 + (0.025 if pico else 0.0) + 0.06 * max(0.0, restaurante.eficiencia - 1.0)
                               + info_cidade.cancelamento_extra + (0.015 if pagamento == "dinheiro" else 0.0))
            pedido_id = len(pedidos) + 1
            if rng.random() < chance_cancelar:
                cartao = pagamento in ("cartao_credito", "cartao_debito")
                motivo = rng.choices(
                    ["restaurante_recusou", "cliente_desistiu", "sem_entregador", "atraso_no_preparo", "pagamento_recusado"],
                    weights=[0.30, 0.25, 0.30 if pico else 0.12, 0.17 * restaurante.eficiencia, 0.10 if cartao else 0.0],
                )[0]
                pedidos.append((pedido_id, cliente.id, restaurante.id, None, criado_em.isoformat(sep=" "), "cancelado",
                                motivo, pagamento, subtotal, round(taxa, 2), desconto, total, distancia, previsao,
                                None, None))
            else:
                categoria = restaurante.categoria
                fator_pico = 1.28 if pico else (1.12 if 12 * 60 <= minuto < 13 * 60 + 30 else 1.0)
                preparo = categoria.preparo_min * restaurante.eficiencia * fator_pico * rng.lognormvariate(0, 0.18)
                espera = rng.expovariate(1 / (6.0 if pico else 3.5))
                candidatos = [e for e in entregadores_por_cidade[cidade]
                              if e.cadastro <= dia and (distancia <= 4.0 or e.modal != "bicicleta")]
                entregador = rng.choice(candidatos)
                transito = info_cidade.transito * (1.15 if pico else 1.0)
                deslocamento = (distancia / VELOCIDADE_KMH[entregador.modal] * 60 * transito
                                * rng.lognormvariate(0, 0.15))
                tempo = max(8, _arredondar(preparo + espera + deslocamento))
                entregue_em = criado_em + timedelta(minutes=tempo)
                pedidos.append((pedido_id, cliente.id, restaurante.id, entregador.id, criado_em.isoformat(sep=" "),
                                "entregue", None, pagamento, subtotal, round(taxa, 2), desconto, total, distancia,
                                previsao, entregue_em.isoformat(sep=" "), tempo))

                atraso = tempo - previsao
                if rng.random() < (0.55 if atraso > 10 else 0.40):
                    nota_media = 4.55 + restaurante.qualidade
                    if atraso > 0:
                        nota_media -= min(2.2, 0.3 + 0.06 * atraso)
                    nota = int(min(5, max(1, _arredondar(rng.gauss(nota_media, 0.65)))))
                    comentario = None
                    if rng.random() < 0.35:
                        if atraso > 15 and nota <= 3 and rng.random() < 0.6:
                            comentario = rng.choice(COMENTARIOS_ATRASO)
                        else:
                            comentario = rng.choice(COMENTARIOS[nota])
                    criada_em = entregue_em + timedelta(minutes=rng.randint(10, 2880))
                    avaliacoes.append((len(avaliacoes) + 1, pedido_id, nota, comentario, criada_em.isoformat(sep=" ")))

            for item, quantidade_item, preco in linhas:
                itens_pedido.append((len(itens_pedido) + 1, pedido_id, item.id, quantidade_item, preco))
        dia += timedelta(days=1)

    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.unlink(missing_ok=True)
    conn = sqlite3.connect(temporario)
    try:
        conn.executescript(DDL)
        conn.executemany(
            "INSERT INTO restaurantes VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r.id, r.nome, r.categoria.nome, r.cidade, r.bairro, r.taxa_base, r.cadastro.isoformat())
             for r in restaurantes],
        )
        conn.executemany(
            "INSERT INTO clientes VALUES (?, ?, ?, ?)",
            [(c.id, c.cidade, c.cadastro.isoformat(), int(c.assinante)) for c in clientes],
        )
        conn.executemany(
            "INSERT INTO entregadores VALUES (?, ?, ?, ?)",
            [(e.id, e.cidade, e.modal, e.cadastro.isoformat()) for e in entregadores],
        )
        conn.executemany(
            "INSERT INTO itens_cardapio VALUES (?, ?, ?, ?, ?)",
            [(i.id, r.id, i.nome, i.tipo, i.preco) for r in restaurantes for i in r.itens],
        )
        conn.executemany("INSERT INTO pedidos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", pedidos)
        conn.executemany("INSERT INTO itens_pedido VALUES (?, ?, ?, ?, ?)", itens_pedido)
        conn.executemany("INSERT INTO avaliacoes VALUES (?, ?, ?, ?, ?)", avaliacoes)
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    temporario.replace(caminho)
    return caminho


def garantir_base(caminho: Path | str = CAMINHO_BANCO) -> Path:
    """Gera a base se ela ainda não existir."""
    caminho = Path(caminho)
    if not caminho.exists():
        gerar_base(caminho)
    return caminho


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera a base SQLite fictícia do marketplace de delivery.")
    parser.add_argument("--saida", type=Path, default=CAMINHO_BANCO, help="arquivo .db de saída")
    parser.add_argument("--semente", type=int, default=SEMENTE_PADRAO, help="semente do gerador aleatório")
    args = parser.parse_args()
    caminho = gerar_base(args.saida, args.semente)
    conn = sqlite3.connect(caminho)
    try:
        for tabela in ("restaurantes", "clientes", "entregadores", "itens_cardapio", "pedidos", "itens_pedido", "avaliacoes"):
            total = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            print(f"{tabela:<15} {total:>7} linhas")
    finally:
        conn.close()
    print(f"Base gerada em {caminho}")


if __name__ == "__main__":
    main()
