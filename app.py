"""Servidor web do FiscOn — Chatbot MEI e Receita Federal (Flask).

- Serve a página (static/index.html, style.css, script.js)
- Expõe a API:  GET /api/status, POST /api/chat, POST /api/feedback, POST /api/voz/transcrever
- Painel de qualidade: GET /painel (a página pede a senha STATUS_TOKEN; os dados vêm de GET /api/painel)
Para testar localmente:  python app.py   ->  http://localhost:5000

Proteções: limite de perguntas por IP (IP real vindo do Cloudflare, não do X-Forwarded-For, que pode
ser falsificado) e um limite GLOBAL por minuto, que protege a cota da GROQ_API_KEY mesmo se alguém
trocar de IP. /api/status mostra só o básico; os detalhes técnicos exigem o cabeçalho X-Token.

IMPORTANTE: a inicialização do RAG roda em uma thread que é iniciada DENTRO do
processo que atende as requisições (worker do gunicorn), nunca só no import.
Threads não sobrevivem a um fork; se o app for importado no processo master
(preload), uma thread criada no import ficaria perdida e o status ficaria
eternamente "carregando".
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
import os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_v] = "1"

import faulthandler
import hashlib
import hmac
import importlib.util
import json
import re
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, Response, abort, jsonify, request, send_from_directory,
                   stream_with_context)

load_dotenv()

import banco  # noqa: E402
import config as C  # noqa: E402
import metricas  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024   # maior pedido aceito (áudio da voz incluído)

TAMANHO_MAX_PERGUNTA = C.TAMANHO_MAX_PERGUNTA
LIMITE_REQUISICOES = C.LIMITE_POR_IP      # por IP...
LIMITE_GLOBAL = C.LIMITE_GLOBAL           # ...e de todas as pessoas juntas...
JANELA_SEGUNDOS = C.JANELA_SEGUNDOS       # ...a cada N segundos

_AQUI = Path(__file__).resolve().parent
_ARQ_RAG = _AQUI / "rag.py"
_PADRAO_PDF = re.compile(r"^\d+-[\w&.-]+\.pdf$", re.IGNORECASE)   # só os PDFs numerados da base

estado = {
    "pid": None, "t0": time.time(), "thread_id": None,
    "pronto": False, "erro": None, "etapa": "aguardando primeira requisicao",
    "retriever": None, "llm": None, "rag": None,
}
_trava = threading.Lock()
_acessos = defaultdict(deque)


def _info_arquivo_rag():
    """Lê o rag.py que está NO DISCO do servidor (sem importar)."""
    try:
        texto = _ARQ_RAG.read_text(encoding="utf-8")
        m = re.search(r'VERSAO\s*=\s*"([^"]+)"', texto)
        return {
            "rag_no_disco": m.group(1) if m else "sem VERSAO (arquivo antigo?)",
            "rag_md5": hashlib.md5(texto.encode("utf-8")).hexdigest()[:8],
        }
    except Exception as e:  # noqa: BLE001
        return {"rag_no_disco": f"erro ao ler: {e}"}


def _pdfs_indexados():
    """Lista de PDFs que entraram no índice (lida do meta.json gerado no build)."""
    try:
        return json.loads((_AQUI / "indice" / "meta.json").read_text(encoding="utf-8")).get("arquivos")
    except Exception:  # noqa: BLE001
        return None


def _base_atualizada_em():
    """Data (ISO) da última vez que o atualizar_base.py trocou algum PDF; None se nunca rodou."""
    try:
        return json.loads((_AQUI / "base_status.json").read_text(encoding="utf-8")).get("atualizado_em")
    except Exception:  # noqa: BLE001
        return None


def _carregar_modulo_rag():
    """Carrega rag.py por caminho, evitando qualquer conflito de import."""
    spec = importlib.util.spec_from_file_location("rag_mei", _ARQ_RAG)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["rag_mei"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _preparar_rag():
    inicio = time.time()
    estado["thread_id"] = threading.get_ident()
    try:
        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError("Variável de ambiente GROQ_API_KEY não configurada.")

        print("[INFO] Importando bibliotecas...", flush=True)
        estado["etapa"] = "executando rag.py"
        rag = _carregar_modulo_rag()
        estado["rag"] = rag
        print(f"[INFO] Bibliotecas importadas em {time.time() - inicio:.1f}s", flush=True)

        estado["retriever"], estado["llm"] = rag.iniciar_rag()
        print(f"[INFO] Chatbot pronto em {time.time() - inicio:.1f}s", flush=True)
        _avisar_pendencias()
        estado["pronto"] = True
        _iniciar_base_web()
        faulthandler.cancel_dump_traceback_later()
    except BaseException as erro:  # noqa: BLE001
        faulthandler.cancel_dump_traceback_later()
        estado["erro"] = f"{type(erro).__name__}: {erro}"
        print(f"[ERRO] Falha ao iniciar o RAG: {erro}", flush=True)
        traceback.print_exc()


def _avisar_pendencias():
    """Lembra, nos logs, das regras fiscais que o NAF ainda não conferiu."""
    try:
        import regras_fiscais
        pendentes = regras_fiscais.pendentes_validacao()
        if pendentes:
            print(f"[AVISO] Regras fiscais ainda não validadas pelo NAF (validado_naf=false): {pendentes}",
                  flush=True)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] não consegui conferir as regras fiscais: {erro}", flush=True)


def _modulo_base_web():
    """base_web.py (coleta periódica dos sites oficiais); None se não puder ser carregado."""
    try:
        import base_web
        return base_web
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] base_web indisponível: {erro}", flush=True)
        return None


def _iniciar_base_web():
    """Liga a coleta periódica dos sites do Sebrae e da Receita Federal (não atrapalha o chat se falhar)."""
    modulo = _modulo_base_web()
    if modulo:
        try:
            modulo.iniciar()
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] base_web não iniciou: {erro}", flush=True)


def garantir_inicializacao():
    """Inicia o RAG UMA vez por processo (idempotente, seguro após fork)."""
    if estado["pid"] == os.getpid():
        return
    with _trava:
        if estado["pid"] == os.getpid():
            return
        estado.update(
            pid=os.getpid(), t0=time.time(), thread_id=None,
            pronto=False, erro=None, etapa="iniciando thread",
            retriever=None, llm=None, rag=None,
        )
        print(f"[INFO] Iniciando RAG no processo {os.getpid()}", flush=True)
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(120, repeat=True, file=sys.stderr)
        threading.Thread(target=_preparar_rag, name="init-rag", daemon=True).start()


# Se o app foi importado no master (preload), o worker recém-criado inicia o RAG.
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=garantir_inicializacao)


@app.before_request
def _antes_de_cada_requisicao():
    garantir_inicializacao()


_ultima_limpeza = [0.0]
_trava_acessos = threading.Lock()
_GLOBAL = "__todos__"


def ip_cliente():
    """IP real de quem fez o pedido.

    No Render há um Cloudflare na frente do servidor: ele preenche True-Client-IP / CF-Connecting-IP com
    o IP verdadeiro e o cliente não consegue mudar esses valores. O X-Forwarded-For NÃO é usado: o Render
    só acrescenta valores a ele, então o primeiro IP da lista pode ser inventado por quem faz o pedido
    (e burlaria o limite por IP). Sem esses cabeçalhos (ex.: rodando local), usa o IP da conexão.
    """
    for cabecalho in dict.fromkeys((C.CABECALHO_IP, "True-Client-IP", "CF-Connecting-IP")):
        valor = (request.headers.get(cabecalho) or "").strip()
        if valor and re.fullmatch(r"[0-9a-fA-F:.]{3,45}", valor):
            return valor
    return request.remote_addr or "?"


def _consumir(fila, agora, limite):
    while fila and agora - fila[0] > JANELA_SEGUNDOS:
        fila.popleft()
    return len(fila) < limite


def _excedeu_limite(ip, global_tambem=True):
    """True (e motivo) se o IP ou o servidor todo passou do limite da janela. Conta o pedido se não passou."""
    agora = time.time()
    with _trava_acessos:
        # De tempos em tempos esquece os IPs que não perguntam nada há mais de uma janela
        # (sem isso o dicionário cresceria para sempre enquanto o servidor estiver no ar).
        if agora - _ultima_limpeza[0] > 10 * JANELA_SEGUNDOS:
            for chave in [k for k, f in _acessos.items() if not f or agora - f[-1] > JANELA_SEGUNDOS]:
                del _acessos[chave]
            _ultima_limpeza[0] = agora
        fila = _acessos[ip]
        if not _consumir(fila, agora, LIMITE_REQUISICOES):
            return "ip"
        if global_tambem:
            todos = _acessos[_GLOBAL]
            if not _consumir(todos, agora, LIMITE_GLOBAL):
                return "global"
            todos.append(agora)
        fila.append(agora)
        return None


def _resposta_limite(motivo, coisa="perguntas"):
    if motivo == "global":
        metricas.registrar_erro("limite_global")
        return jsonify(erro="Muitas pessoas estão usando o chatbot agora. Tente de novo em cerca de 1 minuto."), 429
    metricas.registrar_erro("limite_ip")
    return jsonify(erro=f"Muitas {coisa} em pouco tempo. Aguarde um instante."), 429


@app.after_request
def _cabecalhos_de_seguranca(resposta):
    """Cabeçalhos básicos de proteção (não mudam nada no funcionamento das páginas)."""
    resposta.headers.setdefault("X-Content-Type-Options", "nosniff")
    resposta.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resposta.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resposta.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
    return resposta


@app.get("/")
def index():
    """Página inicial (apresentação + botão para o chat)."""
    return send_from_directory(app.static_folder, "index.html")


@app.get("/chat")
def pagina_chat():
    return send_from_directory(app.static_folder, "chat.html")


def _tem_token():
    """Pedido traz o cabeçalho X-Token certo (STATUS_TOKEN ou, se não houver, BASE_WEB_TOKEN)?"""
    token = C.STATUS_TOKEN
    return bool(token) and hmac.compare_digest(request.headers.get("X-Token", ""), token)


@app.get("/api/status")
def status():
    """Sempre 200 (serve também de health check do Render).

    Público: só o que as páginas usam (pronto/erro/etapa, documentos e data da base). Com o cabeçalho
    X-Token: detalhes técnicos (pilha da inicialização, PID, hash do código, métricas, configuração,
    PDFs faltando e regras ainda não validadas pelo NAF).
    """
    modulo = sys.modules.get("rag_mei")
    etapa = modulo.etapa_atual() if modulo and hasattr(modulo, "etapa_atual") else \
        getattr(modulo, "ETAPA", estado["etapa"])
    iniciando = not estado["pronto"] and not estado["erro"]
    web = _status_base_web()
    dados = dict(
        pronto=estado["pronto"],
        erro="indisponível" if estado["erro"] else None,
        etapa=etapa,
        segundos=int(time.time() - estado["t0"]),
        base_atualizada_em=_base_atualizada_em(),
        base_web={k: web.get(k) for k in ("ativa", "paginas", "atualizada_em")},
        documentos=[{"arquivo": a, "titulo": i.get("titulo"), "ano": i.get("ano")}
                    for a, i in sorted(getattr(modulo, "_info_docs", {}).items())],
    )
    if not _tem_token():
        return jsonify(**dados)
    dados.update(
        erro=estado["erro"],
        pid=os.getpid(),
        thread_init_viva=any(t.name == "init-rag" for t in threading.enumerate()),
        commit=C.COMMIT,
        indice_no_disco=(_AQUI / "indice" / "vetores.npy").is_file(),
        pdfs_indexados=_pdfs_indexados(),
        pdfs_faltando=_pdfs_faltando(),
        regras_nao_validadas=_regras_pendentes(),
        base_web=web,
        metricas=metricas.resumo(),
        banco=banco.status(),
        config=C.publico(),
        **_info_arquivo_rag(),
    )
    if iniciando:
        quadro = sys._current_frames().get(estado.get("thread_id"))
        if quadro is not None:
            dados["pilha_thread_init"] = "".join(traceback.format_stack(quadro)).splitlines()[-12:]
    return jsonify(**dados)


def _pdfs_faltando():
    try:
        import busca
        return busca.pdfs_faltando()
    except Exception:  # noqa: BLE001
        return None


def _regras_pendentes():
    try:
        import regras_fiscais
        return regras_fiscais.pendentes_validacao()
    except Exception:  # noqa: BLE001
        return None


def _status_base_web():
    modulo = sys.modules.get("base_web")
    return modulo.status() if modulo else {"ativa": False}


@app.post("/api/base-web/atualizar")
def atualizar_base_web():
    """Pede uma coleta dos sites oficiais agora (ex.: chamado pelo GitHub Actions).

    Só funciona se a variável BASE_WEB_TOKEN estiver definida e o pedido trouxer o cabeçalho
    X-Token com o mesmo valor. A coleta roda em segundo plano; a resposta volta na hora.
    """
    token = C.BASE_WEB_TOKEN
    if not token or not hmac.compare_digest(request.headers.get("X-Token", ""), token):
        abort(404)
    modulo = _modulo_base_web()
    if not modulo or not modulo.ATIVA:
        return jsonify(erro="base web desligada (BASE_WEB_ATIVA=0)"), 503
    threading.Thread(target=modulo.atualizar_agora, name="base-web-manual", daemon=True).start()
    return jsonify(ok=True, base_web=modulo.status()), 202


@app.post("/api/chat")
def chat():
    if estado["erro"]:
        return jsonify(erro="O chatbot não pôde ser iniciado. Tente novamente mais tarde."), 503
    if not estado["pronto"]:
        return jsonify(erro="O chatbot ainda está sendo preparado. Aguarde alguns segundos."), 503

    motivo = _excedeu_limite(ip_cliente())
    if motivo:
        return _resposta_limite(motivo)

    dados = request.get_json(silent=True) or {}
    pergunta = str(dados.get("pergunta", "")).strip()
    if not pergunta:
        return jsonify(erro="Digite uma pergunta."), 400
    if len(pergunta) > TAMANHO_MAX_PERGUNTA:
        return jsonify(erro=f"A pergunta deve ter até {TAMANHO_MAX_PERGUNTA} caracteres."), 400

    historico = dados.get("historico")
    if not isinstance(historico, list):
        historico = []
    historico = historico[-20:]   # o rag.py ainda limita e valida cada mensagem

    rag = estado["rag"]
    fluxo = rag.responder_stream(estado["retriever"], estado["llm"], pergunta, historico)
    inicio = time.time()

    def eventos():
        """Uma linha JSON por evento (NDJSON): o navegador mostra o texto aos poucos."""
        try:
            for tipo, dado in fluxo:
                if tipo in ("texto", "status"):
                    yield json.dumps({"tipo": tipo, "conteudo": dado}, ensure_ascii=False) + "\n"
                else:
                    # id da resposta: liga o voto 👍/👎 (POST /api/feedback) a esta resposta nas métricas
                    id_resposta = uuid.uuid4().hex[:16]
                    metricas.registrar_resposta(id_resposta, pergunta, dado, (time.time() - inicio) * 1000,
                                                versao=getattr(rag, "VERSAO", None))
                    yield json.dumps(dict(dado, tipo="fim", id=id_resposta), ensure_ascii=False) + "\n"
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] Ao responder: {type(erro).__name__}: {erro}", flush=True)
            metricas.registrar_erro(type(erro).__name__)
            yield json.dumps({"tipo": "erro", "mensagem": _mensagem_erro_llm(erro)},
                             ensure_ascii=False) + "\n"

    return Response(stream_with_context(eventos()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/feedback")
def feedback():
    """Voto 👍/👎 numa resposta: {"id": "...", "voto": "util" | "nao_util", "comentario": "..." (opcional)}."""
    motivo = _excedeu_limite(f"feedback:{ip_cliente()}", global_tambem=False)
    if motivo:
        return _resposta_limite(motivo, "avaliações")
    dados = request.get_json(silent=True) or {}
    id_resposta = str(dados.get("id") or "")[:32]
    voto = str(dados.get("voto") or "")
    comentario = str(dados.get("comentario") or "")[:500]
    if not re.fullmatch(r"[0-9a-f]{8,32}", id_resposta) or voto not in metricas.VOTOS:
        return jsonify(erro="Avaliação inválida."), 400
    if not metricas.registrar_feedback(id_resposta, voto, comentario):
        # resposta antiga (servidor reiniciou) ou desconhecida: não é erro para quem votou
        return jsonify(ok=False, motivo="resposta não encontrada"), 200
    return jsonify(ok=True)


@app.get("/painel")
def pagina_painel():
    """Painel de qualidade (a página em si é pública; os dados exigem a senha STATUS_TOKEN)."""
    return send_from_directory(app.static_folder, "painel.html")


@app.get("/api/painel")
def dados_painel():
    """Números do painel. Com banco: histórico de até 90 dias; sem banco: só desde o último reinício."""
    if not _tem_token():
        return jsonify(erro="Senha inválida."), 401
    try:
        dias = min(max(int(request.args.get("dias", 30)), 1), 90)
    except ValueError:
        dias = 30
    dados = {"banco": banco.ativo(), "memoria": metricas.resumo(), "banco_status": banco.status()}
    if banco.ativo():
        try:
            dados["historico"] = banco.painel(dias)
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] painel: {type(erro).__name__}: {erro}", flush=True)
            dados["erro_banco"] = "Não consegui ler o banco agora. Tente de novo em instantes."
    return jsonify(dados)


@app.post("/api/voz/transcrever")
def transcrever_voz():
    """Recebe o áudio gravado no navegador (campo "audio") e devolve {"texto": "..."}."""
    if estado["erro"]:
        return jsonify(erro="O chatbot não pôde ser iniciado. Tente novamente mais tarde."), 503
    if not estado["pronto"]:
        return jsonify(erro="O chatbot ainda está sendo preparado. Aguarde alguns segundos."), 503
    motivo = _excedeu_limite(f"voz:{ip_cliente()}")
    if motivo:
        return _resposta_limite(motivo, "gravações")

    arquivo = request.files.get("audio")
    if arquivo is None:
        return jsonify(erro="Nenhum áudio recebido."), 400
    import voz   # import tardio: o chat funciona mesmo se este módulo der problema

    conteudo = arquivo.read(voz.MAX_BYTES_AUDIO + 1)
    tipo = arquivo.mimetype or request.form.get("tipo", "")
    try:
        texto = voz.transcrever(estado["llm"], conteudo, arquivo.filename or "pergunta", tipo)
    except voz.ErroVoz as erro:
        return jsonify(erro=str(erro)), 400
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Transcrição: {type(erro).__name__}: {erro}", flush=True)
        return jsonify(erro=_mensagem_erro_llm(erro)), 502
    if not texto:
        return jsonify(erro="Não consegui entender o áudio. Tente falar mais perto do microfone."), 422
    return jsonify(texto=texto)


@app.errorhandler(413)
def _grande_demais(_erro):
    return jsonify(erro="O envio ficou grande demais. Tente um áudio mais curto."), 413


def _mensagem_erro_llm(erro):
    """Traduz erros da API da Groq em mensagens úteis para o usuário."""
    nome = type(erro).__name__
    status = getattr(erro, "status_code", None)
    if nome == "RateLimitError" or status == 429:
        return ("Muitas pessoas estão usando o chatbot agora e o limite da inteligência "
                "artificial foi atingido. Tente de novo em cerca de 1 minuto.")
    if nome in ("APIConnectionError", "APITimeoutError"):
        return "Não consegui me conectar à inteligência artificial. Verifique a conexão e tente novamente."
    if nome == "AuthenticationError" or status == 401:
        return "O chatbot está com um problema de configuração (chave da API). Avise os responsáveis."
    if status and status >= 500:
        return "O serviço de inteligência artificial está instável no momento. Tente novamente em instantes."
    return "Não foi possível gerar a resposta agora. Tente novamente."


@app.get("/documentos/<nome>")
def documento(nome):
    """Abre um dos PDFs da base (ex.: /documentos/1-Cartilha_MEI.pdf#page=7)."""
    if not _PADRAO_PDF.match(nome) or not (_AQUI / nome).is_file():
        abort(404)
    return send_from_directory(_AQUI, nome, mimetype="application/pdf", max_age=86400)


if __name__ == "__main__":
    garantir_inicializacao()
    app.run(host="0.0.0.0", port=C.PORTA, debug=False)
