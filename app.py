"""Servidor web do Chatbot MEI (Flask).

- Serve a página (static/index.html, style.css, script.js)
- Expõe a API:  GET /api/status   e   POST /api/chat
Para testar localmente:  python app.py   ->  http://localhost:5000

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
import importlib.util
import re
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

TAMANHO_MAX_PERGUNTA = 1000
LIMITE_REQUISICOES = 15      # por IP...
JANELA_SEGUNDOS = 60         # ...a cada 60 segundos

_AQUI = Path(__file__).resolve().parent
_ARQ_RAG = _AQUI / "rag.py"

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
        import json
        return json.loads((_AQUI / "indice" / "meta.json").read_text(encoding="utf-8")).get("arquivos")
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
        estado["pronto"] = True
        faulthandler.cancel_dump_traceback_later()
    except BaseException as erro:  # noqa: BLE001
        faulthandler.cancel_dump_traceback_later()
        estado["erro"] = f"{type(erro).__name__}: {erro}"
        print(f"[ERRO] Falha ao iniciar o RAG: {erro}", flush=True)
        traceback.print_exc()


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


def _excedeu_limite(ip):
    agora = time.time()
    fila = _acessos[ip]
    while fila and agora - fila[0] > JANELA_SEGUNDOS:
        fila.popleft()
    if len(fila) >= LIMITE_REQUISICOES:
        return True
    fila.append(agora)
    return False


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    # Sempre 200 (serve também de health check do Render)
    modulo = sys.modules.get("rag_mei")
    etapa = getattr(modulo, "ETAPA", estado["etapa"])
    iniciando = not estado["pronto"] and not estado["erro"]
    dados = dict(
        pronto=estado["pronto"],
        erro=estado["erro"],
        etapa=etapa,
        segundos=int(time.time() - estado["t0"]),
        pid=os.getpid(),
        thread_init_viva=any(t.name == "init-rag" for t in threading.enumerate()),
        commit=(os.getenv("RENDER_GIT_COMMIT") or "?")[:8],
        indice_no_disco=(_AQUI / "indice" / "vetores.npy").is_file(),
        pdfs_indexados=_pdfs_indexados(),
        **_info_arquivo_rag(),
    )
    if iniciando:
        quadro = sys._current_frames().get(estado.get("thread_id"))
        if quadro is not None:
            dados["pilha_thread_init"] = "".join(traceback.format_stack(quadro)).splitlines()[-12:]
    return jsonify(**dados)


@app.post("/api/chat")
def chat():
    if estado["erro"]:
        return jsonify(erro="O chatbot não pôde ser iniciado. Tente novamente mais tarde."), 503
    if not estado["pronto"]:
        return jsonify(erro="O chatbot ainda está sendo preparado. Aguarde alguns segundos."), 503

    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    if _excedeu_limite(ip):
        return jsonify(erro="Muitas perguntas em pouco tempo. Aguarde um instante."), 429

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

    try:
        resposta, fontes = estado["rag"].responder(
            estado["retriever"], estado["llm"], pergunta, historico)
        return jsonify(resposta=resposta, fontes=fontes)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Ao responder: {erro}", flush=True)
        return jsonify(erro="Não foi possível gerar a resposta agora. Tente novamente."), 500


if __name__ == "__main__":
    garantir_inicializacao()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
