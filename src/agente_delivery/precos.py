"""Preços da API da Anthropic, usados para calcular o custo de cada pergunta."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preco:
    """US$ por milhão de tokens."""

    entrada: float
    saida: float
    escrita_cache: float  # cache de 5 minutos: 1,25x a entrada
    leitura_cache: float


# Tabela de preços da API (novembro de 2026). Tokens de raciocínio (thinking)
# são cobrados como saída e já vêm somados em usage.output_tokens.
PRECOS: dict[str, Preco] = {
    "claude-opus-5-5": Preco(entrada=4.00, saida=20.00, escrita_cache=5.00, leitura_cache=0.20),
    "claude-sonnet-5-5": Preco(entrada=2.00, saida=10.00, escrita_cache=2.50, leitura_cache=0.10),
    "claude-haiku-5-5": Preco(entrada=0.10, saida=0.50, escrita_cache=0.125, leitura_cache=0.01),
}


def custo_usd(modelo: str, entrada: int, saida: int, escrita_cache: int = 0, leitura_cache: int = 0) -> float:
    """Custo em dólares de um conjunto de tokens no modelo informado."""
    try:
        preco = PRECOS[modelo]
    except KeyError:
        raise KeyError(f"Sem preço cadastrado para o modelo {modelo!r}. Adicione em precos.py.") from None
    return (
        entrada * preco.entrada
        + saida * preco.saida
        + escrita_cache * preco.escrita_cache
        + leitura_cache * preco.leitura_cache
    ) / 1_000_000
