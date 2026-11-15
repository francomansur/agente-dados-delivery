"""Núcleo de ferramentas: descrever_schema e executar_sql.

As mesmas funções servem ao agente (agente.py), ao servidor MCP
(servidor_mcp.py) e à interface web (app.py).

O executar_sql aceita apenas leitura e tem quatro camadas de proteção:
  1. validação do texto: um único comando, começando por SELECT ou WITH,
     sem palavras-chave de escrita fora de strings e comentários;
  2. conexão aberta em modo somente leitura (mode=ro) e PRAGMA query_only;
  3. autorizador do SQLite, que nega qualquer ação que não seja leitura;
  4. limites: no máximo MAX_LINHAS linhas devolvidas, tempo máximo de
     execução e tamanho máximo de texto/blob.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CAMINHO_BANCO, MAX_LINHAS, TIMEOUT_SQL_SEGUNDOS

GLOSSARIO = """\
- Pedidos feitos (ou realizados): todos os pedidos, inclusive os cancelados. Pedidos entregues: status = 'entregue'.
- Faturamento: soma de pedidos.total dos pedidos com status = 'entregue'.
- Ticket médio: média de pedidos.total dos pedidos com status = 'entregue'.
- Taxa de cancelamento: pedidos com status = 'cancelado' divididos por todos os pedidos.
- Pedido atrasado: pedido entregue com tempo_entrega_min > previsao_entrega_min.
- Tempo de entrega: pedidos.tempo_entrega_min (do pedido feito até a entrega), só de pedidos entregues.
- Itens vendidos: some itens_pedido.quantidade de pedidos entregues; a receita de um item é quantidade x preco_unitario.
- Cidade de um pedido: a cidade do restaurante (cliente e restaurante são sempre da mesma cidade).
- Dia da semana: strftime('%w', criado_em) devolve 0 = domingo, 1 = segunda ... 6 = sábado.
"""

_PALAVRAS_PROIBIDAS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH",
    "PRAGMA", "VACUUM", "REINDEX", "ANALYZE", "TRUNCATE", "GRANT", "REVOKE",
)
_RE_PROIBIDAS = re.compile(r"\b(" + "|".join(_PALAVRAS_PROIBIDAS) + r")\b", re.IGNORECASE)
_ACOES_PERMITIDAS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_TAMANHO_MAX_TEXTO = 1_000_000  # bytes por string ou blob gerado na consulta
_TAMANHO_MAX_CELULA = 300  # caracteres por célula devolvida ao modelo


class ErroSQL(Exception):
    """Erro esperado ao executar SQL. Volta ao modelo como tool_result com is_error."""


class SQLRecusado(ErroSQL):
    """A validação bloqueou a consulta antes de ela chegar ao banco."""


class TempoEsgotado(ErroSQL):
    """A consulta passou do tempo máximo e foi interrompida."""


class ErroExecucao(ErroSQL):
    """O SQLite recusou ou não conseguiu executar a consulta."""


@dataclass(frozen=True)
class ResultadoSQL:
    sql: str
    colunas: list[str]
    linhas: list[tuple[Any, ...]]
    truncado: bool
    duracao_ms: float

    def para_dict(self) -> dict[str, Any]:
        return {
            "colunas": self.colunas,
            "linhas": [[_valor_serializavel(v) for v in linha] for linha in self.linhas],
            "total_linhas": len(self.linhas),
            "truncado": self.truncado,
        }

    def para_texto(self) -> str:
        """Versão compacta enviada ao modelo como resultado da ferramenta."""
        dados = self.para_dict()
        if self.truncado:
            dados["aviso"] = (f"Resultado cortado em {len(self.linhas)} linhas. "
                              "Agregue no SQL ou use LIMIT se precisar de menos linhas.")
        return json.dumps(dados, ensure_ascii=False)


def _valor_serializavel(valor: Any) -> Any:
    if isinstance(valor, bytes):
        return f"<blob de {len(valor)} bytes>"
    if isinstance(valor, str) and len(valor) > _TAMANHO_MAX_CELULA:
        return valor[:_TAMANHO_MAX_CELULA] + "…"
    return valor


def _esqueleto(sql: str) -> str:
    """Remove comentários e esvazia strings e identificadores entre aspas.

    O resultado preserva a estrutura do comando e permite procurar ';' e
    palavras-chave sem cair em pegadinhas como "SELECT 'DROP TABLE x'".
    """
    saida: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            fim = sql.find("\n", i)
            i = n if fim == -1 else fim
            saida.append(" ")
        elif c == "/" and sql.startswith("/*", i):
            fim = sql.find("*/", i + 2)
            i = n if fim == -1 else fim + 2
            saida.append(" ")
        elif c in ("'", '"', "`", "["):
            fecha = "]" if c == "[" else c
            j = i + 1
            while j < n:
                if sql[j] == fecha:
                    if fecha != "]" and j + 1 < n and sql[j + 1] == fecha:  # aspas escapadas ('')
                        j += 2
                        continue
                    break
                j += 1
            saida.append(" x ")
            i = j + 1
        else:
            saida.append(c)
            i += 1
    return "".join(saida)


def validar_sql(sql: str) -> str:
    """Confere se o SQL é uma única consulta de leitura. Devolve o SQL sem espaços nas pontas."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLRecusado("A consulta está vazia.")
    esqueleto = _esqueleto(sql)
    comandos = [parte for parte in esqueleto.split(";") if parte.strip()]
    if not comandos:
        raise SQLRecusado("A consulta está vazia.")
    if len(comandos) > 1:
        raise SQLRecusado("Envie um único comando SQL por vez.")
    primeira = comandos[0].split(None, 1)[0].upper()
    if primeira not in ("SELECT", "WITH"):
        raise SQLRecusado(f"Só consultas de leitura (SELECT ou WITH) são permitidas; recebi um comando {primeira}.")
    proibida = _RE_PROIBIDAS.search(comandos[0])
    if proibida:
        raise SQLRecusado(f"A consulta contém {proibida.group(1).upper()}, que não é permitido. Use apenas leitura.")
    return sql.strip()


def _autorizador(acao: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if acao in _ACOES_PERMITIDAS else sqlite3.SQLITE_DENY


def _abrir_leitura(caminho: Path | str) -> sqlite3.Connection:
    caminho = Path(caminho)
    if not caminho.exists():
        raise FileNotFoundError(
            f"Base não encontrada em {caminho}. Gere com: python -m agente_delivery.base_sintetica"
        )
    conn = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def conectar_somente_leitura(caminho: Path | str = CAMINHO_BANCO) -> sqlite3.Connection:
    """Abre a base para SQL vindo de fora: modo leitura, autorizador e limites ligados."""
    conn = _abrir_leitura(caminho)  # o PRAGMA roda antes do autorizador, que nega PRAGMA
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _TAMANHO_MAX_TEXTO)
    conn.set_authorizer(_autorizador)
    return conn


def executar_sql(
    sql: str,
    caminho_banco: Path | str = CAMINHO_BANCO,
    max_linhas: int = MAX_LINHAS,
    timeout_s: float = TIMEOUT_SQL_SEGUNDOS,
) -> ResultadoSQL:
    """Executa uma consulta de leitura e devolve no máximo max_linhas linhas.

    Levanta SQLRecusado, TempoEsgotado ou ErroExecucao (todas filhas de
    ErroSQL) com uma mensagem que o modelo consegue usar para corrigir o SQL.
    """
    sql = validar_sql(sql)
    conn = conectar_somente_leitura(caminho_banco)
    inicio = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - inicio > timeout_s), 1000)
    try:
        cursor = conn.execute(sql)
        linhas = cursor.fetchmany(max_linhas + 1)
        colunas = [d[0] for d in cursor.description or []]
    except (sqlite3.Error, sqlite3.Warning) as erro:
        mensagem = str(erro)
        if "interrupted" in mensagem:
            raise TempoEsgotado(
                f"A consulta passou de {timeout_s:g} s e foi interrompida. Simplifique ou filtre mais."
            ) from erro
        if "not authorized" in mensagem or "readonly" in mensagem:
            raise SQLRecusado(f"O banco recusou a operação ({mensagem}). Só leitura é permitida.") from erro
        raise ErroExecucao(f"Erro do SQLite: {mensagem}") from erro
    finally:
        conn.close()
    truncado = len(linhas) > max_linhas
    return ResultadoSQL(
        sql=sql,
        colunas=colunas,
        linhas=[tuple(linha) for linha in linhas[:max_linhas]],
        truncado=truncado,
        duracao_ms=round((time.monotonic() - inicio) * 1000, 1),
    )


def descrever_schema(caminho_banco: Path | str = CAMINHO_BANCO) -> str:
    """Descreve tabelas, colunas, valores possíveis e o glossário de métricas."""
    conn = _abrir_leitura(caminho_banco)  # SQL nosso, sem autorizador (usa pragma_table_info)
    try:
        tabelas = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
        partes = [
            "# Base de um marketplace de delivery (dados fictícios)",
            "",
        ]
        inicio, fim = conn.execute("SELECT MIN(criado_em), MAX(criado_em) FROM pedidos").fetchone()
        partes.append(f"Pedidos de {inicio[:10]} a {fim[:10]}. Datas e horas são texto ISO (AAAA-MM-DD HH:MM:SS).")
        partes.append("")
        for nome, ddl in tabelas:
            total = conn.execute(f'SELECT COUNT(*) FROM "{nome}"').fetchone()[0]
            partes.append(f"## {nome} ({total} linhas)")
            partes.append(ddl.strip() + ";")
            valores = []
            for coluna in conn.execute(f"SELECT name, type FROM pragma_table_info('{nome}')").fetchall():
                if coluna[1] != "TEXT" or coluna[0].endswith(("_em", "data_cadastro")):
                    continue
                distintos = conn.execute(
                    f'SELECT DISTINCT "{coluna[0]}" FROM "{nome}" WHERE "{coluna[0]}" IS NOT NULL '
                    f'ORDER BY 1 LIMIT 16'
                ).fetchall()
                if len(distintos) <= 15:
                    valores.append(f"- {coluna[0]}: " + ", ".join(str(v[0]) for v in distintos))
            if valores:
                partes.append("Valores possíveis:")
                partes.extend(valores)
            partes.append("")
        partes.append("## Glossário de métricas")
        partes.append(GLOSSARIO)
        return "\n".join(partes)
    finally:
        conn.close()
