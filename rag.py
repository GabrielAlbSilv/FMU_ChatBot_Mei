"""Lógica RAG do FiscOn — Chatbot MEI e Receita Federal (versão leve).

- Embeddings: fastembed (ONNX) direto, sem LangChain/Chroma
- Índice: arrays numpy salvos em disco (pasta indice/), gerados no build
- LLM: SDK oficial da Groq
Na inicialização do servidor apenas carregamos o índice (segundos, pouca RAM).
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
# No plano free (CPU fracionada) o OpenBLAS tenta criar uma thread por núcleo do
# host e o import do numpy fica minutos travado.
import os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_v] = "1"

import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

VERSAO = "rag-leve-v12-regras"
ETAPA = "rag.py novo carregando"


def _etapa(texto):
    global ETAPA
    ETAPA = texto
    print(f"[RAG] {texto}", flush=True)


_etapa("importando numpy")
import numpy as np  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))   # app.py carrega este arquivo por caminho
import verificador  # noqa: E402  (conferência com Sebrae e Receita Federal)
import regras_fiscais  # noqa: E402  (contas e regras por ano-calendário: DAS, limites, IRPF...)

_etapa("rag importado")

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_INDICE = PASTA_PROJETO / "indice"
PASTA_MODELO = PASTA_PROJETO / "modelo_cache"
ARQ_VETORES = PASTA_INDICE / "vetores.npy"
ARQ_TRECHOS = PASTA_INDICE / "trechos.json"
ARQ_META = PASTA_INDICE / "meta.json"
VERSAO_INDICE = "v5-hibrido"   # v5: ignora também o sumário "assunto ... 001" do IRPF

# Modelo de embeddings. O padrão é leve (cabe no plano free do Render), mas foi
# treinado em inglês. Para entender melhor perguntas em português, defina no .env:
#   MODELO_EMBEDDINGS=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
# (o índice é reconstruído automaticamente quando o modelo muda; usa mais RAM).
NOME_MODELO_EMB = os.getenv("MODELO_EMBEDDINGS", "sentence-transformers/all-MiniLM-L6-v2")
MODELO_LLM = os.getenv("MODELO_LLM", "openai/gpt-oss-120b")
# Modelo usado só para reescrever a pergunta antes da busca. O padrão é o gpt-oss-20b, cerca de
# 2x mais rápido que o 120b e suficiente para essa tarefa curta. Se a Groq recusar o modelo
# (ex.: foi descontinuado), o bot passa a usar o MODELO_LLM sozinho.
MODELO_REESCRITA = os.getenv("MODELO_REESCRITA", "openai/gpt-oss-20b")
REESCREVER_CONSULTA = os.getenv("REESCREVER_CONSULTA", "1") != "0"
# Modelo da etapa que compara a resposta dos PDFs com os sites do Sebrae e da Receita Federal.
MODELO_VERIFICACAO = os.getenv("MODELO_VERIFICACAO", MODELO_LLM)
# Quanto o modelo "pensa" antes de responder (low | medium | high). "low" responde bem mais rápido.
ESFORCO_RESPOSTA = os.getenv("ESFORCO_RESPOSTA", "low")
# Quando conferir com os sites:
#   "depois" (padrão): a resposta aparece na hora (dos PDFs) e a conferência roda em seguida; se algo
#                      estiver desatualizado, a resposta é trocada pela versão corrigida.
#   "antes":           comportamento antigo — só mostra a resposta depois de conferida (mais lento).
CONFERENCIA_MODO = os.getenv("CONFERENCIA_MODO", "depois").strip().lower()
# Junta à resposta até N páginas oficiais recentes da base web local (base_web.py). 0 = desliga.
WEB_NA_RESPOSTA = int(os.getenv("WEB_NA_RESPOSTA", "2") or 0)
MAX_HISTORICO = 6          # mensagens anteriores (usuário + bot) enviadas ao LLM
MAX_CHARS_HISTORICO = 1500  # por mensagem
TAM_CHUNK = 900
SOBREPOSICAO = 250
K = 8
LIMITE_INDICE = 8.5   # '?' por 1000 caracteres: acima disso a página é um índice de perguntas

PADRAO_ARQUIVOS = re.compile(r"^\d+-.+\.pdf$", re.IGNORECASE)   # ex.: 6-Cartilha_Impostos.pdf


def listar_pdfs():
    return sorted(
        a for a in PASTA_PROJETO.iterdir()
        if a.is_file() and PADRAO_ARQUIVOS.match(a.name)
    )


def assinatura_pdfs():
    """Impressão digital dos PDFs da base (nome + conteúdo). Muda quando um PDF é atualizado."""
    import hashlib

    h = hashlib.sha1()
    for a in listar_pdfs():
        h.update(a.name.encode("utf-8"))
        h.update(hashlib.sha1(a.read_bytes()).digest())
    return h.hexdigest()


# ------------------------------------------------------ nomes e edições dos documentos
# Rótulo curto (chips de fonte na tela) e título completo (usado no prompt) de cada PDF.
# Os títulos de fontes_pdfs.json têm prioridade; os daqui cobrem os PDFs que não estão lá.
DOCUMENTOS_CONHECIDOS = {
    "3-Perguntas&Respostas2_MEI.pdf": ("Perguntas frequentes MEI (gov.br)",
                                       "Perguntas frequentes do Portal do Empreendedor (MEI)"),
    "4-Perguntas&Respostas3_MEI.pdf": ("P&R MEI e Simei",
                                       "Perguntas e Respostas MEI e Simei (Comitê Gestor do Simples Nacional)"),
    "5-Impostos_PF_MEI.pdf": ("P&R Imposto de Renda PF",
                              "Imposto de Renda Pessoa Física — Perguntas e Respostas (Receita Federal)"),
    "7-Perguntas&Respostas_SimplesNacional.pdf": ("P&R Simples Nacional",
                                                  "Perguntas e Respostas Simples Nacional (Comitê Gestor)"),
    "8-Exclusao_SimplesNacional_2026.pdf": ("Exclusão do Simples 2026",
                                            "Exclusão do Simples Nacional 2026 — Perguntas e Respostas (Receita Federal)"),
    "15-Roteiro_Opcao_SimplesNacional_2027.pdf": ("Roteiro opção Simples 2027",
                                                  "Roteiro da Opção pelo Simples Nacional para 2027 (Comitê Gestor)"),
}
_info_docs = {}   # arquivo -> {"rotulo", "titulo", "ano"}


def _nome_legivel(arquivo):
    """'3-Perguntas&Respostas2_MEI.pdf' -> 'Perguntas & Respostas 2 MEI'."""
    nome = re.sub(r"^\d+-|\.pdf$", "", arquivo, flags=re.IGNORECASE)
    nome = nome.replace("&", " & ").replace("_", " ")
    nome = re.sub(r"(?<=[A-Za-zÀ-ÿ])(?=\d)", " ", nome)
    return re.sub(r"\s+", " ", nome).strip()


def _ano_do_pdf(caminho):
    """Ano da edição (metadados + 3 primeiras páginas). Mesmo critério do atualizar_base.py."""
    try:
        from pypdf import PdfReader

        leitor = PdfReader(str(caminho))
        textos = [str((leitor.metadata or {}).get(c) or "") for c in ("/ModDate", "/CreationDate")]
        textos += [leitor.pages[i].extract_text() or "" for i in range(min(3, len(leitor.pages)))]
        anos = [int(a) for t in textos for a in re.findall(r"(?<!\d)(20[0-3]\d)(?!\d)", t)]
        return max(anos) if anos else None
    except Exception:  # noqa: BLE001
        return None


def carregar_info_documentos():
    """Lê títulos (fontes_pdfs.json + DOCUMENTOS_CONHECIDOS) e o ano de cada PDF da base."""
    titulos = {}
    try:
        manifesto = json.loads((PASTA_PROJETO / "fontes_pdfs.json").read_text(encoding="utf-8"))
        titulos = {f["arquivo"]: f["titulo"] for f in manifesto.get("fontes", [])
                   if isinstance(f, dict) and f.get("arquivo") and f.get("titulo")}
    except (OSError, ValueError):
        pass
    _info_docs.clear()
    for caminho in listar_pdfs():
        rotulo, titulo = DOCUMENTOS_CONHECIDOS.get(caminho.name, (None, None))
        legivel = _nome_legivel(caminho.name)
        _info_docs[caminho.name] = {
            "rotulo": rotulo or legivel,
            # o ano vai à parte (campo "ano"): tira "(edição 2026)" do título para não repetir
            "titulo": re.sub(r"\s*\(edi[cç][aã]o \d{4}\)", "", titulos.get(caminho.name) or titulo or legivel),
            "ano": _ano_do_pdf(caminho),
        }
    return _info_docs


def info_documento(arquivo):
    return _info_docs.get(arquivo) or {"rotulo": _nome_legivel(arquivo), "titulo": _nome_legivel(arquivo), "ano": None}


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
        raise FileNotFoundError("Nenhum PDF numerado (ex.: 1-Cartilha_MEI.pdf) na raiz do projeto.")

    trechos = []
    for arquivo in arquivos:
        leitor = PdfReader(str(arquivo))
        texto_total, marcas, ignoradas = "", [], 0   # marcas: (posição_inicial, página)
        for num, pagina in enumerate(leitor.pages, start=1):
            bruto = pagina.extract_text() or ""
            if bruto and (1000 * bruto.count("?") / len(bruto) > LIMITE_INDICE
                          or len(re.findall(r"\.{5,}", bruto)) >= 4
                          # sumário no formato "Assunto da pergunta 001" (P&R do IRPF)
                          or len(re.findall(r"(?m)(?:^|\s)\d{3}\s*$", bruto)) >= 10):
                ignoradas += 1            # página de índice/sumário (sem conteúdo)
                continue
            limpo = _limpar(bruto)
            if not limpo:
                continue
            marcas.append((len(texto_total), num))
            texto_total += limpo + " "
        print(f"[INDEX] {arquivo.name}: {len(leitor.pages)} págs ({ignoradas} de índice ignoradas)", flush=True)

        antes = len(trechos)
        for inicio, trecho in _dividir(texto_total):
            pagina = 1
            for pos, num in marcas:
                if pos <= inicio:
                    pagina = num
                else:
                    break
            trechos.append({"fonte": arquivo.name, "pagina": pagina, "texto": trecho})
        if len(trechos) == antes:
            print(f"[INDEX] AVISO: nenhum texto extraído de {arquivo.name} "
                  "(PDF escaneado/imagem? precisa de OCR).", flush=True)

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
    arquivos = sorted({t["fonte"] for t in trechos})
    ARQ_META.write_text(json.dumps({"versao": VERSAO_INDICE, "modelo": NOME_MODELO_EMB,
                                    "arquivos": arquivos, "assinatura": assinatura_pdfs()}), encoding="utf-8")
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
    "nota": "nota fiscal emissao", "notas": "nota fiscal emissao",
    "declaracao": "dasn simei anual", "declarar": "dasn simei anual",
    "atrasado": "atraso multa juros", "atrasada": "atraso multa juros",
    "atrasei": "atraso multa juros", "divida": "debito parcelamento",
    "dividas": "debito parcelamento", "parcelar": "parcelamento",
    "socio": "socio titular", "empresa": "cnpj", "salario": "remuneracao",
    "desenquadrar": "desenquadramento", "estourar": "excesso limite",
    "ultrapassar": "excesso limite", "passar": "excesso limite",
    # Receita Federal / Imposto de Renda da pessoa física
    "ir": "imposto renda irpf", "irpf": "imposto renda declaracao",
    "leao": "imposto renda carne", "devolucao": "restituicao", "devolver": "restituicao",
    "lote": "restituicao", "malha": "malha fina pendencia",
    "retificar": "retificadora declaracao", "corrigir": "retificadora",
    "dependente": "dependentes deducao", "dependentes": "deducao",
    "filho": "dependente", "filhos": "dependente",
    "deduzir": "deducao dedutivel", "abater": "deducao dedutivel",
    "isento": "isencao rendimentos isentos", "isenta": "isencao rendimentos isentos",
    "pendente": "pendencia regularizacao", "suspenso": "regularizacao situacao cadastral",
    "irregular": "regularizacao situacao cadastral",
    "aluguel": "alugueis rendimentos", "alugueis": "rendimentos",
    "pensao": "pensao alimenticia", "aposentado": "aposentadoria rendimentos",
    "ganho": "ganho capital", "acoes": "renda variavel", "bolsa": "renda variavel",
    "simples": "simples nacional", "microempresa": "me epp simples",
    "excluido": "exclusao", "exclusao": "excluido termo",
}


def tokenizar(texto, consulta=False):
    """Tokens normalizados (sem acento, radical de até 7 letras).

    Testado nos PDFs: sem bigramas e com sinônimos na consulta recupera melhor que
    com bigramas (frases genéricas como "pode ter" geravam falsos positivos).
    """
    texto = re.sub(r"\bDAS\b", " dasmei ", texto)          # DAS (imposto) != "das" (preposição)
    brutos = [t for t in re.findall(r"[a-z0-9]+", _sem_acento(texto)) if t not in _STOP]
    if consulta:
        # Pergunta sobre Imposto de Renda da PESSOA FÍSICA (e não sobre o MEI): os sinônimos do
        # MEI (DAS, DASN-SIMEI) puxariam a busca para o lugar errado.
        sobre_ir = ("mei" not in brutos and "simei" not in brutos and "dasmei" not in brutos
                    and bool({"irpf", "dirpf", "ir", "restituicao", "leao"} & set(brutos)
                             or ("imposto" in brutos and "renda" in brutos)))
        tabela = {**_SINONIMOS, **_SINONIMOS_IR} if sobre_ir else _SINONIMOS
        for t in list(brutos):
            brutos += tabela.get(t, "").split()
    return [(t[:-1] if len(t) > 3 and t.endswith("s") else t)[:7] for t in brutos]


_SINONIMOS_IR = {   # substituem os de _SINONIMOS quando a pergunta é sobre IR da pessoa física
    "imposto": "tributo", "impostos": "tributo",
    "declarar": "declaracao ajuste anual obrigada", "declaracao": "ajuste anual",
    "precisa": "obrigada", "obrigado": "obrigada", "obrigada": "obrigada",
    "pagar": "pagamento quota", "paga": "pagamento", "quanto": "valor",
    "atrasado": "atraso multa", "atrasada": "atraso multa", "atrasei": "atraso multa",
}


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

    def _placar(self, pergunta, peso_lexical=1.0, peso_semantico=0.6):
        lexical = self.bm25.pontuar(tokenizar(pergunta, consulta=True))
        placar = peso_lexical / (60 + _ranking(lexical))
        if self.embeddings is not None and self.vetores is not None:
            q = np.array(list(self.embeddings.query_embed([pergunta])), dtype=np.float32)
            semantico = self.vetores @ _normalizar(q)[0]
            placar = placar + peso_semantico / (60 + _ranking(semantico))
        return placar

    def buscar_varias(self, consultas, k=K):
        """Funde (RRF) os rankings de várias formulações da mesma dúvida.

        Ex.: a pergunta original do usuário + a versão reescrita pelo LLM com o
        vocabulário dos documentos. Assim o bot não depende de o usuário usar
        exatamente as mesmas palavras dos PDFs.
        """
        consultas = [c for c in dict.fromkeys(c.strip() for c in consultas if c) if c]
        if not consultas:
            return []
        placar = sum(self._placar(c) for c in consultas)
        ordem = np.argsort(-placar)
        return juntar_vizinhos(self.trechos, ordem, k)


def _colar(a, b):
    """Junta dois trechos vizinhos removendo a sobreposição (SOBREPOSICAO) entre eles."""
    amostra = b[:60]
    pos = a.rfind(amostra) if amostra else -1
    if pos >= 0 and b.startswith(a[pos:]):
        return a[:pos] + b
    return f"{a} {b}"


def juntar_vizinhos(trechos, ordem, k=K, max_por_grupo=3):
    """Escolhe os melhores trechos e funde os que são vizinhos no mesmo PDF.

    Os trechos se sobrepõem (SOBREPOSICAO) e, em documentos de perguntas e respostas, a
    resposta costuma continuar no trecho seguinte. Sem fundir, dois vizinhos ocupariam duas
    vagas repetindo texto; fundidos, ocupam uma vaga e liberam espaço para outro assunto.
    Devolve até `k` grupos, na ordem de relevância, cada um com fonte, página(s) e texto.
    """
    escolhidos, grupos = [], []
    for i in (int(x) for x in ordem[:k * 3]):
        escolhidos.append(i)
        grupos = _agrupar(trechos, escolhidos, max_por_grupo)
        if len(grupos) >= k:
            break
    saida = []
    for membros in grupos[:k]:
        texto = trechos[membros[0]]["texto"]
        for m in membros[1:]:
            texto = _colar(texto, trechos[m]["texto"])
        paginas = sorted({int(trechos[m]["pagina"]) for m in membros})
        saida.append({"fonte": trechos[membros[0]]["fonte"], "pagina": paginas[0],
                      "paginas": paginas, "texto": texto})
    return saida


def _agrupar(trechos, escolhidos, max_por_grupo):
    """Agrupa índices consecutivos do mesmo PDF; ordena os grupos pelo melhor colocado."""
    rank = {i: r for r, i in enumerate(escolhidos)}
    grupos, atual = [], []
    for i in sorted(escolhidos):
        if (atual and i == atual[-1] + 1 and len(atual) < max_por_grupo
                and trechos[i]["fonte"] == trechos[atual[-1]]["fonte"]):
            atual.append(i)
        else:
            if atual:
                grupos.append(atual)
            atual = [i]
    if atual:
        grupos.append(atual)
    return sorted(grupos, key=lambda g: min(rank[i] for i in g))


def iniciar_rag():
    _etapa("importando groq")
    from groq import Groq

    _etapa("carregando modelo de embeddings (fastembed)")
    embeddings = criar_embeddings()

    existe = ARQ_VETORES.is_file() and ARQ_TRECHOS.is_file() and ARQ_META.is_file()
    if existe:
        meta = json.loads(ARQ_META.read_text(encoding="utf-8"))
        existe = (meta.get("versao") == VERSAO_INDICE
                  and meta.get("modelo", "sentence-transformers/all-MiniLM-L6-v2") == NOME_MODELO_EMB
                  # PDF novo/atualizado desde que o índice foi gerado => refaz (base nunca fica velha)
                  and meta.get("assinatura") == assinatura_pdfs())
    _etapa(f"indice no disco: {'sim' if existe else 'NAO'}")
    if not existe:
        _etapa("criando indice a partir dos PDFs (build nao gerou)")
        construir_indice(embeddings)

    _etapa("carregando indice e montando busca por palavras-chave")
    vetores = np.load(ARQ_VETORES)
    trechos = json.loads(ARQ_TRECHOS.read_text(encoding="utf-8"))
    indice = Indice(embeddings, vetores, trechos)

    _etapa("carregando indice: identificando edicao de cada documento")
    for nome, info in carregar_info_documentos().items():
        print(f"[RAG] {nome}: {info['titulo']} (edição {info['ano'] or '?'})", flush=True)

    _etapa("criando cliente Groq")
    cliente = Groq(api_key=os.environ["GROQ_API_KEY"])
    _etapa("pronto")
    return indice, cliente


# ------------------------------------------------------------- geração de resposta
# Edite à vontade: é assim que o bot se apresenta quando perguntam quem ele é.
SOBRE_O_BOT = """\
- Nome: FiscOn: Chatbot MEI e Receita Federal (assistente virtual).
- Função: tirar dúvidas sobre dois grandes temas: (1) o Microempreendedor Individual (MEI) —
  o que é, quem pode ser, como abrir (formalizar), impostos e o boleto mensal (DAS),
  declaração anual (DASN-SIMEI), limites de faturamento, nota fiscal, contratação de
  empregado, benefícios do INSS, dívidas, desenquadramento e como dar baixa (encerrar) o
  MEI; e (2) a Receita Federal — impostos e tributos federais, CPF e CNPJ, Imposto de
  Renda da Pessoa Física, declarações, regularização e dívidas fiscais, e como usar os
  serviços do órgão. Também explica conceitos básicos de empreendedorismo e tributação
  para quem está começando (o que é imposto, CNPJ, Simples Nacional, faturamento etc.).
- Base de conhecimento: cartilhas e documentos de perguntas e respostas sobre MEI e
  sobre impostos da Receita Federal, além de conhecimento geral para explicar conceitos
  básicos.
- Atualização: visita periodicamente os sites do Sebrae e da Receita Federal (únicas fontes da
  internet aceitas), guarda as páginas e PDFs mais novos e confere cada resposta com eles; se algo
  estiver desatualizado, corrige.
- Acessibilidade: a pessoa pode perguntar falando (botão do microfone), ouvir as respostas em voz
  alta (botão "Ouvir" ou menu de acessibilidade) e ver o texto em Libras pelo VLibras, o tradutor
  oficial do governo (botão "Libras" em cada resposta).
- Atendimento humano: para casos pessoais ou mais complexos, a pessoa pode agendar atendimento
  presencial ou online com o NAF (Núcleo de Apoio Fiscal) da FMU pelo e-mail naf@fmu.br ou pelo
  formulário que aparece abaixo de cada resposta.
- Limites: é uma IA; pode errar e não substitui um contador ou os canais oficiais
  (Portal do Empreendedor — gov.br/mei, site da Receita Federal — gov.br/receitafederal, Sebrae)."""

MARCA_SUGESTOES = "@@SUGESTOES:"

SISTEMA = f"""Você é o Chatbot MEI e Receita Federal, um assistente virtual simpático, didático e preciso,
especializado em dois temas: Microempreendedor Individual (MEI) e Receita Federal
(impostos, CPF, CNPJ, declarações e serviços do órgão), com foco em tributação básica
para pequenos empreendedores e pessoas físicas brasileiras. Responda sempre em português do Brasil.

## Sobre você
{SOBRE_O_BOT}

## Como decidir de onde vem a resposta
1. **Saudações, agradecimentos e perguntas sobre você** ("oi", "obrigado", "qual sua
   função?", "o que você é?", "o que você sabe fazer?"): responda com naturalidade usando
   a seção "Sobre você". Não precisa usar os trechos dos documentos.
2. **Dúvidas específicas sobre MEI ou Receita Federal** (regras, valores, limites, prazos, procedimentos,
   direitos e obrigações): baseie-se PRIORITARIAMENTE nos trechos dos documentos
   fornecidos e cite-os. Os trechos foram recuperados por busca automática: use os que
   forem relevantes e ignore os que não forem. Interprete a pergunta pelo sentido, não
   pelas palavras exatas — o usuário pode usar palavras diferentes das do documento.
3. **Conceitos gerais** (ex.: "o que é imposto?", "o que é CNPJ?", "o que é
   faturamento?", "o que é nota fiscal?", "qual a diferença entre MEI e ME?"): explique
   com seu conhecimento geral de forma simples, com um exemplo do dia a dia quando
   ajudar, e depois conecte o conceito ao MEI ou à Receita Federal usando os documentos quando houver algo
   relacionado neles.
4. **Números que mudam com o tempo** (valores do DAS, salário mínimo, limite de
   faturamento, alíquotas, datas e prazos): se vier o bloco **FATOS CALCULADOS**, ele é a
   fonte principal: use exatamente esses valores, datas e conclusões, sem refazer contas e
   sem trocar por outro número dos trechos (os trechos podem ser de outro ano). Comece a
   resposta pela conclusão do fato (ex.: "Não, você está dispensado..." / "Sim, houve
   excesso de R$ 9.500,00..."). Sem FATOS CALCULADOS, use SOMENTE os valores dos documentos e
   diga que podem ter sido atualizados, recomendando conferir no Portal do Empreendedor. Se
   o valor não estiver nos documentos nem nos fatos, NÃO invente: diga que não encontrou o
   valor atualizado e indique onde consultar.
   Responda TODAS as partes da pergunta (ex.: "precisa pagar o DAS e entregar a declaração?"
   pede as duas respostas). Nunca faça a conta de cabeça: comparações como "34 mil é maior que
   35.584" e limites proporcionais já vêm prontos nos fatos.
5. **Assuntos fora do escopo** (sem relação com MEI, Receita Federal, empreendedorismo,
   impostos ou finanças do pequeno negócio): diga gentilmente que é especializado em MEI
   e Receita Federal e sugira o tipo de pergunta que pode responder.
6. **Pergunta vaga ou ambígua**: responda a interpretação mais provável e, se fizer
   diferença, pergunte ao final o que a pessoa quis dizer.
7. **Situação pessoal complicada** (dívidas acumuladas, malha fina, exclusão do Simples,
   desenquadramento, CPF ou CNPJ irregular): oriente o caminho geral e sugira também o
   atendimento do NAF da FMU ou de um contador para analisar o caso.

Nunca contradiga os documentos com conhecimento externo. Se as fontes não cobrem a
dúvida por completo, responda o que for possível e diga claramente o que faltou.
Cada trecho informa o documento e o ano da edição. Alguns trechos vêm de páginas oficiais
do Sebrae ou da Receita Federal coletadas recentemente ("página oficial"/"PDF oficial mais
novo"): são a informação mais atual. Se dois trechos divergirem, prefira o mais recente (em
geral a página oficial), diga de onde veio (ex.: "Segundo o site da Receita Federal, ...") e
avise que a regra mudou. Use a data de hoje (informada
junto da pergunta) para entender "este ano", "ano passado" e prazos, e diga o ano a que um
valor ou prazo se refere quando o documento informar.
Use o histórico da conversa para entender perguntas de continuação
("e se eu atrasar?", "e quanto custa?", "pode explicar melhor?").
Nunca peça senha da conta gov.br nem dados pessoais completos (CPF, CNPJ); se a pessoa
enviar, não repita esses dados na resposta. Valores que a pessoa informou para um cálculo
(faturamento, rendimentos) podem ser usados na resposta.
Os FATOS CALCULADOS não têm número de trecho: não coloque colchetes para eles.

## Citações
Os trechos vêm numerados como [1], [2], ... Ao usar uma informação de um trecho,
coloque o número dele entre colchetes logo após a frase, ex.: "O MEI pode ter um
empregado [3]." Não cite trechos que você não usou. Não invente números de trecho.
Informações de conhecimento geral ficam sem citação.

## Formato
- Vá direto ao ponto: a primeira frase já responde a pergunta (ex.: "Sim, o MEI pode ter
  um empregado."); depois vêm os detalhes.
- Linguagem simples, sem juridiquês; explique siglas na primeira vez (ex.: DAS —
  Documento de Arrecadação do Simples Nacional).
- Quando a pessoa precisar FAZER algo (emitir, declarar, abrir, regularizar, parcelar),
  termine com o próximo passo prático: onde fazer (Portal do Empreendedor, app MEI, e-CAC,
  site da Receita) e o que ter em mãos. Para sites, use o endereço curto oficial
  (ex.: gov.br/mei); a tela já mostra botões com os links dos serviços mais comuns.
- Use parágrafos curtos, **negrito** para o essencial e listas com "-" ou "1." quando
  houver passos ou requisitos. NÃO use tabelas nem títulos com "#".
- Respostas de tamanho proporcional à pergunta: saudações em 1–2 frases; dúvidas
  complexas podem ter mais detalhes.

## Perguntas de continuação
Depois da resposta, na ÚLTIMA linha, escreva exatamente neste formato:
{MARCA_SUGESTOES} pergunta curta 1 | pergunta curta 2 | pergunta curta 3
São 2 ou 3 perguntas que o usuário provavelmente faria em seguida, escritas como o
usuário escreveria (ex.: "E se eu atrasar o DAS?"), sobre MEI ou Receita Federal e que você
consegue responder. Não mencione essa linha no texto da resposta."""

PROMPT_REESCRITA = """Você prepara consultas de busca para uma base de documentos sobre MEI
(Microempreendedor Individual) e Receita Federal: cartilhas do MEI, perguntas e respostas
oficiais e cartilhas de impostos.

Reescreva a ÚLTIMA pergunta do usuário como UMA consulta de busca completa e autônoma:
- resolva referências ao histórico (ex.: "e quanto custa?" -> "quanto custa o DAS mensal do MEI");
- corrija erros de digitação e gírias;
- acrescente sinônimos e os termos técnicos que os documentos usam (ex.: DAS, DASN-SIMEI,
  CNPJ, formalização, baixa, faturamento, receita bruta, Simples Nacional, INSS,
  previdência, nota fiscal, ocupações permitidas, desenquadramento, empregado e, para a
  Receita Federal: imposto de renda, IRPF, CPF, declaração, restituição, malha fina, tributos).
Responda APENAS com a consulta, em uma linha, sem explicações."""


def _sanear_historico(historico):
    """Mantém só mensagens válidas, recentes e de tamanho limitado."""
    limpo = []
    for m in historico or []:
        if not isinstance(m, dict):
            continue
        papel = m.get("papel") or m.get("role")
        texto = str(m.get("texto") or m.get("content") or "").strip()
        if papel in ("user", "usuario"):
            papel = "user"
        elif papel in ("assistant", "bot"):
            papel = "assistant"
        else:
            continue
        if texto:
            limpo.append({"role": papel, "content": texto[:MAX_CHARS_HISTORICO]})
    return limpo[-MAX_HISTORICO:]


def _opcoes_modelo(modelo, esforco):
    """Os modelos gpt-oss aceitam 'reasoning_effort'; outros modelos não."""
    return {"reasoning_effort": esforco} if "gpt-oss" in modelo else {}


def reescrever_consulta(cliente, pergunta, historico, situacao=""):
    """Usa o LLM para transformar a pergunta numa consulta rica para a busca.

    `situacao`: dados que o usuário já informou na conversa (ex.: "MEI aberto em 07/2026;
    faturamento R$ 50.000,00"), extraídos por regras_fiscais — assim a busca não perde o mês de
    abertura quando ele foi dito duas mensagens atrás.
    Se der qualquer erro, devolve None e a busca segue só com a pergunta original.
    """
    conversa = "\n".join(
        f"{'Usuário' if m['role'] == 'user' else 'Assistente'}: {m['content'][:400]}"
        for m in historico[-4:]
    )
    entrada = (f"Histórico recente:\n{conversa}\n\n" if conversa else "") + \
        (f"Dados já informados pelo usuário: {situacao}\n\n" if situacao else "") + \
        f"Última pergunta do usuário: {pergunta}"
    global _modelo_reescrita
    for tentativa in range(2):
        modelo = _modelo_reescrita
        try:
            r = cliente.chat.completions.create(
                model=modelo,
                temperature=0,
                # folga para o raciocínio interno dos modelos gpt-oss (conta como saída)
                max_completion_tokens=700,
                messages=[{"role": "system", "content": PROMPT_REESCRITA},
                          {"role": "user", "content": entrada}],
                **_opcoes_modelo(modelo, "low"),
            )
            linhas = (r.choices[0].message.content or "").strip().splitlines()
            consulta = linhas[0].strip().strip('"') if linhas else ""
            return consulta[:500] or None
        except Exception as erro:  # noqa: BLE001
            if tentativa == 0 and modelo != MODELO_LLM and _erro_de_modelo(erro):
                print(f"[RAG] modelo de reescrita '{modelo}' indisponível; usando {MODELO_LLM}", flush=True)
                _modelo_reescrita = MODELO_LLM
                continue
            print(f"[RAG] reescrita da consulta falhou: {erro}", flush=True)
            return None
    return None


_modelo_reescrita = MODELO_REESCRITA


def _erro_de_modelo(erro):
    """Erro da Groq do tipo 'modelo não existe / foi descontinuado' (e não limite ou rede)."""
    status = getattr(erro, "status_code", None)
    texto = str(erro).lower()
    return status in (400, 404) and ("model" in texto or "decommission" in texto)


# [3]  [1, 4]  [2-3]  e também o formato 【3†...】 que alguns modelos usam
_CITACAO = re.compile(r"\s?[\[【]\s*(\d{1,2}(?:\s*[,;–-]\s*\d{1,2})*)\s*(?:†[^\]】]*)?[\]】]")


def _extrair_citacoes(resposta, total):
    """Remove marcadores [n] do texto e devolve os índices (0-based) citados."""
    citados = set()

    def trocar(m):
        grupo = m.group(1)
        numeros = [int(n) for n in re.findall(r"\d+", grupo)]
        if ("–" in grupo or "-" in grupo) and len(numeros) == 2:
            numeros = list(range(numeros[0], numeros[1] + 1))
        validos = [n for n in numeros if 1 <= n <= total]
        if not validos:
            return m.group(0)
        citados.update(n - 1 for n in validos)
        return ""

    texto = _CITACAO.sub(trocar, resposta)
    texto = re.sub(r"[ \t]+([.,;:!?])", r"\1", texto)
    return texto.strip(), citados


def _formatar_fontes(trechos):
    """[{"arquivo": "4-....pdf", "paginas": [7, 9], "rotulo": "P&R MEI e Simei", "titulo": "..."}, ...]"""
    paginas = defaultdict(set)
    for t in trechos:
        if t.get("url"):          # página oficial da base web: vai em _formatar_paginas_oficiais
            continue
        paginas[t["fonte"]].update(int(p) for p in (t.get("paginas") or [t["pagina"]]))
    saida = []
    for fonte, pags in sorted(paginas.items()):
        info = info_documento(fonte)
        saida.append({"arquivo": fonte, "paginas": sorted(pags), "rotulo": info["rotulo"],
                      "titulo": info["titulo"] + (f" (edição {info['ano']})" if info["ano"] else "")})
    return saida


def _formatar_paginas_oficiais(trechos):
    """Páginas dos sites oficiais usadas na resposta: [{"url","titulo","site","data"}]."""
    vistas, saida = set(), []
    for t in trechos:
        if t.get("url") and t["url"] not in vistas and verificador.url_permitida(t["url"]):
            vistas.add(t["url"])
            saida.append({k: t.get(k, "") for k in ("url", "titulo", "site", "data")})
    return saida


# ----------------------------------------------------- atalhos para o Portal oficial
# (regex aplicada ao texto sem acentos, rótulo do botão, link oficial, ícone, só na pergunta?)
# "só na pergunta" = o atalho só aparece se a PESSOA falou do assunto (evita botões de IR em
# respostas sobre DAS que apenas mencionam o Imposto de Renda, por exemplo).
# O ícone é um nome que o script.js transforma em desenho (ver ICONES_ACAO).
ACOES_PORTAL = [
    (r"\b(emit|gera|pag|imprim)\w*\b.{0,40}\b(das|boleto|guia)\b|\bpgmei\b",
     "Emitir o DAS (boleto mensal)",
     "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATSPO/pgmei.app/Identificacao",
     "boleto", False),
    (r"\bdasn\b|declaracao anual",
     "Fazer a declaração anual (DASN-SIMEI)",
     "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATSPO/dasnsimei.app/Identificacao",
     "declaracao", False),
    (r"\b(abrir|formaliz\w*|virar|ser|tornar)\b.{0,25}\bmei\b|quero ser mei|inscricao como mei",
     "Abrir meu MEI",
     "https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/quero-ser-mei",
     "abrir", False),
    (r"\bbaixa\b|\bencerr\w*|\bfechar\b.{0,25}\b(mei|empresa|cnpj)\b",
     "Serviços do MEI (inclui dar baixa)",
     "https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/servicos-para-mei",
     "servicos", False),
    (r"nota fiscal de servico|nfs-?e|\bemit\w*\b.{0,20}\bnota",
     "Emissor de nota fiscal de serviço",
     "https://www.nfse.gov.br/EmissorNacional",
     "nota", False),
    (r"\bparcel\w*|\bdividas?\b|\bdebitos?\b",
     "Serviços do MEI no Simples Nacional (parcelamento)",
     "https://www8.receita.fazenda.gov.br/SimplesNacional/Servicos/Grupo.aspx?grp=8",
     "parcelar", True),
    (r"imposto de renda|\b(irpf|dirpf)\b|\bir\b.{0,20}\b(declar|restitu|pagar|devo)|restituicao|malha fina",
     "Meu Imposto de Renda (Receita Federal)",
     "https://www.gov.br/receitafederal/pt-br/assuntos/meu-imposto-de-renda",
     "leao", True),
    (r"\bcpf\b.{0,40}\b(situacao|regular\w*|pendente|pendencia|suspens\w*|cancelad\w*|consult\w*|comprovante)"
     r"|\b(situacao|regulariz\w*|consult\w*|comprovante)\b.{0,30}\bcpf\b",
     "Consultar a situação do CPF",
     "https://servicos.receita.fazenda.gov.br/servicos/cpf/consultasituacao/consultapublica.asp",
     "cpf", True),
    (r"\b(cartao|comprovante|consult\w*|situacao)\b.{0,30}\bcnpj\b|\bcnpj\b.{0,30}\b(cartao|comprovante|situacao)\b",
     "Emitir o comprovante do CNPJ",
     "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/cnpjreva_Solicitacao.asp",
     "cnpj", True),
]
MAX_ACOES = 2


def detectar_acoes(pergunta, resposta):
    so_pergunta = _sem_acento(pergunta)
    tudo = _sem_acento(f"{pergunta} {resposta}")
    acoes = []
    for padrao, rotulo, url, icone, apenas_pergunta in ACOES_PORTAL:
        if re.search(padrao, so_pergunta if apenas_pergunta else tudo):
            acoes.append({"rotulo": rotulo, "url": url, "icone": icone})
        if len(acoes) >= MAX_ACOES:
            break
    return acoes


# "@@SUGESTOES:" (formato pedido) e também "SUGESTÕES:" / "Sugestões de perguntas:" no fim,
# caso o modelo esqueça o "@@".
_LINHA_SUGESTOES = re.compile(r"(?:^|\n)[ \t>*_]*(?:@@\s*)?sugest(?:oes|ões)[^:\n]{0,30}:", re.IGNORECASE)


def _separar_sugestoes(texto):
    """Tira a linha '@@SUGESTOES: a | b | c' do fim da resposta."""
    pos = texto.find("@@")
    if pos < 0:
        achados = list(_LINHA_SUGESTOES.finditer(texto))
        # só aceita sem "@@" se estiver no finalzinho (evita cortar um parágrafo legítimo)
        if not achados or len(texto) - achados[-1].start() > 400:
            return texto.strip(), []
        pos = achados[-1].start()
    cauda = texto[pos:].split(":", 1)[-1]
    sugestoes = []
    for s in re.split(r"\s*\|\s*|\n", cauda):
        s = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", s.strip(" *\"'")).strip(" *\"'")
        if 5 <= len(s) <= 120 and s not in sugestoes:
            sugestoes.append(s)
    return texto[:pos].strip(), sugestoes[:3]


# -------------------------------------------------------------------- cache
# Perguntas sem contexto (início de conversa) se repetem muito: "quem pode ser MEI?".
# Guardamos a resposta pronta para não gastar uma chamada da Groq de novo.
CACHE_MAX = 200
CACHE_VALIDADE = 24 * 3600   # segundos
_cache = {}   # chave -> (momento, resultado)


def _chave_cache(pergunta):
    return " ".join(re.findall(r"[a-z0-9]+", _sem_acento(pergunta)))


def _cache_ler(chave):
    item = _cache.get(chave)
    if item and time.time() - item[0] < CACHE_VALIDADE:
        return item[1]
    _cache.pop(chave, None)
    return None


def _cache_gravar(chave, resultado):
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))   # remove o mais antigo
    _cache[chave] = (time.time(), resultado)


# -------------------------------------------------------------------- geração
def _cabecalho_trecho(t):
    if t.get("url"):                                   # página oficial (base web)
        quando = f", publicada/atualizada em {t['data']}" if t.get("data") else ""
        tipo = "PDF oficial mais novo" if t.get("tipo") == "pdf" else "página oficial"
        return f"(Fonte: {tipo} — site {t.get('site', '')}: {t.get('titulo', '')}{quando})"
    info = info_documento(t["fonte"])
    pags = t.get("paginas") or [t["pagina"]]
    if len(pags) == 1:
        paginas = f"p. {pags[0]}"
    elif pags[-1] - pags[0] == len(pags) - 1:          # páginas seguidas: "p. 7–8"
        paginas = f"p. {pags[0]}–{pags[-1]}"
    else:                                              # com salto (ex.: sumário ignorado no meio)
        paginas = "p. " + ", ".join(map(str, pags))
    edicao = f", edição {info['ano']}" if info["ano"] else ""
    return f"(Fonte: {info['titulo']}{edicao}, {paginas})"


def _hoje():
    return datetime.now(verificador.FUSO_BR).strftime("%d/%m/%Y")


def montar_mensagens(trechos, pergunta, historico, fatos=None, situacao=""):
    if trechos:
        contexto = "\n\n".join(f"[{i}] {_cabecalho_trecho(t)}\n{t['texto']}"
                               for i, t in enumerate(trechos, start=1))
        bloco = f"Trechos recuperados dos documentos sobre MEI e Receita Federal:\n{contexto}"
    elif fatos:
        bloco = "(Nenhum trecho de documento foi encontrado; responda com os FATOS CALCULADOS acima.)"
    else:
        bloco = ("(Nenhum trecho de documento foi buscado: a mensagem parece ser uma saudação, "
                 "agradecimento ou pergunta sobre você. Responda com base na seção \"Sobre você\".)")
    if fatos:   # os fatos vêm ANTES dos trechos: têm prioridade
        bloco = f"{regras_fiscais.bloco_prompt(fatos)}\n\n{bloco}"
    dados = f"\nDados já informados pelo usuário nesta conversa: {situacao}" if situacao else ""
    usuario = f"{bloco}\n\n---\nData de hoje: {_hoje()}{dados}\nPergunta do usuário: {pergunta}"
    return ([{"role": "system", "content": SISTEMA}] + historico +
            [{"role": "user", "content": usuario}])


# ------------------------------------------------- conversa simples (sem busca nos PDFs)
# "oi", "obrigado", "quem é você?"... não precisam de busca nem de conferência nos sites:
# pulamos a reescrita da consulta (1 chamada a menos na Groq) e a resposta sai mais rápido.
_PALAVRAS_CONVERSA = set("""
oi oii oiii ola opa eai e ai hey hi hello alo bom boa dia tarde noite tudo bem bom beleza blz
obrigado obrigada obg brigado brigada valeu vlw grato grata agradeco muito pela pelo ajuda ajudou
ok okay certo entendi show perfeito otimo legal massa top tchau ate mais logo flw abraco
quem voce vc e o que a um uma faz sabe fazer pode me ajudar qual sua seu funcao nome como funciona
se chama fiscon bot chatbot robo ia assistente virtual ser humano pessoa real
""".split())
_ANCORAS_CONVERSA = set("""
oi oii oiii ola opa eai hey hi hello alo dia tarde noite obrigado obrigada obg brigado brigada valeu
vlw grato grata agradeco ok okay entendi tchau flw voce vc fiscon bot chatbot robo assistente nome
""".split())


def eh_conversa_simples(pergunta):
    palavras = re.findall(r"[a-z]+", _sem_acento(pergunta))
    return (0 < len(palavras) <= 10 and all(p in _PALAVRAS_CONVERSA for p in palavras)
            and any(p in _ANCORAS_CONVERSA for p in palavras))


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conferencia")


def trechos_da_web(consulta, k=WEB_NA_RESPOSTA):
    """Páginas oficiais recentes da base web local, no mesmo formato dos trechos dos PDFs."""
    if k <= 0:
        return []
    try:
        import base_web
        achados = base_web.buscar(consulta, k=k, min_termos=3, limite_chars=1600)
    except Exception as erro:  # noqa: BLE001
        print(f"[RAG] base web indisponível: {erro}", flush=True)
        return []
    return [{"fonte": "web", "pagina": 0, "url": a["url"], "titulo": a["titulo"], "site": a["site"],
             "data": a.get("data", ""), "tipo": a.get("tipo", "pagina"), "texto": a["texto"]} for a in achados]


def _gerar_texto(cliente, mensagens):
    """Chama o modelo em streaming. Produz ("texto", pedaço) e, no fim, ("completo", texto_inteiro)."""
    fluxo = cliente.chat.completions.create(
        model=MODELO_LLM,
        temperature=0.2,
        stream=True,
        messages=mensagens,
        **_opcoes_modelo(MODELO_LLM, ESFORCO_RESPOSTA),
    )
    completo, enviado = "", 0
    for pedaco in fluxo:
        if not pedaco.choices:
            continue
        completo += pedaco.choices[0].delta.content or ""
        # Não envia a linha de sugestões (começa com "@@") nem um "@" que pode iniciá-la.
        pos = completo.find("@@")
        limite = pos if pos >= 0 else len(completo) - (1 if completo.endswith("@") else 0)
        if limite > enviado:
            yield "texto", completo[enviado:limite]
            enviado = limite
    yield "completo", completo


def responder_stream(indice, cliente, pergunta, historico=None):
    """Gera a resposta aos poucos.

    Produz tuplas:
      ("status", mensagem) -> etapa em andamento ("Consultando os documentos…", "Conferindo…")
      ("texto", pedaço)   -> trecho novo da resposta (pode conter marcadores [n])
      ("fim", resultado)  -> {"resposta", "fontes", "paginas_oficiais", "acoes", "sugestoes",
                              "verificacao", "fatos", "ajuste_regras", "substituida", "cache"}
    Erros da Groq (ex.: limite de uso) são propagados como exceção.

    Regras fiscais: antes de chamar o modelo, regras_fiscais.py calcula os fatos da pergunta
    (limites, DAS, IRPF, desenquadramento...) com TODO o histórico do usuário — não só as últimas
    mensagens —, e no fim a resposta é conferida com esses fatos (ver _aplicar_regras).

    Velocidade: a coleta das páginas oficiais para a conferência começa EM PARALELO com a
    resposta; no modo "depois" (padrão) a pessoa já lê a resposta enquanto ela é conferida.
    """
    historico_bruto = historico if isinstance(historico, list) else []
    historico = _sanear_historico(historico_bruto)
    conversa = eh_conversa_simples(pergunta)
    fatos, situacao = [], ""
    if not conversa:
        try:
            fatos = regras_fiscais.fatos_para(pergunta, historico_bruto)
            situacao = regras_fiscais.resumo_situacao(regras_fiscais.extrair_situacao(pergunta, historico_bruto))
        except Exception as erro:  # noqa: BLE001  (regra quebrada não pode derrubar o chat)
            print(f"[RAG] regras fiscais falharam: {type(erro).__name__}: {erro}", flush=True)
            fatos, situacao = [], ""
        if fatos:
            print(f"[RAG] fatos calculados: {[f['tema'] for f in fatos]} | situação: {situacao}", flush=True)

    # Respostas com fatos dependem dos valores informados: não usam o cache.
    chave = None if (historico or fatos) else _chave_cache(pergunta)
    if chave:
        pronto = _cache_ler(chave)
        if pronto:
            yield "texto", pronto["resposta"]
            yield "fim", dict(pronto, cache=True)
            return

    consultas = [pergunta]
    if conversa:
        trechos = []
        print(f"[RAG] conversa simples (sem busca): {pergunta!r}", flush=True)
    else:
        reescrita = (reescrever_consulta(cliente, pergunta, historico, situacao)
                     if REESCREVER_CONSULTA else None)
        if reescrita:
            consultas.append(reescrita)
        elif historico or situacao:
            # Sem reescrita: junta a última pergunta do usuário e os dados já informados.
            anteriores = [m["content"] for m in historico if m["role"] == "user"]
            consultas.append(" ".join(filter(None, [anteriores[-1] if anteriores else "", situacao, pergunta])))
        print(f"[RAG] consultas: {consultas}", flush=True)
        trechos = indice.buscar_varias(consultas) + trechos_da_web(consultas[-1])

    conferir = verificador.ATIVO and not conversa
    # Começa já a juntar as páginas oficiais (base local -> internet), enquanto o modelo escreve.
    coleta = _executor.submit(verificador.coletar_trechos, consultas[-1]) if conferir else None
    mensagens = montar_mensagens(trechos, pergunta, historico, fatos, situacao)

    if conferir and CONFERENCIA_MODO == "antes":
        yield from _responder_com_conferencia(cliente, trechos, consultas, pergunta, mensagens, chave, coleta, fatos)
        return

    completo = ""
    for tipo, dado in _gerar_texto(cliente, mensagens):
        if tipo == "texto":
            yield "texto", dado
        else:
            completo = dado

    texto, sugestoes = _separar_sugestoes(completo)
    resposta, citados = _extrair_citacoes(texto, len(trechos))
    verificacao, substituida = {"status": "nao_aplicavel"}, False
    if conferir and resposta and verificador.deve_verificar(pergunta, bool(citados)):
        yield "status", "Conferindo com o Sebrae e a Receita Federal…"
        resposta, citados, verificacao = _conferir(cliente, pergunta, consultas, resposta, citados, trechos,
                                                   coleta, fatos)
        substituida = verificacao["status"] == "atualizada"
    elif coleta:
        coleta.cancel()
    resposta, citados, ajuste = _aplicar_regras(resposta, citados, fatos)
    substituida = substituida or ajuste == "corrigida"
    yield "fim", dict(_montar_resultado(pergunta, resposta, citados, trechos, sugestoes, verificacao, chave,
                                        fatos, ajuste),
                      substituida=substituida, cache=False)


_MARCA_FATO = re.compile(r"\s?\[\s*F\d{1,2}\s*\]")


def _aplicar_regras(resposta, citados, fatos):
    """Confere a resposta com os fatos calculados. Devolve (resposta, citados, ajuste).

    - faltou um valor-chave (ex.: R$ 86,05)  -> acrescenta "Conferência automática" ("complementada");
    - contradiz a regra (ex.: "precisa declarar" com R$ 34 mil; efeito em 01/01/2027 com excesso
      acima de 20%; "não encontrei")         -> troca pela resposta montada com os fatos ("corrigida").
    """
    resposta = _MARCA_FATO.sub("", resposta or "")
    if not fatos:
        return resposta, citados, None
    final, ajuste, motivos = regras_fiscais.garantir(resposta, fatos)
    if ajuste:
        print(f"[RAG] resposta {ajuste.upper()} pelas regras fiscais: {motivos}", flush=True)
    if ajuste == "corrigida":
        citados = set()            # a resposta nova vem só dos fatos (as fontes deles vão em "fatos")
    return final, citados, ajuste


def _conferir(cliente, pergunta, consultas, resposta, citados, trechos, coleta, fatos=None):
    """Compara a resposta com as páginas oficiais. Devolve (resposta, citados, verificacao)."""
    trechos_web = None
    if coleta is not None:
        try:
            trechos_web = coleta.result(timeout=verificador.TEMPO_TOTAL + 10)
        except Exception as erro:  # noqa: BLE001  (tempo esgotado, rede...)
            print(f"[RAG] coleta paralela falhou: {type(erro).__name__}", flush=True)
            trechos_web = []
    verificacao = verificador.verificar(
        cliente, MODELO_VERIFICACAO, _opcoes_modelo(MODELO_VERIFICACAO, "low"),
        pergunta, consultas[-1], resposta, trechos, trechos_web=trechos_web, fatos=fatos)
    if verificacao["status"] == "atualizada":
        print(f"[RAG] resposta ATUALIZADA pelos sites: {verificacao['divergencias']}", flush=True)
        resposta, citados = _extrair_citacoes(verificacao.pop("resposta"), len(trechos))
    verificacao.pop("resposta", None)
    return resposta, citados, verificacao


def _montar_resultado(pergunta, resposta, citados, trechos, sugestoes, verificacao, chave,
                      fatos=None, ajuste=None):
    usados = [trechos[i] for i in sorted(citados)]
    resultado = {
        "resposta": resposta,
        "fontes": _formatar_fontes(usados),
        "paginas_oficiais": _formatar_paginas_oficiais(usados),
        "acoes": detectar_acoes(pergunta, resposta),
        "sugestoes": sugestoes,
        "verificacao": verificacao,
        # regras oficiais usadas no cálculo (a tela mostra o selo e o link da fonte de cada uma)
        "fatos": regras_fiscais.fatos_publicos(fatos or []),
        "ajuste_regras": ajuste,
    }
    # Não guarda no cache quando os sites estavam fora do ar: da próxima vez tenta de novo.
    if chave and resposta and verificacao.get("status") != "indisponivel":
        _cache_gravar(chave, resultado)
    return resultado


def _responder_com_conferencia(cliente, trechos, consultas, pergunta, mensagens, chave, coleta=None, fatos=None):
    """Modo "antes": rascunho (PDFs) -> comparação com Sebrae/Receita Federal -> resposta final.

    O rascunho NÃO é mostrado: o usuário só vê a versão já conferida (ou a dos PDFs, se não
    foi possível conferir). Eventos extras ("status") deixam a tela informar cada etapa.
    """
    yield "status", "Consultando os documentos…"
    r = cliente.chat.completions.create(
        model=MODELO_LLM,
        temperature=0.2,
        stream=False,
        messages=mensagens,
        **_opcoes_modelo(MODELO_LLM, ESFORCO_RESPOSTA),
    )
    completo = r.choices[0].message.content or ""
    texto, sugestoes = _separar_sugestoes(completo)
    resposta, citados = _extrair_citacoes(texto, len(trechos))
    verificacao = {"status": "nao_aplicavel"}

    if resposta and verificador.deve_verificar(pergunta, bool(citados)):
        yield "status", "Conferindo com o Sebrae e a Receita Federal…"
        resposta, citados, verificacao = _conferir(cliente, pergunta, consultas, resposta, citados, trechos,
                                                   coleta, fatos)
    elif coleta is not None:
        coleta.cancel()
    resposta, citados, ajuste = _aplicar_regras(resposta, citados, fatos)

    yield "texto", resposta
    yield "fim", dict(_montar_resultado(pergunta, resposta, citados, trechos, sugestoes, verificacao, chave,
                                        fatos, ajuste),
                      substituida=False, cache=False)


def responder(indice, cliente, pergunta, historico=None):
    """Versão sem streaming (útil para testes): devolve o resultado final."""
    for tipo, dado in responder_stream(indice, cliente, pergunta, historico):
        if tipo == "fim":
            return dado
    return None
