"""Servidor web do Chatbot MEI (Flask).

- Serve a página (static/index.html, style.css, script.js)
- Expõe a API:  GET /api/status   e   POST /api/chat
Para testar localmente:  python app.py   ->  http://localhost:5000
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
# No plano free (CPU fracionada) o OpenBLAS tenta criar uma thread por núcleo do
# host e o import do numpy fica minutos travado.
import os as _os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ[_v] = "1"

import faulthandler
import hashlib
import importlib.util
import os
import re
import sys
import threading
import traceback
from pathlib import Path
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

TAMANHO_MAX_PERGUNTA = 1000
LIMITE_REQUISICOES = 15      # por IP...
JANELA_SEGUNDOS = 60         # ...a cada 60 segundos

estado = {"pronto": False, "erro": None, "retriever": None, "llm": None, "etapa": "iniciando", "rag": None}
_t0 = time.time()
_AQUI = Path(__file__).resolve().parent
_ARQ_RAG = _AQUI / "rag.py"


def _info_arquivo_rag():
    """Lê o rag.py que está NO DISCO do servidor (sem importar)."""
    try:
        texto = _ARQ_RAG.read_text(encoding="utf-8")
        m = re.search(r'VERSAO\s*=\s*"([^"]+)"', texto)
        return {
            "rag_no_disco": m.group(1) if m else "sem VERSAO (arquivo antigo?)",
            "rag_md5": hashlib.md5(texto.encode("utf-8")).hexdigest()[:8],
            "rag_linhas": texto.count("\n"),
        }
    except Exception as e:  # noqa: BLE001
        return {"rag_no_disco": f"erro ao ler: {e}"}


def _carregar_modulo_rag():
    """Carrega rag.py por caminho, evitando qualquer conflito de import."""
    spec = importlib.util.spec_from_file_location("rag_mei", _ARQ_RAG)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["rag_mei"] = modulo
    spec.loader.exec_module(modulo)
    return modulo

# Se a inicialização passar de 90 s, imprime nos logs onde cada thread está parada.
faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
_acessos = defaultdict(deque)

def _preparar_rag():
    """Roda em segundo plano para o servidor abrir a porta imediatamente."""
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
        import traceback

        faulthandler.cancel_dump_traceback_later()
        estado["erro"] = f"{type(erro).__name__}: {erro}"
        print(f"[ERRO] Falha ao iniciar o RAG: {erro}", flush=True)
        traceback.print_exc()

threading.Thread(target=_preparar_rag, daemon=True).start()


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
    pilha = None
    if not estado["pronto"] and not estado["erro"]:
        quadro = sys._current_frames().get(estado.get("thread_id"))
        if quadro is not None:
            pilha = "".join(traceback.format_stack(quadro)).splitlines()[-16:]
    modulo = sys.modules.get("rag_mei")
    etapa = getattr(modulo, "ETAPA", estado["etapa"])
    return jsonify(
        pronto=estado["pronto"],
        erro=estado["erro"],
        etapa=etapa,
        segundos=int(time.time() - _t0),
        rag_carregado=getattr(modulo, "VERSAO", None),
        pilha_thread_init=pilha,
        threads=[t.name for t in threading.enumerate()],
        commit=(os.getenv("RENDER_GIT_COMMIT") or "?")[:8],
        indice_no_disco=(_AQUI / "indice" / "vetores.npy").is_file(),
        modelo_cache_no_disco=(_AQUI / "modelo_cache").is_dir(),
        **_info_arquivo_rag(),
    )


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

    try:
        resposta, fontes = estado["rag"].responder(estado["retriever"], estado["llm"], pergunta)
        return jsonify(resposta=resposta, fontes=fontes)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Ao responder: {erro}", flush=True)
        return jsonify(erro="Não foi possível gerar a resposta agora. Tente novamente."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
