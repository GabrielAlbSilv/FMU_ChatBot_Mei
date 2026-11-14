"""Lógica RAG do Chatbot MEI (versão leve).

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
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

VERSAO = "rag-leve-v8"
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

# Modelo de embeddings. O padrão é leve (cabe no plano free do Render), mas foi
# treinado em inglês. Para entender melhor perguntas em português, defina no .env:
#   MODELO_EMBEDDINGS=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
# (o índice é reconstruído automaticamente quando o modelo muda; usa mais RAM).
NOME_MODELO_EMB = os.getenv("MODELO_EMBEDDINGS", "sentence-transformers/all-MiniLM-L6-v2")
MODELO_LLM = os.getenv("MODELO_LLM", "openai/gpt-oss-120b")
# Modelo usado só para reescrever a pergunta antes da busca (pode ser um menor/mais rápido).
MODELO_REESCRITA = os.getenv("MODELO_REESCRITA", MODELO_LLM)
REESCREVER_CONSULTA = os.getenv("REESCREVER_CONSULTA", "1") != "0"
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
                          or len(re.findall(r"\.{5,}", bruto)) >= 4):
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
                                    "arquivos": arquivos}), encoding="utf-8")
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
        placar = sum(self._placar(c) for c in consultas)
        melhores = np.argsort(-placar)[:k]
        return [self.trechos[i] for i in melhores]


def iniciar_rag():
    _etapa("importando groq")
    from groq import Groq

    _etapa("carregando modelo de embeddings (fastembed)")
    embeddings = criar_embeddings()

    existe = ARQ_VETORES.is_file() and ARQ_TRECHOS.is_file() and ARQ_META.is_file()
    if existe:
        meta = json.loads(ARQ_META.read_text(encoding="utf-8"))
        existe = (meta.get("versao") == VERSAO_INDICE
                  and meta.get("modelo", "sentence-transformers/all-MiniLM-L6-v2") == NOME_MODELO_EMB)
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


# ------------------------------------------------------------- geração de resposta
# Edite à vontade: é assim que o bot se apresenta quando perguntam quem ele é.
SOBRE_O_BOT = """\
- Nome: Chatbot MEI (assistente virtual).
- Função: tirar dúvidas sobre o Microempreendedor Individual (MEI) — o que é, quem
  pode ser, como abrir (formalizar), impostos e o boleto mensal (DAS), declaração
  anual (DASN-SIMEI), limites de faturamento, nota fiscal, contratação de empregado,
  benefícios do INSS, dívidas, desenquadramento e como dar baixa (encerrar) o MEI.
  Também explica conceitos básicos de empreendedorismo e tributação para quem está
  começando (o que é imposto, CNPJ, Simples Nacional, faturamento etc.).
- Base de conhecimento: cartilhas e documentos de perguntas e respostas sobre MEI e
  impostos, além de conhecimento geral para explicar conceitos básicos.
- Limites: é uma IA; pode errar e não substitui um contador ou os canais oficiais
  (Portal do Empreendedor — gov.br/mei, Receita Federal, Sebrae)."""

SISTEMA = f"""Você é o Chatbot MEI, um assistente virtual simpático, didático e preciso,
especializado em Microempreendedor Individual (MEI) e em tributação básica para
pequenos empreendedores brasileiros. Responda sempre em português do Brasil.

## Sobre você
{SOBRE_O_BOT}

## Como decidir de onde vem a resposta
1. **Saudações, agradecimentos e perguntas sobre você** ("oi", "obrigado", "qual sua
   função?", "o que você é?", "o que você sabe fazer?"): responda com naturalidade usando
   a seção "Sobre você". Não precisa usar os trechos dos documentos.
2. **Dúvidas específicas sobre MEI** (regras, valores, limites, prazos, procedimentos,
   direitos e obrigações): baseie-se PRIORITARIAMENTE nos trechos dos documentos
   fornecidos e cite-os. Os trechos foram recuperados por busca automática: use os que
   forem relevantes e ignore os que não forem. Interprete a pergunta pelo sentido, não
   pelas palavras exatas — o usuário pode usar palavras diferentes das do documento.
3. **Conceitos gerais** (ex.: "o que é imposto?", "o que é CNPJ?", "o que é
   faturamento?", "o que é nota fiscal?", "qual a diferença entre MEI e ME?"): explique
   com seu conhecimento geral de forma simples, com um exemplo do dia a dia quando
   ajudar, e depois conecte o conceito ao MEI usando os documentos quando houver algo
   relacionado neles.
4. **Números que mudam com o tempo** (valores do DAS, salário mínimo, limite de
   faturamento, alíquotas, datas e prazos): use SOMENTE os valores dos documentos e diga
   que podem ter sido atualizados, recomendando conferir no Portal do Empreendedor. Se
   o valor não estiver nos documentos, NÃO invente: diga que não encontrou o valor
   atualizado e indique onde consultar.
5. **Assuntos fora do escopo** (sem relação com MEI, empreendedorismo, impostos ou
   finanças do pequeno negócio): diga gentilmente que é especializado em MEI e sugira
   o tipo de pergunta que pode responder.
6. **Pergunta vaga ou ambígua**: responda a interpretação mais provável e, se fizer
   diferença, pergunte ao final o que a pessoa quis dizer.

Nunca contradiga os documentos com conhecimento externo. Se as fontes não cobrem a
dúvida por completo, responda o que for possível e diga claramente o que faltou.
Use o histórico da conversa para entender perguntas de continuação
("e se eu atrasar?", "e quanto custa?", "pode explicar melhor?").

## Citações
Os trechos vêm numerados como [1], [2], ... Ao usar uma informação de um trecho,
coloque o número dele entre colchetes logo após a frase, ex.: "O MEI pode ter um
empregado [3]." Não cite trechos que você não usou. Não invente números de trecho.
Informações de conhecimento geral ficam sem citação.

## Formato
- Vá direto ao ponto: comece pela resposta e depois dê os detalhes.
- Linguagem simples, sem juridiquês; explique siglas na primeira vez (ex.: DAS —
  Documento de Arrecadação do Simples Nacional).
- Use parágrafos curtos, **negrito** para o essencial e listas com "-" ou "1." quando
  houver passos ou requisitos. NÃO use tabelas nem títulos com "#".
- Respostas de tamanho proporcional à pergunta: saudações em 1–2 frases; dúvidas
  complexas podem ter mais detalhes."""

PROMPT_REESCRITA = """Você prepara consultas de busca para uma base de documentos sobre MEI
(Microempreendedor Individual): cartilhas do MEI, perguntas e respostas oficiais e
cartilhas de impostos.

Reescreva a ÚLTIMA pergunta do usuário como UMA consulta de busca completa e autônoma:
- resolva referências ao histórico (ex.: "e quanto custa?" -> "quanto custa o DAS mensal do MEI");
- corrija erros de digitação e gírias;
- acrescente sinônimos e os termos técnicos que os documentos usam (ex.: DAS, DASN-SIMEI,
  CNPJ, formalização, baixa, faturamento, receita bruta, Simples Nacional, INSS,
  previdência, nota fiscal, ocupações permitidas, desenquadramento, empregado).
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


def reescrever_consulta(cliente, pergunta, historico):
    """Usa o LLM para transformar a pergunta numa consulta rica para a busca.

    Se der qualquer erro, devolve None e a busca segue só com a pergunta original.
    """
    conversa = "\n".join(
        f"{'Usuário' if m['role'] == 'user' else 'Assistente'}: {m['content'][:400]}"
        for m in historico[-4:]
    )
    entrada = (f"Histórico recente:\n{conversa}\n\n" if conversa else "") + \
        f"Última pergunta do usuário: {pergunta}"
    try:
        r = cliente.chat.completions.create(
            model=MODELO_REESCRITA,
            temperature=0,
            max_completion_tokens=400,
            messages=[{"role": "system", "content": PROMPT_REESCRITA},
                      {"role": "user", "content": entrada}],
            **_opcoes_modelo(MODELO_REESCRITA, "low"),
        )
        linhas = (r.choices[0].message.content or "").strip().splitlines()
        consulta = linhas[0].strip().strip('"') if linhas else ""
        return consulta[:500] or None
    except Exception as erro:  # noqa: BLE001
        print(f"[RAG] reescrita da consulta falhou: {erro}", flush=True)
        return None


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
    paginas = defaultdict(set)
    for t in trechos:
        paginas[t["fonte"]].add(int(t["pagina"]))
    return [f"{fonte} (p. {', '.join(str(p) for p in sorted(pags))})"
            for fonte, pags in sorted(paginas.items())]


def montar_mensagens(trechos, pergunta, historico):
    contexto = "\n\n".join(
        f"[{i}] (Fonte: {t['fonte']}, p. {t['pagina']})\n{t['texto']}"
        for i, t in enumerate(trechos, start=1)
    )
    usuario = (f"Trechos recuperados dos documentos sobre MEI:\n{contexto}\n\n"
               f"---\nPergunta do usuário: {pergunta}")
    return ([{"role": "system", "content": SISTEMA}] + historico +
            [{"role": "user", "content": usuario}])


def responder(indice, cliente, pergunta, historico=None):
    historico = _sanear_historico(historico)

    consultas = [pergunta]
    reescrita = reescrever_consulta(cliente, pergunta, historico) if REESCREVER_CONSULTA else None
    if reescrita:
        consultas.append(reescrita)
    elif historico:
        # Sem reescrita: junta a última pergunta do usuário para dar contexto à busca.
        anteriores = [m["content"] for m in historico if m["role"] == "user"]
        if anteriores:
            consultas.append(f"{anteriores[-1]} {pergunta}")
    print(f"[RAG] consultas: {consultas}", flush=True)

    trechos = indice.buscar_varias(consultas)
    r = cliente.chat.completions.create(
        model=MODELO_LLM,
        temperature=0.2,
        messages=montar_mensagens(trechos, pergunta, historico),
        **_opcoes_modelo(MODELO_LLM, "medium"),
    )
    resposta, citados = _extrair_citacoes(r.choices[0].message.content or "", len(trechos))
    fontes = _formatar_fontes([trechos[i] for i in sorted(citados)])
    return resposta, fontes
