"""Linha de comando: faz uma pergunta ao agente e mostra resposta, SQL e custo.

    python -m agente_delivery "Qual o ticket médio em São Paulo?"
    python -m agente_delivery --modelo claude-haiku-5-5 "Qual cidade fatura mais?"
"""

from __future__ import annotations

import argparse

from .agente import Agente
from .base_sintetica import garantir_base
from .config import CAMINHO_BANCO, ESFORCO_PADRAO, MODELO_PADRAO


def _tabela(colunas: list[str], linhas: list[tuple], limite: int = 15) -> str:
    if not colunas:
        return "(sem colunas)"
    textos = [[str(v) for v in linha] for linha in linhas[:limite]]
    larguras = [max(len(c), *(len(t[i]) for t in textos)) if textos else len(c) for i, c in enumerate(colunas)]
    saida = [" | ".join(c.ljust(w) for c, w in zip(colunas, larguras)), "-+-".join("-" * w for w in larguras)]
    saida += [" | ".join(t.ljust(w) for t, w in zip(linha, larguras)) for linha in textos]
    if len(linhas) > limite:
        saida.append(f"... mais {len(linhas) - limite} linhas")
    return "\n".join(saida)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pergunte em português sobre a base de delivery.")
    parser.add_argument("pergunta", help="pergunta em linguagem natural")
    parser.add_argument("--modelo", default=MODELO_PADRAO, help=f"modelo do Claude (padrão: {MODELO_PADRAO})")
    parser.add_argument("--esforco", default=ESFORCO_PADRAO, choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args()

    garantir_base(CAMINHO_BANCO)
    agente = Agente(modelo=args.modelo, esforco=args.esforco)
    resposta = agente.perguntar(args.pergunta)

    print(resposta.texto)
    if not resposta.respondivel:
        print("\n(A base não tem os dados para responder essa pergunta.)")
    for numero, consulta in enumerate(resposta.consultas, start=1):
        print(f"\n--- SQL {numero}{' (erro, o agente corrigiu)' if consulta.erro else ''} ---")
        print(consulta.sql)
        if consulta.erro:
            print(f"Erro: {consulta.erro}")
    if resposta.consulta_final and resposta.consulta_final.resultado:
        resultado = resposta.consulta_final.resultado
        print("\n--- Resultado ---")
        print(_tabela(resultado.colunas, resultado.linhas))
    uso = resposta.uso
    print(
        f"\n{uso.chamadas} chamadas ao modelo | {uso.entrada + uso.escrita_cache + uso.leitura_cache} tokens de entrada "
        f"({uso.leitura_cache} do cache) | {uso.saida} de saída | US$ {uso.custo_usd:.4f} | {resposta.segundos:.1f} s"
    )


if __name__ == "__main__":
    main()
