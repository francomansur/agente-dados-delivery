"""Agente que responde perguntas sobre a base de delivery.

O loop de tool use é escrito à mão, sem framework de agentes:

  1. envia a pergunta ao Claude junto com a ferramenta executar_sql;
  2. se o modelo pedir a ferramenta (stop_reason == "tool_use"), executa o SQL
     e devolve o resultado num tool_result. Erro de SQL volta com
     is_error=True, e o modelo corrige a consulta na volta seguinte;
  3. repete até o modelo dar a resposta final, um JSON validado por
     structured outputs: {"resposta": "...", "respondivel": true/false}.

O schema da base vai no system prompt (com cache de prompt), gerado pela
mesma função descrever_schema que o servidor MCP expõe como ferramenta.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .config import CAMINHO_BANCO, ESFORCO_PADRAO, MAX_LINHAS, MODELO_PADRAO
from .ferramentas import ErroSQL, ResultadoSQL, descrever_schema, executar_sql
from .precos import custo_usd

MAX_PASSOS = 8  # chamadas ao modelo por pergunta
MAX_TOKENS = 16_000

FERRAMENTA_SQL: dict[str, Any] = {
    "name": "executar_sql",
    "description": (
        "Executa UMA consulta SQL de leitura (SELECT ou WITH ... SELECT) na base SQLite do marketplace "
        "e devolve colunas e linhas em JSON. Use sempre que a resposta depender de números, listas ou "
        "datas da base. Comandos que alteram dados são bloqueados. O resultado vem limitado a "
        f"{MAX_LINHAS} linhas: agregue no SQL em vez de trazer linhas soltas."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Uma única consulta no dialeto do SQLite."},
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
    "strict": True,
}

FORMATO_RESPOSTA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "resposta": {"type": "string", "description": "Resposta em português, em Markdown."},
            "respondivel": {
                "type": "boolean",
                "description": "false quando a base não tem os dados necessários para responder.",
            },
        },
        "required": ["resposta", "respondivel"],
        "additionalProperties": False,
    },
}

INSTRUCOES = """\
Você é o analista de dados de um marketplace de delivery. Responde, em português, perguntas sobre a base SQLite descrita em <base>.

Como trabalhar:
- Todo número da resposta deve vir de uma consulta feita com a ferramenta executar_sql. Não estime nem invente valores.
- Escreva SQL para SQLite e deixe o banco fazer as contas: agregue, filtre e ordene no próprio SQL.
- Siga o glossário de métricas que está no fim da descrição da base.
- Se a consulta der erro, leia a mensagem, corrija o SQL e tente de novo.
- Se a pergunta tiver mais de uma interpretação razoável, escolha a mais comum e diga qual foi.
- Se a base não tiver os dados necessários, não troque a pergunta por outra parecida: explique o que falta e responda com respondivel = false. Faça o mesmo com perguntas fora do assunto da base.

Resposta final:
- resposta: texto curto em Markdown, com os números principais e suas unidades no formato brasileiro (R$ 1.234,56; 12,3%; 38,5 minutos). Em rankings e listas, use uma lista numerada com os valores.
- respondivel: true se a base permitiu responder; false se faltaram dados.
"""


@dataclass
class Consulta:
    """Um SQL que o modelo pediu para executar, com o resultado ou o erro."""

    sql: str
    resultado: ResultadoSQL | None = None
    erro: str | None = None


@dataclass
class Uso:
    """Tokens somados de todas as chamadas ao modelo numa pergunta."""

    modelo: str
    chamadas: int = 0
    entrada: int = 0
    saida: int = 0
    escrita_cache: int = 0
    leitura_cache: int = 0

    def somar(self, usage: Any) -> None:
        self.chamadas += 1
        self.entrada += usage.input_tokens
        self.saida += usage.output_tokens
        self.escrita_cache += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.leitura_cache += getattr(usage, "cache_read_input_tokens", 0) or 0

    @property
    def custo_usd(self) -> float:
        return custo_usd(self.modelo, self.entrada, self.saida, self.escrita_cache, self.leitura_cache)


@dataclass
class Resposta:
    texto: str
    respondivel: bool
    parada: str  # end_turn, refusal, max_tokens, formato_invalido ou limite_de_passos
    consultas: list[Consulta]
    uso: Uso
    segundos: float
    mensagens: list[dict[str, Any]] = field(repr=False)  # histórico completo, para continuar a conversa

    @property
    def consulta_final(self) -> Consulta | None:
        """Última consulta que rodou sem erro: é a que embasa a resposta."""
        return next((c for c in reversed(self.consultas) if c.resultado is not None), None)


class ModeloInesperado(RuntimeError):
    """A API respondeu com um modelo diferente do pedido."""


class Agente:
    def __init__(
        self,
        modelo: str = MODELO_PADRAO,
        caminho_banco: Path | str = CAMINHO_BANCO,
        esforco: str = ESFORCO_PADRAO,
        cliente: Any = None,
        max_passos: int = MAX_PASSOS,
    ):
        self.modelo = modelo
        self.caminho_banco = Path(caminho_banco)
        self.esforco = esforco
        self.max_passos = max_passos
        self.cliente = cliente or anthropic.Anthropic(timeout=120.0, max_retries=3)
        schema = descrever_schema(self.caminho_banco)
        # Bloco fixo e igual em toda chamada: o cache_control guarda ferramentas + system no cache.
        self.system = [
            {
                "type": "text",
                "text": f"{INSTRUCOES}\n<base>\n{schema}\n</base>",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def perguntar(self, pergunta: str, historico: list[dict[str, Any]] | None = None) -> Resposta:
        """Responde uma pergunta. Passe resposta.mensagens em historico para continuar a conversa."""
        inicio = time.monotonic()
        mensagens: list[dict[str, Any]] = list(historico or [])
        mensagens.append({"role": "user", "content": pergunta})
        consultas: list[Consulta] = []
        uso = Uso(modelo=self.modelo)

        for _ in range(self.max_passos):
            resposta = self.cliente.messages.create(
                model=self.modelo,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=[FERRAMENTA_SQL],
                messages=mensagens,
                output_config={"effort": self.esforco, "format": FORMATO_RESPOSTA},
                cache_control={"type": "ephemeral"},  # cache automático da conversa que cresce no loop
            )
            if resposta.model != self.modelo:
                raise ModeloInesperado(f"Pedi {self.modelo}, mas a resposta veio de {resposta.model}.")
            uso.somar(resposta.usage)

            if resposta.stop_reason == "tool_use":
                # O conteúdo inteiro volta para o histórico (inclui blocos de thinking e tool_use).
                mensagens.append({"role": "assistant", "content": resposta.content})
                resultados = [self._executar(bloco, consultas) for bloco in resposta.content if bloco.type == "tool_use"]
                mensagens.append({"role": "user", "content": resultados})  # todos os resultados numa mensagem só
                continue

            texto, respondivel, parada = self._ler_resposta_final(resposta)
            if parada == "end_turn" or parada == "formato_invalido":
                mensagens.append({"role": "assistant", "content": resposta.content})
            return Resposta(texto, respondivel, parada, consultas, uso, time.monotonic() - inicio, mensagens)

        texto = "Não cheguei a uma resposta dentro do limite de passos. Tente reformular a pergunta."
        return Resposta(texto, False, "limite_de_passos", consultas, uso, time.monotonic() - inicio, mensagens)

    def _executar(self, bloco: Any, consultas: list[Consulta]) -> dict[str, Any]:
        """Roda a ferramenta pedida e monta o tool_result correspondente."""
        sql = bloco.input.get("sql") if isinstance(bloco.input, dict) else None
        if bloco.name != "executar_sql" or not isinstance(sql, str):
            return {
                "type": "tool_result",
                "tool_use_id": bloco.id,
                "content": f"Chamada inválida: {bloco.name} com {bloco.input!r}. Use executar_sql com o campo sql.",
                "is_error": True,
            }
        try:
            resultado = executar_sql(sql, self.caminho_banco)
        except ErroSQL as erro:
            consultas.append(Consulta(sql=sql, erro=str(erro)))
            return {"type": "tool_result", "tool_use_id": bloco.id, "content": str(erro), "is_error": True}
        consultas.append(Consulta(sql=sql, resultado=resultado))
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": resultado.para_texto()}

    @staticmethod
    def _ler_resposta_final(resposta: Any) -> tuple[str, bool, str]:
        if resposta.stop_reason == "refusal":
            return "O modelo recusou esta pergunta.", False, "refusal"
        if resposta.stop_reason == "max_tokens":
            return "A resposta passou do limite de tokens e foi cortada.", False, "max_tokens"
        bruto = "".join(bloco.text for bloco in resposta.content if bloco.type == "text")
        try:
            dados = json.loads(bruto)
            return str(dados["resposta"]), bool(dados["respondivel"]), "end_turn"
        except (json.JSONDecodeError, KeyError, TypeError):
            return bruto.strip() or "(resposta vazia)", True, "formato_invalido"
