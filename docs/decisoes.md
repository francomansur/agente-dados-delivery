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

## 15/11/2026: exemplo real de chamada no lugar do GIF

- **Contexto:** o plano pedia um GIF no README. Gravar um GIF de terminal exigiria instalar ferramentas fora do projeto, como vhs ou asciinema, e o prazo da candidatura estava curto.
- **Decisão:** o README mostra um exemplo real de chamada e resposta, copiado de uma chamada à API rodando em Docker, com a pergunta, o SQL gerado e as linhas.
- **Alternativa:** gravar o GIF depois, com o vhs, se o Franco quiser.

## 15/11/2026: estrutura em camadas por responsabilidade

- **Contexto:** a classe `Database` misturava a validação da entrada, o validador, a execução, a descrição do schema e a auditoria, e o `api.py` juntava as rotas com a montagem dos objetos (conexões, cliente do modelo e serviços). O Franco pediu a divisão que usa no trabalho: o service tem as validações e as regras de negócio, e o repository tem só SQL.
- **Decisão:** cinco camadas, com as dependências num sentido só: `api` → `services` → (`repositories`, `llm`) → `core`.
  - `api/routes.py` só trata o HTTP: lê o pedido, chama o service e traduz os erros em status.
  - `services/` tem os casos de uso e as regras. O `question_service.py` responde a pergunta, o `graph.py` é o fluxo do LangGraph, o `sql_tools.py` passa o SQL do modelo pelo Pydantic, pelo validador, pelo repository e pela auditoria, e o `sql_validator.py` tem as regras de segurança do SQL.
  - `repositories/delivery_repository.py` só fala com o banco: conexões, `ping`, execução somente leitura de um SELECT e as consultas ao catálogo que descrevem as tabelas. Ele não importa o validador nem a auditoria.
  - `llm/` tem o cliente do Claude, os prompts e os preços. Os prompts recebem o schema e o resultado como texto e valores, sem importar o banco.
  - `core/` tem as configurações, os erros e a auditoria.
  - `main.py` cria o app e, no lifespan, monta as peças de cada camada; as rotas as recebem pelo `Depends` do FastAPI. É o único módulo que conhece todas as camadas, como um container de injeção de dependência, e segue o formato da documentação do FastAPI.
  - O `tests/test_layers.py` confere os imports de cada camada.
- **Por que um repository só:** o SQL das perguntas é escrito pelo modelo, então não há consultas fixas por tabela, como um `OrderRepository` com um método por consulta. O repository oferece a execução somente leitura e as consultas ao catálogo, e isso serve a qualquer pergunta. Se a ação de escrita com aprovação (EXT-01) entrar, ela terá consultas fixas, e aí surge um `ReviewRepository`.
- **Prova de que o comportamento não mudou:** a descrição do schema, os system prompts e o OpenAPI saíram idênticos, byte a byte, aos de antes da refatoração. A avaliação rodou de novo, no commit `a8c681d`, e o `make compare` não marcou nenhuma pergunta: 100% de acerto antes e depois, nas 60 execuções. O tempo médio passou de 3,2 s para 3,6 s, mas as consultas ao banco levaram o mesmo tempo (cerca de 25 ms cada, pela auditoria); a diferença está nas chamadas ao modelo.
- **Alternativas:**
  - manter o pacote sem camadas, mais curto, mas com responsabilidades misturadas;
  - um repository por tabela, que não serve quando quem escreve o SQL é o modelo;
  - deixar a montagem dos objetos em `api/routes.py`, que misturaria as rotas com as conexões e faria as rotas importarem todas as camadas.

## 15/11/2026: interface web simples, sem framework

- **Contexto:** o Franco queria testar o agente numa tela, além do `/docs` e do `curl`. O plano já tinha deixado o Gradio de fora.
- **Decisão:** uma página só, servida pela própria API em `GET /`, com HTML, CSS e JavaScript puros em `src/delivery_agent/web/`, sem framework, sem build e sem nada de fora do servidor.
  - Ela chama `POST /questions?include_details=true` com um `fetch` comum e mostra cada pergunta como um cartão: a resposta, a tabela e, sob demanda, o SQL, as tentativas, o tempo e o custo.
  - A página não guarda conversa, porque o agente não tem memória, e uma dica embaixo do campo avisa isso.
- **Visual:** o Franco pediu o estilo dos apps de delivery: vermelho, branco e cartões com cantos arredondados. A página não usa nome, logo nem fonte de nenhuma empresa. O vermelho é um pouco mais escuro que o habitual, para o texto branco dos botões manter o contraste mínimo de 4,5:1.
- **Por quê:** a API já devolve tudo o que a tela precisa, e uma página estática não acrescenta dependência, etapa de build nem servidor. Sem recursos externos, ela funciona sem internet e tem uma política de segurança de conteúdo (CSP) que só aceita o próprio servidor.
- **Alternativas:**
  - Gradio ou Streamlit, rápidos de montar em Python, mas seriam um segundo app, e o plano tinha deixado o Gradio de fora;
  - React com Vite, que exigiria Node e uma etapa de build para uma tela pequena;
  - mostrar os passos do agente ao vivo, com SSE, que fica como extra para depois.
