"""Caminhos e valores padrão usados pelo projeto."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Raiz do repositório (src/agente_delivery/config.py -> sobe dois níveis).
RAIZ_PROJETO = Path(__file__).resolve().parents[2]

# Lê ANTHROPIC_API_KEY e as demais variáveis do .env local, se existir.
# Variáveis já definidas no ambiente têm prioridade.
load_dotenv(RAIZ_PROJETO / ".env")

# A base pode ser trocada pela variável de ambiente AGENTE_BANCO.
CAMINHO_BANCO = Path(os.environ.get("AGENTE_BANCO", RAIZ_PROJETO / "data" / "delivery.db"))

MODELO_PADRAO = os.environ.get("AGENTE_MODELO", "claude-opus-5-5")
ESFORCO_PADRAO = "medium"

# Limites do executar_sql.
MAX_LINHAS = 100
TIMEOUT_SQL_SEGUNDOS = 5.0
