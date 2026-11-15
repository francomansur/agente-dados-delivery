# Decisões

Registro curto das decisões do projeto: o contexto, o que foi decidido e as alternativas consideradas.

## 15/11/2026: rota e campos da API em inglês

- **Contexto:** o plano citava `POST /perguntas` com `{resposta, sql, linhas}`, mas a regra de idioma do projeto põe os identificadores em inglês.
- **Decisão:** `POST /questions`, que devolve `{answer, sql, rows, ...}`.
- **Alternativa:** manter os nomes em português, o que abriria uma exceção à regra de idioma.

## 15/11/2026: 20 perguntas na avaliação, 16 com SQL e 4 recusas

- **Contexto:** o plano pede 20 perguntas com resposta calculada por SQL escrito à mão. O primeiro passo do fluxo, porém, é recusar o que está fora do escopo, e isso também precisa ser medido.
- **Decisão:** 16 perguntas com SQL de referência e 4 em que o certo é recusar (falta de dado, dado pessoal e tentativa de prompt injection).
- **Alternativa:** 24 perguntas, sendo 20 com SQL e 4 recusas.

## 15/11/2026: 3 repetições por pergunta

- **Contexto:** o modelo varia um pouco entre execuções, e uma oscilação isolada não deve ser tratada como regressão.
- **Decisão:** cada rodada executa cada pergunta 3 vezes. A comparação entre rodadas só marca uma pergunta quando o resultado dela muda em pelo menos 2 das 3 repetições.
- **Alternativa:** 1 execução por pergunta, mais barata, mas incapaz de separar a oscilação de uma regressão.

## 15/11/2026: validador de SQL com o sqlglot

- **Contexto:** o validador anterior era um analisador léxico feito à mão para o SQLite. No Postgres, ele precisaria tratar strings com escape (`E'...'`), dollar quoting, comentários aninhados e o `LIMIT` do nível externo, que são casos de borda fáceis de errar.
- **Decisão:** usar o sqlglot, com o dialeto do Postgres, para verificar que há uma única instrução, que ela é uma consulta de leitura, que não há escrita em nenhum ponto da árvore (inclusive dentro de um `WITH`), que não há funções proibidas e se já existe `LIMIT`. O SQL executado continua sendo o texto original, só com o `LIMIT` acrescentado: o sqlglot analisa, mas não reescreve.
- **Por quê:** num teste com 39 tentativas de burla, o sqlglot recusou 37. Das duas que passaram, uma é inofensiva (`TABLE orders`, atalho de leitura do Postgres), e a outra, um identificador com escape Unicode (`U&"..."`) que ele não decodifica, passa a ser recusada antes da análise. Ele também aceitou as 26 consultas válidas do teste.
- **Alternativas:** manter o analisador feito à mão, com mais código próprio e mais risco nos casos de borda; ou usar o pglast, que usa o parser do próprio Postgres e é mais exato, mas depende de uma extensão em C. Como a barreira principal é o banco (usuário somente leitura e permissões), o sqlglot basta.
