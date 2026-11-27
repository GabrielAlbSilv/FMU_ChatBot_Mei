# Banco de dados (PostgreSQL no Neon, gratuito) — passo a passo

O banco é **opcional**. Sem ele o FiscOn funciona igual, mas as métricas e os votos 👍/👎 somem a cada
reinício do Render. Com ele, ficam guardados e aparecem no painel `/painel`.

**O que fica guardado:** um resumo de cada resposta (pergunta **já sem CPF/CNPJ/e-mail/telefone**, início da
resposta, se foi conferida/atualizada/corrigida, tempo), o voto e o comentário de quem votou, e o resultado
de cada avaliação automática. As perguntas são apagadas sozinhas depois de 180 dias (`RETENCAO_DIAS`).

**Custo:** zero. O plano Free do Neon dá 1 GB por projeto (o FiscOn usa poucos MB por mês) e "dorme" depois
de 5 minutos sem uso, o que economiza a cota de processamento. Não pede cartão de crédito.

> As telas do Neon mudam de vez em quando; se um botão tiver outro nome, procure o equivalente.

## 1. Criar a conta e o banco

1. Acesse **https://neon.com** e clique em **Sign up**. O jeito mais rápido é entrar com a conta do GitHub.
2. Na tela de criar projeto (**Create project**):
   - **Project name:** `fiscon`
   - **Postgres version:** a padrão (a mais nova)
   - **Region:** **AWS São Paulo (sa-east-1)** — os dados ficam no Brasil (bom para a LGPD).
     A distância até o Render não atrasa o chat: as gravações acontecem em segundo plano.
   - **Database name:** pode deixar `neondb`.
3. Clique em **Create project**.

## 2. Copiar o endereço de conexão (DATABASE_URL)

1. No painel do projeto, clique em **Connect** (ou **Connection string** no Dashboard).
2. Deixe **Connection pooling** ligado (o endereço terá `-pooler` no meio).
3. Copie o endereço inteiro. Ele tem este formato:
   ```
   postgresql://neondb_owner:SENHA@ep-xxxx-xxxx-pooler.sa-east-1.aws.neon.tech/neondb?sslmode=require
   ```
   ⚠️ Ele contém a senha do banco: **não cole em chat, issue, print ou commit.** Se vazar, use
   **Roles → Reset password** no Neon e troque nos lugares abaixo.

## 3. Colocar no Render

1. Render → seu serviço → **Environment** → **Add Environment Variable**.
2. **Key:** `DATABASE_URL` · **Value:** o endereço copiado.
3. **Save Changes**. O Render refaz o deploy sozinho.

As tabelas (`respostas` e `avaliacoes`) são criadas automaticamente na primeira resposta do chat.

## 4. Colocar no GitHub (para a avaliação semanal gravar no painel)

1. GitHub → repositório → **Settings → Secrets and variables → Actions**.
2. Aba **Secrets** → **Repository secrets** → **New repository secret**.
3. **Name:** `DATABASE_URL` · **Secret:** o mesmo endereço.

(Aproveite e confira se o secret `GROQ_API_KEY` também está lá: a avaliação semanal precisa dele.)

## 5. Conferir se funcionou

1. Abra o chat e faça uma pergunta qualquer. Vote **Útil** ou **Não ajudou**.
2. Abra `https://SEU-SERVICO.onrender.com/painel` e entre com o `STATUS_TOKEN` (Render → Environment).
   A mensagem do topo deve dizer **"Histórico dos últimos 30 dias (banco de dados)"** e a resposta deve aparecer.
3. Também dá para ver direto no Neon: **SQL Editor** →
   ```sql
   SELECT criado_em, pergunta, verificacao, voto FROM respostas ORDER BY criado_em DESC LIMIT 10;
   ```

Se o painel disser "Sem banco de dados", a variável não chegou ao servidor (confira o nome `DATABASE_URL`
e se o deploy terminou). Se disser "Não consegui ler o banco", veja `banco` > `ultimo_erro` em
`/api/status` (com o cabeçalho `X-Token`).

## Consultas úteis no SQL Editor do Neon

```sql
-- Aprovação por semana
SELECT date_trunc('week', criado_em) AS semana,
       count(*) FILTER (WHERE voto = 'util') AS util,
       count(*) FILTER (WHERE voto = 'nao_util') AS nao_util
FROM respostas GROUP BY 1 ORDER BY 1 DESC;

-- Perguntas que mais receberam "Não ajudou" (candidatas a novos casos de teste)
SELECT pergunta, comentario, verificacao, ajuste FROM respostas
WHERE voto = 'nao_util' ORDER BY votado_em DESC LIMIT 30;

-- Evolução das avaliações automáticas
SELECT criado_em, versao, aprovados, casos, percentual, reprovados FROM avaliacoes ORDER BY criado_em DESC;
```

## Apagar tudo (se precisar)

```sql
DROP TABLE respostas; DROP TABLE avaliacoes;   -- são recriadas vazias na próxima resposta
```
Ou remova a variável `DATABASE_URL` do Render para desligar o banco.
