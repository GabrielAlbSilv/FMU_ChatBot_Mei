"""Lógica RAG do Chatbot MEI (versão leve).

- Embeddings: fastembed (ONNX) direto, sem LangChain/Chroma
- Índice: arrays numpy salvos em disco (pasta indice/), gerados no build
- LLM: SDK oficial da Groq
Na inicialização do servidor apenas carregamos o índice (segundos, pouca RAM).
"""
import json
import os
import re
from pathlib import Path

import numpy as np

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_INDICE = PASTA_PROJETO / "indice"
PASTA_MODELO = PASTA_PROJETO / "modelo_cache"
ARQ_VETORES = PASTA_INDICE / "vetores.npy"
ARQ_TRECHOS = PASTA_INDICE / "trechos.json"

NOME_MODELO_EMB = "sentence-transformers/all-MiniLM-L6-v2"
MODELO_LLM = "openai/gpt-oss-120b"
TAM_CHUNK = 1000
SOBREPOSICAO = 200
K = 5

PADRAO_ARQUIVOS = re.compile(r"^[1-5]-.*_mei\.pdf$", re.IGNORECASE)


def listar_pdfs():
    return sorted(
        a for a in PASTA_PROJETO.iterdir()
        if a.is_file() and PADRAO_ARQUIVOS.match(a.name)
    )


def criar_embeddings():
    from fastembed import TextEmbedding

    return TextEmbedding(
        model_name=NOME_MODELO_EMB,
        cache_dir=str(PASTA_MODELO),
        threads=2,
    )


def _dividir(texto):
    """Divide o texto em trechos de ~TAM_CHUNK caracteres com sobreposição."""
    texto = re.sub(r"[ \t]+", " ", texto).strip()
    trechos, inicio = [], 0
    while inicio < len(texto):
        fim = min(inicio + TAM_CHUNK, len(texto))
        if fim < len(texto):
            corte = texto.rfind(" ", inicio + TAM_CHUNK // 2, fim)
            if corte != -1:
                fim = corte
        trecho = texto[inicio:fim].strip()
        if len(trecho) > 50:
            trechos.append(trecho)
        if fim >= len(texto):
            break
        inicio = max(fim - SOBREPOSICAO, inicio + 1)
    return trechos


def _normalizar(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return (m / n).astype(np.float32)


def construir_indice(embeddings):
    from pypdf import PdfReader

    arquivos = listar_pdfs()
    if not arquivos:
        raise FileNotFoundError("Nenhum PDF MEI (1 a 5, terminando em _MEI.pdf) na raiz do projeto.")

    trechos = []
    for arquivo in arquivos:
        print(f"[INDEX] Lendo {arquivo.name}...", flush=True)
        leitor = PdfReader(str(arquivo))
        for num, pagina in enumerate(leitor.pages, start=1):
            texto = pagina.extract_text() or ""
            for t in _dividir(texto):
                trechos.append({"fonte": arquivo.name, "pagina": num, "texto": t})

    if not trechos:
        raise RuntimeError("Nenhum texto pôde ser extraído dos PDFs.")
    print(f"[INDEX] {len(trechos)} trechos preparados. Gerando embeddings...", flush=True)

    vetores = []
    lote = 64
    for i in range(0, len(trechos), lote):
        parte = [t["texto"] for t in trechos[i:i + lote]]
        vetores.extend(list(embeddings.passage_embed(parte)))
        print(f"[INDEX] {min(i + lote, len(trechos))}/{len(trechos)}", flush=True)

    matriz = _normalizar(np.array(vetores, dtype=np.float32))
    PASTA_INDICE.mkdir(exist_ok=True)
    np.save(ARQ_VETORES, matriz)
    ARQ_TRECHOS.write_text(json.dumps(trechos, ensure_ascii=False), encoding="utf-8")
    print("[INDEX] Índice criado com sucesso.", flush=True)


class Indice:
    def __init__(self, embeddings, vetores, trechos):
        self.embeddings = embeddings
        self.vetores = vetores
        self.trechos = trechos

    def buscar(self, pergunta, k=K):
        q = np.array(list(self.embeddings.query_embed([pergunta])), dtype=np.float32)
        q = _normalizar(q)[0]
        pontos = self.vetores @ q
        melhores = np.argsort(-pontos)[:k]
        return [self.trechos[i] for i in melhores]


def iniciar_rag():
    from groq import Groq

    print("[RAG] Carregando modelo de embeddings...", flush=True)
    embeddings = criar_embeddings()

    if not (ARQ_VETORES.is_file() and ARQ_TRECHOS.is_file()):
        print("[RAG] Índice não encontrado; criando a base (pode demorar)...", flush=True)
        construir_indice(embeddings)

    print("[RAG] Carregando índice...", flush=True)
    vetores = np.load(ARQ_VETORES)
    trechos = json.loads(ARQ_TRECHOS.read_text(encoding="utf-8"))
    indice = Indice(embeddings, vetores, trechos)

    cliente = Groq(api_key=os.environ["GROQ_API_KEY"])
    return indice, cliente


def montar_prompt(contexto, pergunta):
    return f"""
Você é um assistente especializado em Microempreendedor Individual (MEI).

Responda utilizando apenas as informações
dos documentos do MEI fornecidos abaixo.
Não utilize conhecimento externo.

Se não encontrar a resposta, informe que
a informação não foi localizada nos documentos do MEI.

Contexto:
{contexto}

Pergunta:
{pergunta}
"""


def responder(indice, cliente, pergunta):
    trechos = indice.buscar(pergunta)
    contexto = "\n\n".join(f"[Fonte: {t['fonte']}]\n{t['texto']}" for t in trechos)
    r = cliente.chat.completions.create(
        model=MODELO_LLM,
        temperature=0,
        messages=[{"role": "user", "content": montar_prompt(contexto, pergunta)}],
    )
    resposta = r.choices[0].message.content
    fontes = sorted({t["fonte"] for t in trechos})
    return resposta, fontes
