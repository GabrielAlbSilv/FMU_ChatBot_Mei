"""Busca nos PDFs do FiscOn: leitura dos PDFs, índice (embeddings + BM25) e recuperação de trechos.

Separado do rag.py (que cuida da conversa com o modelo) para cada arquivo ter uma responsabilidade:
  - extrair_trechos / construir_indice: PDFs -> trechos -> vetores salvos em indice/ (no build);
  - Indice.buscar_varias: busca híbrida (palavras-chave BM25 + significado), fundida por RRF;
  - juntar_vizinhos: junta trechos vizinhos e limita quantos trechos um mesmo PDF pode ocupar.
O rag.py reexporta estes nomes (rag.tokenizar, rag.juntar_vizinhos...), então nada que já usava o
rag.py precisa mudar.
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
# No plano free (CPU fracionada) o OpenBLAS tenta criar uma thread por núcleo do
# host e o import do numpy fica minutos travado.
import os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_v] = "1"

import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import unicodedata  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

import config as C  # noqa: E402

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_INDICE = PASTA_PROJETO / "indice"
PASTA_MODELO = PASTA_PROJETO / "modelo_cache"
ARQ_VETORES = PASTA_INDICE / "vetores.npy"
ARQ_TRECHOS = PASTA_INDICE / "trechos.json"
ARQ_META = PASTA_INDICE / "meta.json"
ARQ_FONTES_PDF = PASTA_PROJETO / "fontes_pdfs.json"
VERSAO_INDICE = "v5-hibrido"   # v5: ignora também o sumário "assunto ... 001" do IRPF

# Modelo de embeddings. O padrão é leve (cabe no plano free do Render), mas foi
# treinado em inglês. Para entender melhor perguntas em português, defina no .env:
#   MODELO_EMBEDDINGS=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
# (o índice é reconstruído automaticamente quando o modelo muda; usa mais RAM).
# Para comparar modelos antes de trocar: python avaliar_busca.py (mede acertos e memória).
NOME_MODELO_EMB = C.MODELO_EMBEDDINGS
TAM_CHUNK = 900
SOBREPOSICAO = 250
K = 8
LIMITE_INDICE = 8.5   # '?' por 1000 caracteres: acima disso a página é um índice de perguntas
MAX_POR_PDF = C.MAX_TRECHOS_POR_PDF   # vagas (de K) que um mesmo PDF pode ocupar; perguntas de IR: sem limite

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


def pdfs_faltando():
    """PDFs marcados como ativos em fontes_pdfs.json que NÃO estão na pasta (ex.: download falhou)."""
    try:
        manifesto = json.loads(ARQ_FONTES_PDF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    presentes = {a.name for a in listar_pdfs()}
    return sorted(f["arquivo"] for f in manifesto.get("fontes", [])
                  if isinstance(f, dict) and f.get("arquivo") and f.get("ativo", True) is not False
                  and f["arquivo"] not in presentes)


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


def _palavras_brutas(texto):
    texto = re.sub(r"\bDAS\b", " dasmei ", texto)          # DAS (imposto) != "das" (preposição)
    return [t for t in re.findall(r"[a-z0-9]+", _sem_acento(texto)) if t not in _STOP]


def _sobre_ir(brutos):
    """Pergunta sobre Imposto de Renda da PESSOA FÍSICA (e não sobre o MEI)? Nesse caso os sinônimos
    do MEI (DAS, DASN-SIMEI) puxariam a busca para o lugar errado."""
    return ("mei" not in brutos and "simei" not in brutos and "dasmei" not in brutos
            and bool({"irpf", "dirpf", "ir", "restituicao", "leao"} & set(brutos)
                     or ("imposto" in brutos and "renda" in brutos)))


def pergunta_sobre_ir(texto):
    return _sobre_ir(_palavras_brutas(texto or ""))


def tokenizar(texto, consulta=False):
    """Tokens normalizados (sem acento, radical de até 7 letras).

    Testado nos PDFs: sem bigramas e com sinônimos na consulta recupera melhor que
    com bigramas (frases genéricas como "pode ter" geravam falsos positivos).
    """
    brutos = _palavras_brutas(texto)
    if consulta:
        tabela = {**_SINONIMOS, **_SINONIMOS_IR} if _sobre_ir(brutos) else _SINONIMOS
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
        # Perguntas de IR podem (e devem) usar só o PDF do IRPF; nas demais, um PDF sozinho não
        # ocupa todas as vagas (o do IRPF tem 340 páginas e dominaria qualquer busca).
        limite = None if any(pergunta_sobre_ir(c) for c in consultas) else MAX_POR_PDF
        return juntar_vizinhos(self.trechos, ordem, k, max_por_fonte=limite)


def _colar(a, b):
    """Junta dois trechos vizinhos removendo a sobreposição (SOBREPOSICAO) entre eles."""
    amostra = b[:60]
    pos = a.rfind(amostra) if amostra else -1
    if pos >= 0 and b.startswith(a[pos:]):
        return a[:pos] + b
    return f"{a} {b}"


def juntar_vizinhos(trechos, ordem, k=K, max_por_grupo=3, max_por_fonte=None):
    """Escolhe os melhores trechos e funde os que são vizinhos no mesmo PDF.

    Os trechos se sobrepõem (SOBREPOSICAO) e, em documentos de perguntas e respostas, a
    resposta costuma continuar no trecho seguinte. Sem fundir, dois vizinhos ocupariam duas
    vagas repetindo texto; fundidos, ocupam uma vaga e liberam espaço para outro assunto.
    max_por_fonte: no máximo esse número de grupos do mesmo PDF; os que sobram só entram se
    não houver candidatos de outros PDFs para completar as `k` vagas.
    Devolve até `k` grupos, na ordem de relevância, cada um com fonte, página(s) e texto.
    """
    escolhidos, grupos, reserva = [], [], []
    for i in (int(x) for x in ordem[:k * (4 if max_por_fonte else 3)]):
        if max_por_fonte:
            teste = _agrupar(trechos, escolhidos + [i], max_por_grupo)
            mesma = sum(1 for g in teste if trechos[g[0]]["fonte"] == trechos[i]["fonte"])
            if mesma > max_por_fonte:
                reserva.append(i)
                continue
        escolhidos.append(i)
        grupos = _agrupar(trechos, escolhidos, max_por_grupo)
        if len(grupos) >= k:
            break
    for i in reserva:                      # faltou candidato de outro PDF: completa com o que sobrou
        if len(grupos) >= k:
            break
        escolhidos.append(i)
        grupos = _agrupar(trechos, escolhidos, max_por_grupo)
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
