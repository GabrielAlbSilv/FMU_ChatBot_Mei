# Mudanças

## v13 — LC 238/2026, segurança e qualidade (27/11/2026)

**Informação**
- `regras_fiscais.json`: Lei Complementar nº 238/2026 — teto de R$ 110 mil (2027) e R$ 140 mil (2028) com
  limite proporcional de R$ 9.166,67 e R$ 11.666,67 por mês, ambos `vigente: false` até a lei orçamentária
  confirmar; até 2 empregados a partir de 01/01/2027; perdão automático das dívidas da contribuição
  previdenciária do MEI vencidas até 13/10/2026.
- Novos fatos calculados: `empregados` e `remissao`. Excesso no ano de um teto ainda não confirmado mostra os
  dois cenários, sem fixar conclusão. O limite novo passa a valer só trocando `vigente` (não depende mais do
  salário mínimo do ano).
- Quando há fatos calculados, a resposta só aparece depois de conferida com eles.
- A reescrita da pergunta devolve JSON com dados sugeridos; o código aceita só o que confirma no texto do usuário.
- Busca: um mesmo PDF ocupa no máximo 5 das 8 vagas (o do IRPF tem ~77% dos trechos), exceto em perguntas de IR.
- Aviso nos logs e no status de PDFs ativos que faltam na pasta e de regras ainda não validadas pelo NAF.

**Segurança e privacidade**
- Limite por IP usa o IP real (`True-Client-IP` do Cloudflare); o `X-Forwarded-For`, falsificável, não é usado.
- Limite global por minuto (`LIMITE_GLOBAL`) protege a cota da Groq.
- `privacidade.py`: CPF, CNPJ, e-mail e telefone mascarados antes de logs, cache e Groq; aviso na tela do chat.
- `/api/status` público mostra só o básico; detalhes e métricas exigem `X-Token` (`STATUS_TOKEN`).
- Cabeçalhos `X-Frame-Options` e `Permissions-Policy`.

**Código e infraestrutura**
- `config.py` concentra todas as variáveis de ambiente.
- `rag.py` dividido: `busca.py` (índice e recuperação) + `rag.py` (conversa com o modelo).
- `testes.py` dividido em `tests/` (um arquivo por assunto); 88 → 118 testes.
- `requirements.txt` com faixas de versão; `requirements-testes.txt` para o CI.
- GitHub Actions: workflow de testes; a atualização de PDFs roda os testes antes de publicar;
  `manter-acordado.yml` opcional.
- `base_web.py`: uma coleta por vez (trava real) e reindexação adiada das páginas aprendidas.
- Cache de respostas protegido por trava (várias threads no gunicorn).

**Qualidade**
- Botões Útil / Não ajudou → `POST /api/feedback`; métricas em `metricas.py` (logs `[METRICA]`/`[FEEDBACK]`).
- `avaliar_casos.py`: 5 casos novos da LC 238 e histórico em `relatorios/historico_avaliacoes.jsonl`.
- `avaliar_busca.py`: compara modelos de embeddings (acertos e memória) antes de trocar no Render.
- `README.md` com arquitetura, decisões, deploy e manutenção.
