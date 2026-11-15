# AGENTS.md

Guia para quem vai mexer neste repositório, seja pessoa ou agente de IA.

## Antes de mudar o código

- A fonte da verdade é o [`docs/spec.md`](docs/spec.md). Cada requisito tem um ID e uma prova (um teste ou uma pergunta da avaliação) na tabela de rastreabilidade.
- Se uma mudança altera um comportamento, atualize a spec e a prova no mesmo commit.
- Decisões de arquitetura vão para o [`docs/decisoes.md`](docs/decisoes.md), com contexto e alternativas.

## Comandos

```bash
make install            # cria o .venv e instala o projeto com as dependências de desenvolvimento
cp .env.example .env    # depois, preencha as senhas
make db-up              # sobe o Postgres com os dados fictícios (Docker)
make check              # ruff, mypy e pytest com cobertura
make db-reset           # recria o banco do zero, depois de mudar o schema ou o gerador
```

Os testes marcados com `db` precisam do Postgres no ar. Sem ele, são pulados; no CI, com `REQUIRE_DB=1`, falham.

## Idioma

Uma língua por camada:
- **Inglês nos identificadores:** pacote, módulos, classes, funções, variáveis, docstrings, mensagens de commit, tabelas, colunas, rotas e campos da API, variáveis de ambiente e códigos de status (`delivered`, `credit_card`, `invalid_format`).
- **Português no conteúdo para pessoas:** dados do negócio, respostas do agente, mensagens que chegam ao usuário ou ao modelo, README, spec, `docs/decisoes.md` e descrições de tabelas e colunas (`COMMENT ON`).

O código escrito antes dessa regra, em português, foi convertido para inglês ao ser reaproveitado. O original continua no histórico do git.

## Estilo de código

- `ruff format` e `ruff check` limpos, e `mypy --strict` sem erros.
- Docstrings no estilo Google.
- **Espaçamento vertical.** O `ruff format` preserva essas linhas em branco, mas não as cria sozinho:
  - linha em branco antes e depois de cada bloco `if`, `for`, `while`, `try` e `with` dentro de funções, exceto quando o bloco é o primeiro ou o último comando de onde está;
  - linha em branco antes do `return` quando há outros comandos antes dele no mesmo bloco;
  - etapas lógicas separadas por uma linha em branco, e nunca duas linhas em branco seguidas dentro de uma função.

Exemplo:

```python
text, answerable, stop_reason = self._read_final_answer(response)

if stop_reason in ("end_turn", "invalid_format"):
    messages.append({"role": "assistant", "content": response.content})

    return Answer(text, answerable, stop_reason, queries, usage, time.monotonic() - start, messages)

text = "Não cheguei a uma resposta dentro do limite de passos. Tente reformular a pergunta."

return Answer(text, False, "step_limit", queries, usage, time.monotonic() - start, messages)
```

O exemplo também mostra a regra de idioma: os códigos internos (`"invalid_format"`, `"step_limit"`) ficam em inglês, e só a mensagem para o usuário fica em português.

## Versionamento

- Tudo direto na branch `main`.
- Um commit por passo, e não por etapa inteira, com um assunto só, no padrão Conventional Commits e em inglês. Por exemplo: `feat: add read-only database role`.
- Ao fim de cada etapa, uma tag: `etapa-1-base`, `etapa-2-agente` e `etapa-3-avaliacao`.
- Os resultados da avaliação (`evals/runs/`) também vão para o Git, porque são o histórico de regressão da IA.
- Commits feitos com ajuda de IA mantêm a linha `Co-Authored-By`.
