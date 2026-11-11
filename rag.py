"""Lógica RAG do Chatbot MEI (versão leve).

- Embeddings: fastembed (ONNX) direto, sem LangChain/Chroma
- Índice: arrays numpy salvos em disco (pasta indice/), gerados no build
- LLM: SDK oficial da Groq
Na inicialização do servidor apenas carregamos o índice (segundos, pouca RAM).
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
# No plano free (CPU fracionada) o OpenBLAS tenta criar uma thread por núcleo do
# host e o import do numpy fica minutos travado.
import os as _os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ[_v] = "1"

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

VERSAO = "rag-leve-v6"
ETAPA = "rag.py novo carregando"


def _etapa(texto):
    global ETAPA
    ETAPA = texto
    print(f"[RAG] {texto}", flush=True)


_etapa("importando numpy")
import numpy as np  # noqa: E402

_etapa("rag importado")

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_INDICE = PASTA_PROJETO / "indice"
PASTA_MODELO = PASTA_PROJETO / "modelo_cache"
ARQ_VETORES = PASTA_INDICE / "vetores.npy"
ARQ_TRECHOS = PASTA_INDICE / "trechos.json"
ARQ_META = PASTA_INDICE / "meta.json"
VERSAO_INDICE = "v4-hibrido"

NOME_MODELO_EMB = "sentence-transformers/all-MiniLM-L6-v2"
MODELO_LLM = "openai/gpt-oss-120b"
TAM_CHUNK = 900
SOBREPOSICAO = 250
K = 8
LIMITE_INDICE = 8.5   # '?' por 1000 caracteres: acima disso a página é um índice de perguntas

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


def _limpar(texto):
    """Remove ruído do PDF e cola o texto em uma linha só."""
    texto = texto.replace("\uf0b7", " ").replace("\u2022", " ").replace("\xa0", " ")
    texto = re.sub(r"(?im)^\s*\d{1,3}\s*$", " ", texto)          # números de página soltos
    texto = re.sub(r"(?i)voltar ao sum[aá]rio", " ", texto)
    texto = re.sub(r"\.{4,}", " ", texto)                          # pontilhado de sumário
    texto = re.sub(r"\s+", " ", texto)
    texto = re.sub(r"(?<=[?!])(?=[A-Za-zÀ-ÿ0-9])", " ", texto)      # "MEI?Depende" -> "MEI? Depende"
    texto = re.sub(r"(?<=[a-zà-ÿ)])\.(?=\d{1,3}\.\s)", ". ", texto)  # "sócio.38. Servidor"
    return texto.strip()


def _dividir(texto):
    """Divide em trechos de ~TAM_CHUNK caracteres. Retorna [(inicio, trecho)]."""
    trechos, inicio, n = [], 0, len(texto)
    while inicio < n:
        fim = min(inicio + TAM_CHUNK, n)
        if fim < n:
            janela = texto[inicio:fim]
            corte = max(janela.rfind(". "), janela.rfind("? "), janela.rfind("; "))
            if corte < TAM_CHUNK * 0.6:
                corte = janela.rfind(" ")
            if corte > TAM_CHUNK * 0.4:
                fim = inicio + corte + 1
        trecho = texto[inicio:fim].strip()
        if len(trecho) > 80:
            trechos.append((inicio, trecho))
        if fim >= n:
            break
        inicio = max(fim - SOBREPOSICAO, inicio + 1)
    return trechos


def extrair_trechos():
    """Lê os PDFs e devolve a lista de trechos (dicts). Não usa embeddings."""
    from pypdf import PdfReader

    arquivos = listar_pdfs()
    if not arquivos:
        raise FileNotFoundError("Nenhum PDF MEI (1 a 5, terminando em _MEI.pdf) na raiz do projeto.")

    trechos = []
    for arquivo in arquivos:
        leitor = PdfReader(str(arquivo))
        texto_total, marcas, ignoradas = "", [], 0   # marcas: (posição_inicial, página)
        for num, pagina in enumerate(leitor.pages, start=1):
            bruto = pagina.extract_text() or ""
            if bruto and (1000 * bruto.count("?") / len(bruto) > LIMITE_INDICE
                          or len(re.findall(r"\.{5,}", bruto)) >= 4):
                ignoradas += 1            # página de índice/sumário (sem conteúdo)
                continue
            limpo = _limpar(bruto)
            if not limpo:
                continue
            marcas.append((len(texto_total), num))
            texto_total += limpo + " "
        print(f"[INDEX] {arquivo.name}: {len(leitor.pages)} págs ({ignoradas} de índice ignoradas)", flush=True)

        for inicio, trecho in _dividir(texto_total):
            pagina = 1
            for pos, num in marcas:
                if pos <= inicio:
                    pagina = num
                else:
                    break
            trechos.append({"fonte": arquivo.name, "pagina": pagina, "texto": trecho})

    if not trechos:
        raise RuntimeError("Nenhum texto pôde ser extraído dos PDFs.")
    return trechos


def _normalizar(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return (m / n).astype(np.float32)


def construir_indice(embeddings):
    trechos = extrair_trechos()
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
    ARQ_META.write_text(json.dumps({"versao": VERSAO_INDICE}), encoding="utf-8")
    print("[INDEX] Índice criado com sucesso.", flush=True)


# ---------------------------------------------------------------- busca lexical
_STOP = {"de", "do", "da", "dos", "das", "a", "o", "as", "os", "e", "em", "no", "na",
         "nos", "nas", "um", "uma", "para", "por", "com", "ao", "aos", "ou", "se"}


def _sem_acento(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


_SINONIMOS = {   # termo da pergunta -> termos que aparecem nos documentos
    "funcionario": "empregado contratar", "funcionarios": "empregado contratar",
    "imposto": "dasmei tributo", "impostos": "dasmei tributo",
    "mensalidade": "dasmei", "boleto": "dasmei", "guia": "dasmei",
    "quanto": "valor", "paga": "valor pagamento", "pagar": "valor pagamento",
    "custa": "valor", "cobrar": "valor",
    "abrir": "formalizar", "registrar": "formalizar", "cadastrar": "formalizar",
    "fechar": "baixa", "cancelar": "baixa", "encerrar": "baixa",
    "teto": "limite", "faturar": "faturamento", "ganhar": "faturamento",
    "obrigado": "obrigatorio", "obrigada": "obrigatorio",
    "atividade": "ocupacoes anexo", "atividades": "ocupacoes anexo", "ocupacao": "anexo",
    "beneficio": "auxilio aposentadoria", "beneficios": "auxilio aposentadoria",
    "aposentar": "aposentadoria", "inss": "previdencia",
}


def tokenizar(texto, consulta=False):
    """Tokens normalizados (sem acento, radical de até 7 letras).

    Testado nos PDFs: sem bigramas e com sinônimos na consulta recupera melhor que
    com bigramas (frases genéricas como "pode ter" geravam falsos positivos).
    """
    texto = re.sub(r"\bDAS\b", " dasmei ", texto)          # DAS (imposto) != "das" (preposição)
    brutos = [t for t in re.findall(r"[a-z0-9]+", _sem_acento(texto)) if t not in _STOP]
    if consulta:
        for t in list(brutos):
            brutos += _SINONIMOS.get(t, "").split()
    return [(t[:-1] if len(t) > 3 and t.endswith("s") else t)[:7] for t in brutos]


class Bm25:
    def __init__(self, documentos, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.n = len(documentos)
        self.tamanhos = np.array([len(d) for d in documentos], dtype=np.float32)
        self.media = float(self.tamanhos.mean()) or 1.0
        self.postings = defaultdict(list)
        for i, doc in enumerate(documentos):
            for termo, tf in Counter(doc).items():
                self.postings[termo].append((i, tf))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5))
                    for t, p in self.postings.items()}

    def pontuar(self, consulta):
        pontos = np.zeros(self.n, dtype=np.float32)
        for termo in set(consulta):
            for i, tf in self.postings.get(termo, ()):
                norm = tf + self.k1 * (1 - self.b + self.b * self.tamanhos[i] / self.media)
                pontos[i] += self.idf[termo] * tf * (self.k1 + 1) / norm
        return pontos


def _ranking(pontos):
    ordem = np.argsort(-pontos)
    posicao = np.empty(len(pontos), dtype=np.int32)
    posicao[ordem] = np.arange(len(pontos))
    return posicao


class Indice:
    """Busca híbrida: palavras-chave (BM25) + significado (embeddings), fundidas por RRF."""

    def __init__(self, embeddings, vetores, trechos):
        self.embeddings = embeddings
        self.vetores = vetores
        self.trechos = trechos
        self.bm25 = Bm25([tokenizar(t["texto"]) for t in trechos])

    def buscar(self, pergunta, k=K, peso_lexical=1.0, peso_semantico=0.6):
        lexical = self.bm25.pontuar(tokenizar(pergunta, consulta=True))
        pos_lex = _ranking(lexical)
        placar = peso_lexical / (60 + pos_lex)
        if self.embeddings is not None and self.vetores is not None:
            q = np.array(list(self.embeddings.query_embed([pergunta])), dtype=np.float32)
            semantico = self.vetores @ _normalizar(q)[0]
            placar = placar + peso_semantico / (60 + _ranking(semantico))
        melhores = np.argsort(-placar)[:k]
        return [self.trechos[i] for i in melhores]


def iniciar_rag():
    _etapa("importando groq")
    from groq import Groq

    _etapa("carregando modelo de embeddings (fastembed)")
    embeddings = criar_embeddings()

    existe = ARQ_VETORES.is_file() and ARQ_TRECHOS.is_file() and ARQ_META.is_file()
    if existe:
        existe = json.loads(ARQ_META.read_text(encoding="utf-8")).get("versao") == VERSAO_INDICE
    _etapa(f"indice no disco: {'sim' if existe else 'NAO'}")
    if not existe:
        _etapa("criando indice a partir dos PDFs (build nao gerou)")
        construir_indice(embeddings)

    _etapa("carregando indice e montando busca por palavras-chave")
    vetores = np.load(ARQ_VETORES)
    trechos = json.loads(ARQ_TRECHOS.read_text(encoding="utf-8"))
    indice = Indice(embeddings, vetores, trechos)

    _etapa("criando cliente Groq")
    cliente = Groq(api_key=os.environ["GROQ_API_KEY"])
    _etapa("pronto")
    return indice, cliente


def montar_prompt(contexto, pergunta):
    return f"""
Você é um assistente especializado em Microempreendedor Individual (MEI).

Responda em português, utilizando apenas as informações
dos documentos do MEI fornecidos abaixo.
Não utilize conhecimento externo.

Se o contexto contiver a resposta, responda de forma direta e completa
(inclua requisitos, valores e limites quando houver). Se o contexto trouxer
só parte da resposta, responda com o que há e deixe claro o que faltou.
Se não encontrar a resposta, informe que
a informação não foi localizada nos documentos do MEI.

Contexto:
{contexto}

Pergunta:
{pergunta}
"""


def responder(indice, cliente, pergunta):
    trechos = indice.buscar(pergunta)
    contexto = "\n\n".join(f"[Fonte: {t['fonte']}, p. {t['pagina']}]\n{t['texto']}" for t in trechos)
    r = cliente.chat.completions.create(
        model=MODELO_LLM,
        temperature=0,
        messages=[{"role": "user", "content": montar_prompt(contexto, pergunta)}],
    )
    resposta = r.choices[0].message.content
    fontes = sorted({t["fonte"] for t in trechos})
    return resposta, fontes
