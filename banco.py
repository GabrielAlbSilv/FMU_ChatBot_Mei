"""Banco de dados OPCIONAL do FiscOn (PostgreSQL — ex.: Neon gratuito). Passo a passo: docs/BANCO.md

Para que serve: o disco do plano gratuito do Render é apagado a cada reinício, então as métricas e os
votos 👍/👎 sumiam. Com DATABASE_URL configurada, ficam guardados:
  - respostas: um resumo de cada resposta (pergunta JÁ MASCARADA, início da resposta, se foi conferida,
    atualizada pelos sites ou corrigida pelas regras, tempo, modelo reserva) + o voto e o comentário;
  - avaliacoes: o resultado de cada rodada do avaliar_casos.py (inclusive a semanal do GitHub Actions).
O painel (/painel) lê essas tabelas.

Sem DATABASE_URL nada muda: o bot funciona só com as métricas em memória (metricas.py).

Como funciona por dentro
  - As gravações vão para uma FILA e uma thread em segundo plano grava: a resposta nunca espera o banco.
    Se o banco estiver fora do ar, o chat continua normal (a gravação é descartada e o erro vai para o log).
  - As tabelas são criadas sozinhas na 1ª conexão (CREATE TABLE IF NOT EXISTS).
  - LGPD: perguntas ficam guardadas por RETENCAO_DIAS (padrão 180) e são apagadas depois, uma vez por dia.
    Elas já chegam aqui sem CPF, CNPJ, e-mail e telefone (privacidade.py).
  - O mesmo código roda em SQLite nos testes (configurar(conectar=..., dialeto="sqlite")).
"""
import json
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import config as C

FUSO_BR = timezone(timedelta(hours=-3))
TAM_FILA = 2000

_trava = threading.RLock()
_fila = queue.Queue(maxsize=TAM_FILA)
_estado = {"conexao": None, "esquema_ok": False, "ultimo_erro": None, "gravados": 0, "descartados": 0,
           "pid": None, "ultima_limpeza": 0.0}
_cfg = {"url": C.DATABASE_URL, "conectar": None, "dialeto": "postgres", "retencao_dias": C.RETENCAO_DIAS}

ESQUEMA = [
    """CREATE TABLE IF NOT EXISTS respostas (
        id TEXT PRIMARY KEY,
        criado_em {TS_TIPO} NOT NULL,
        pergunta TEXT,
        resposta TEXT,
        verificacao TEXT,
        ajuste TEXT,
        substituida BOOLEAN,
        cache BOOLEAN,
        modelo_reserva BOOLEAN,
        temas TEXT,
        ms INTEGER,
        versao TEXT,
        voto TEXT,
        comentario TEXT,
        votado_em {TS_TIPO}
    )""",
    "CREATE INDEX IF NOT EXISTS respostas_criado_em ON respostas (criado_em)",
    "CREATE INDEX IF NOT EXISTS respostas_voto ON respostas (voto)",
    """CREATE TABLE IF NOT EXISTS avaliacoes (
        id TEXT PRIMARY KEY,
        criado_em {TS_TIPO} NOT NULL,
        versao TEXT,
        commit_git TEXT,
        modelo TEXT,
        modelo_embeddings TEXT,
        conferencia_sites BOOLEAN,
        casos INTEGER,
        aprovados INTEGER,
        percentual REAL,
        reprovados TEXT,
        origem TEXT
    )""",
]


def _log(msg):
    print(f"[BANCO] {msg}", flush=True)


def ativo():
    return bool(_cfg["url"] or _cfg["conectar"])


def configurar(url=None, conectar=None, dialeto="postgres", retencao_dias=None):
    """Troca a configuração (usado nos testes e pelo avaliar_casos.py)."""
    with _trava:
        _fechar()
        _cfg.update(url=url, conectar=conectar, dialeto=dialeto)
        if retencao_dias is not None:
            _cfg["retencao_dias"] = retencao_dias
        _estado.update(esquema_ok=False, ultimo_erro=None, ultima_limpeza=0.0)


# ------------------------------------------------------------------------------- SQL portátil
def _sql(texto):
    """Escreva o SQL com %s (parâmetro) e {TS} (parâmetro de data/hora); vira o formato de cada banco."""
    if _cfg["dialeto"] == "sqlite":
        return texto.replace("{TS_TIPO}", "TEXT").replace("{TS}", "?").replace("%s", "?")
    return texto.replace("{TS_TIPO}", "TIMESTAMPTZ").replace("{TS}", "%s::timestamptz")


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conectar():
    if _cfg["conectar"]:
        return _cfg["conectar"]()
    import psycopg   # só é importado se houver DATABASE_URL

    return psycopg.connect(_cfg["url"], autocommit=True, connect_timeout=10)


def _fechar():
    conexao = _estado.get("conexao")
    _estado["conexao"] = None
    if conexao is not None:
        try:
            conexao.close()
        except Exception:  # noqa: BLE001
            pass


def _executar(sql, parametros=(), buscar=False):
    """Executa com 1 nova tentativa (o Neon "dorme" e derruba conexões paradas: basta reconectar)."""
    for tentativa in (1, 2):
        with _trava:
            try:
                if _estado["conexao"] is None:
                    _estado["conexao"] = _conectar()
                if not _estado["esquema_ok"]:
                    cur = _estado["conexao"].cursor()
                    for comando in ESQUEMA:
                        cur.execute(_sql(comando))
                    _estado["esquema_ok"] = True
                cur = _estado["conexao"].cursor()
                cur.execute(_sql(sql), parametros)
                if buscar:
                    colunas = [c[0] for c in cur.description]
                    return [dict(zip(colunas, linha)) for linha in cur.fetchall()]
                return cur.rowcount
            except Exception as erro:  # noqa: BLE001
                _fechar()
                _estado["esquema_ok"] = False
                _estado["ultimo_erro"] = f"{type(erro).__name__}: {str(erro)[:200]}"
                if tentativa == 2:
                    raise
                time.sleep(0.5)
    return None


# ------------------------------------------------------------------------- fila em segundo plano
def _trabalhador():
    while True:
        funcao, args = _fila.get()
        try:
            funcao(*args)
            _estado["gravados"] += 1
        except Exception as erro:  # noqa: BLE001  (banco fora do ar: o chat não pode cair)
            _estado["descartados"] += 1
            _log(f"gravação descartada: {type(erro).__name__}: {str(erro)[:200]}")
        finally:
            _fila.task_done()


def _enfileirar(funcao, *args):
    if not ativo():
        return False
    import os

    with _trava:
        if _estado["pid"] != os.getpid():   # 1 thread por processo (seguro depois de um fork)
            _estado["pid"] = os.getpid()
            threading.Thread(target=_trabalhador, name="banco", daemon=True).start()
    if time.time() - _estado["ultima_limpeza"] > 24 * 3600:
        _estado["ultima_limpeza"] = time.time()
        _enfileirar_sem_limpeza(limpar_antigos)
    return _enfileirar_sem_limpeza(funcao, *args)


def _enfileirar_sem_limpeza(funcao, *args):
    try:
        _fila.put_nowait((funcao, args))
        return True
    except queue.Full:
        _estado["descartados"] += 1
        return False


def esperar(tempo=10):
    """Espera a fila esvaziar (testes e scripts)."""
    fim = time.time() + tempo
    while _fila.unfinished_tasks and time.time() < fim:
        time.sleep(0.02)
    return not _fila.unfinished_tasks


# ------------------------------------------------------------------------------- gravações
def salvar_resposta(resumo):
    """Guarda o resumo de uma resposta (em segundo plano). `resumo` vem de metricas.registrar_resposta."""
    return _enfileirar(_inserir_resposta, dict(resumo))


def _inserir_resposta(r):
    _executar(
        "INSERT INTO respostas (id, criado_em, pergunta, resposta, verificacao, ajuste, substituida, cache, "
        "modelo_reserva, temas, ms, versao) VALUES (%s, {TS}, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (r["id"], r.get("criado_em") or _agora(), r.get("pergunta", ""), r.get("resposta", ""),
         r.get("verificacao"), r.get("ajuste_regras"), bool(r.get("substituida")), bool(r.get("cache")),
         bool(r.get("modelo_reserva")), json.dumps(r.get("temas_regras") or [], ensure_ascii=False),
         int(r.get("ms") or 0), r.get("versao")))


def salvar_voto(id_resposta, voto, comentario=""):
    """Guarda o voto (em segundo plano). Funciona mesmo depois de o servidor reiniciar."""
    return _enfileirar(_atualizar_voto, id_resposta, voto, comentario)


def _atualizar_voto(id_resposta, voto, comentario):
    _executar("UPDATE respostas SET voto = %s, comentario = %s, votado_em = {TS} WHERE id = %s",
              (voto, comentario or None, _agora(), id_resposta))


def salvar_avaliacao(linha, origem="manual"):
    """Grava uma rodada do avaliar_casos.py (na hora: é chamado por script, não pelo chat)."""
    if not ativo():
        return False
    _executar(
        "INSERT INTO avaliacoes (id, criado_em, versao, commit_git, modelo, modelo_embeddings, conferencia_sites, "
        "casos, aprovados, percentual, reprovados, origem) VALUES (%s, {TS}, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4().hex, _agora(), linha.get("versao"), linha.get("commit"), linha.get("modelo"),
         linha.get("modelo_embeddings"), bool(linha.get("conferencia_sites")), int(linha.get("casos") or 0),
         int(linha.get("aprovados") or 0), float(linha.get("percentual") or 0),
         json.dumps(linha.get("reprovados") or [], ensure_ascii=False), origem))
    return True


def limpar_antigos():
    """LGPD: apaga respostas mais antigas que RETENCAO_DIAS (as avaliações, sem dado pessoal, ficam)."""
    limite = (datetime.now(timezone.utc) - timedelta(days=int(_cfg["retencao_dias"]))).isoformat(timespec="seconds")
    apagadas = _executar("DELETE FROM respostas WHERE criado_em < {TS}", (limite,))
    if apagadas:
        _log(f"{apagadas} resposta(s) com mais de {_cfg['retencao_dias']} dias apagada(s)")
    return apagadas


# ------------------------------------------------------------------------------- painel
def _para_data(valor):
    """datetime (Postgres) ou texto ISO (SQLite) -> datetime no fuso de Brasília."""
    if valor is None:
        return None
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor)
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(FUSO_BR)


def painel(dias=30):
    """Números do painel: totais, série por dia, piores respostas e histórico das avaliações."""
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat(timespec="seconds")
    linhas = _executar("SELECT criado_em, verificacao, ajuste, voto, ms, modelo_reserva, cache FROM respostas "
                       "WHERE criado_em >= {TS}", (desde,), buscar=True)
    total = len(linhas)
    util = sum(1 for l in linhas if l["voto"] == "util")
    nao = sum(1 for l in linhas if l["voto"] == "nao_util")

    def pct(condicao):
        return round(100 * sum(1 for l in linhas if condicao(l)) / total, 1) if total else 0.0

    tempos = sorted(int(l["ms"] or 0) for l in linhas)
    hoje = datetime.now(FUSO_BR).date()
    por_dia = {}
    for n in range(dias - 1, -1, -1):          # todos os dias do período, mesmo sem respostas
        dia = (hoje - timedelta(days=n)).strftime("%Y-%m-%d")
        por_dia[dia] = {"dia": dia, "respostas": 0, "util": 0, "nao_util": 0}
    for l in linhas:
        dia = _para_data(l["criado_em"]).strftime("%Y-%m-%d")
        d = por_dia.setdefault(dia, {"dia": dia, "respostas": 0, "util": 0, "nao_util": 0})
        d["respostas"] += 1
        if l["voto"] in ("util", "nao_util"):
            d[l["voto"]] += 1
    piores = _executar("SELECT id, criado_em, pergunta, resposta, comentario, verificacao, ajuste FROM respostas "
                       "WHERE voto = 'nao_util' ORDER BY votado_em DESC LIMIT 20", buscar=True)
    avaliacoes = _executar("SELECT criado_em, versao, commit_git, modelo, casos, aprovados, percentual, reprovados, "
                           "origem FROM avaliacoes ORDER BY criado_em DESC LIMIT 20", buscar=True)
    for p in piores:
        p["criado_em"] = _para_data(p["criado_em"]).strftime("%d/%m/%Y %H:%M")
    for a in avaliacoes:
        a["criado_em"] = _para_data(a["criado_em"]).strftime("%d/%m/%Y %H:%M")
        try:
            a["reprovados"] = json.loads(a["reprovados"] or "[]")
        except ValueError:
            a["reprovados"] = []
    return {
        "dias": dias,
        "totais": {
            "respostas": total, "votos": util + nao, "util": util, "nao_util": nao,
            "aprovacao": round(100 * util / (util + nao), 1) if util + nao else None,
            "conferida_confirmada": pct(lambda l: l["verificacao"] == "confirmado"),
            "atualizada_pelos_sites": pct(lambda l: l["verificacao"] == "atualizada"),
            "corrigida_pelas_regras": pct(lambda l: l["ajuste"] == "corrigida"),
            "complementada_pelas_regras": pct(lambda l: l["ajuste"] == "complementada"),
            "modelo_reserva": pct(lambda l: bool(l["modelo_reserva"])),
            "tempo_mediano_ms": tempos[len(tempos) // 2] if tempos else None,
        },
        "por_dia": [por_dia[d] for d in sorted(por_dia)],
        "piores": piores,
        "avaliacoes": avaliacoes,
    }


def status():
    return {"ativo": ativo(), "conectado": _estado["conexao"] is not None, "ultimo_erro": _estado["ultimo_erro"],
            "gravados": _estado["gravados"], "descartados": _estado["descartados"], "na_fila": _fila.qsize(),
            "retencao_dias": _cfg["retencao_dias"]}
