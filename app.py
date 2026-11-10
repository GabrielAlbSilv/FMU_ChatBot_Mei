"""Servidor web do Chatbot MEI (Flask).

- Serve a página (static/index.html, style.css, script.js)
- Expõe a API:  GET /api/status   e   POST /api/chat
Para testar localmente:  python app.py   ->  http://localhost:5000
"""
import os
import threading
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

TAMANHO_MAX_PERGUNTA = 1000
LIMITE_REQUISICOES = 15      # por IP...
JANELA_SEGUNDOS = 60         # ...a cada 60 segundos

estado = {"pronto": False, "erro": None, "retriever": None, "llm": None}
_acessos = defaultdict(deque)

def _preparar_rag():
    """Roda em segundo plano para o servidor abrir a porta imediatamente."""
    inicio = time.time()
    try:
        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError("Variável de ambiente GROQ_API_KEY não configurada.")

        print("[INFO] Importando bibliotecas...", flush=True)
        import rag
        print(f"[INFO] Bibliotecas importadas em {time.time() - inicio:.1f}s", flush=True)

        estado["retriever"], estado["llm"] = rag.iniciar_rag()
        print(f"[INFO] Chatbot pronto em {time.time() - inicio:.1f}s", flush=True)
        estado["pronto"] = True
    except Exception as erro:  # noqa: BLE001
        estado["erro"] = str(erro)
        print(f"[ERRO] Falha ao iniciar o RAG: {erro}", flush=True)

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
    return jsonify(pronto=estado["pronto"], erro=estado["erro"])


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
        import rag

        resposta, fontes = rag.responder(estado["retriever"], estado["llm"], pergunta)
        return jsonify(resposta=resposta, fontes=fontes)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Ao responder: {erro}", flush=True)
        return jsonify(erro="Não foi possível gerar a resposta agora. Tente novamente."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
