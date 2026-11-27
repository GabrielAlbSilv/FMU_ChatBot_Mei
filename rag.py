"""Lógica RAG do FiscOn — Chatbot MEI e Receita Federal (versão leve).

- Busca nos PDFs (embeddings fastembed + BM25): busca.py
- Contas e regras por ano-calendário: regras_fiscais.py
- Conferência com Sebrae e Receita Federal: verificador.py (+ base_web.py, pesquisa.py)
- LLM: SDK oficial da Groq
- Dados pessoais (CPF, CNPJ, e-mail, telefone) são mascarados antes de qualquer uso: privacidade.py
Na inicialização do servidor apenas carregamos o índice (segundos, pouca RAM).
"""
# Limita threads das bibliotecas numéricas ANTES de importar numpy/onnxruntime.
import os

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_v] = "1"

import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import defaultdict  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

VERSAO = "rag-leve-v14"
ETAPA = "rag.py novo carregando"


def _etapa(texto):
    global ETAPA
    ETAPA = texto
    print(f"[RAG] {texto}", flush=True)


def etapa_atual():
    return ETAPA


sys.path.insert(0, str(Path(__file__).resolve().parent))   # app.py carrega este arquivo por caminho
import config as C  # noqa: E402

_etapa("importando numpy")
import numpy as np  # noqa: E402

import busca  # noqa: E402  (PDFs -> índice -> trechos)
import privacidade  # noqa: E402  (CPF, CNPJ, e-mail e telefone mascarados)
import regras_fiscais  # noqa: E402  (contas e regras por ano-calendário: DAS, limites, IRPF...)
import verificador  # noqa: E402  (conferência com Sebrae e Receita Federal)
# Reexporta a busca: rag.tokenizar, rag.juntar_vizinhos, rag.construir_indice... continuam funcionando.
from busca import (ARQ_META, ARQ_TRECHOS, ARQ_VETORES, DOCUMENTOS_CONHECIDOS, K, NOME_MODELO_EMB,  # noqa: E402,F401
                   PASTA_INDICE, PASTA_MODELO, PASTA_PROJETO, SOBREPOSICAO, TAM_CHUNK, VERSAO_INDICE, Bm25,
                   Indice, _dividir, _info_docs, _limpar, _nome_legivel, _sem_acento, assinatura_pdfs,
                   carregar_info_documentos, construir_indice, criar_embeddings, extrair_trechos, info_documento,
                   juntar_vizinhos, listar_pdfs, pdfs_faltando, tokenizar)

_etapa("rag importado")

MODELO_LLM = C.MODELO_LLM
MODELO_RESERVA = C.MODELO_RESERVA if C.MODELO_RESERVA != C.MODELO_LLM else ""
PAUSA_RESERVA_S = C.PAUSA_RESERVA_S
# Modelo usado só para reescrever a pergunta antes da busca. O padrão é o gpt-oss-20b, cerca de
# 2x mais rápido que o 120b e suficiente para essa tarefa curta. Se a Groq recusar o modelo
# (ex.: foi descontinuado), o bot passa a usar o MODELO_LLM sozinho.
MODELO_REESCRITA = C.MODELO_REESCRITA
REESCREVER_CONSULTA = C.REESCREVER_CONSULTA
EXTRAIR_DADOS_LLM = C.EXTRAIR_DADOS_LLM
# Modelo da etapa que compara a resposta dos PDFs com os sites do Sebrae e da Receita Federal.
MODELO_VERIFICACAO = C.MODELO_VERIFICACAO
# Quanto o modelo "pensa" antes de responder (low | medium | high). "low" responde bem mais rápido.
ESFORCO_RESPOSTA = C.ESFORCO_RESPOSTA
# Quando conferir com os sites:
#   "depois" (padrão): a resposta aparece na hora (dos PDFs) e a conferência roda em seguida; se algo
#                      estiver desatualizado, a resposta é trocada pela versão corrigida.
#   "antes":           comportamento antigo — só mostra a resposta depois de conferida (mais lento).
# Em qualquer modo, quando há FATOS CALCULADOS a resposta só aparece depois de conferida com eles
# (é curta e a conferência é local): a pessoa nunca lê uma conta errada que depois some.
CONFERENCIA_MODO = C.CONFERENCIA_MODO
# Junta à resposta até N páginas oficiais recentes da base web local (base_web.py). 0 = desliga.
WEB_NA_RESPOSTA = C.WEB_NA_RESPOSTA
MAX_HISTORICO = 6          # mensagens anteriores (usuário + bot) enviadas ao LLM
MAX_CHARS_HISTORICO = 1500  # por mensagem


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
    faltando = pdfs_faltando()
    if faltando:
        print(f"[RAG] AVISO: PDFs ativos em fontes_pdfs.json que não estão na pasta: {faltando} "
              "(rode python atualizar_base.py)", flush=True)

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
Mudanças recentes de lei (ex.: a Lei Complementar nº 238/2026, que muda o teto do MEI, o número de
empregados e perdoa dívidas) podem não estar nos PDFs: quando vierem nos FATOS CALCULADOS, eles valem.
Dados pessoais da pergunta aparecem mascarados (ex.: "[CPF]"): não peça nem tente adivinhar esses números.

## Citações
Os trechos vêm numerados como [1], [2], ... Ao usar uma informação de um trecho,
coloque o número dele entre colchetes logo após a frase, ex.: "O MEI deve entregar a
declaração anual todo ano [3]." Não cite trechos que você não usou. Não invente números de trecho.
Informações de conhecimento geral ficam sem citação.

## Formato
- Vá direto ao ponto: a primeira frase já responde a pergunta (ex.: "Sim, o MEI pode emitir
  nota fiscal."); depois vêm os detalhes.
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

Tarefa 1 — reescreva a ÚLTIMA pergunta do usuário como UMA consulta de busca completa e autônoma:
- resolva referências ao histórico (ex.: "e quanto custa?" -> "quanto custa o DAS mensal do MEI");
- corrija erros de digitação e gírias;
- acrescente sinônimos e os termos técnicos que os documentos usam (ex.: DAS, DASN-SIMEI,
  CNPJ, formalização, baixa, faturamento, receita bruta, Simples Nacional, INSS,
  previdência, nota fiscal, ocupações permitidas, desenquadramento, empregado e, para a
  Receita Federal: imposto de renda, IRPF, CPF, declaração, restituição, malha fina, tributos).

Tarefa 2 — copie os dados que o USUÁRIO informou (não invente; sem o dado, use null):
- "faturamento": receita bruta do MEI informada, em reais (número, ex.: 50000);
- "rendimento": rendimentos tributáveis da pessoa física informados, em reais (número);
- "mes_abertura" (1 a 12) e "ano_abertura" (ex.: 2026): quando o MEI foi aberto;
- "atividade": "servicos", "comercio" ou "misto"; e "evidencia_atividade": as palavras EXATAS do
  usuário que mostram a atividade (ex.: "sou manicure");
- "temas": lista com os assuntos da pergunta, só destes: irpf, excesso, teto, limite, das_valor,
  sem_faturamento, dasn, dirf, empregados, remissao (lista vazia se nenhum).

Responda APENAS com um JSON válido em uma linha, sem explicações, neste formato:
{"consulta": "...", "faturamento": null, "rendimento": null, "mes_abertura": null, "ano_abertura": null,
 "atividade": null, "evidencia_atividade": null, "temas": []}"""


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


def _ler_reescrita(texto):
    """Resposta da reescrita -> (consulta, dados). Aceita o JSON pedido ou só a consulta (formato antigo)."""
    texto = (texto or "").strip()
    ini, fim = texto.find("{"), texto.rfind("}")
    if ini >= 0 and fim > ini:
        try:
            dados = json.loads(texto[ini:fim + 1])
        except ValueError:
            dados = None
        if isinstance(dados, dict):
            consulta = str(dados.get("consulta") or "").strip()
            return (consulta[:500] or None), dados
    if texto.startswith("{"):   # JSON cortado/inválido: aproveita só a consulta, se der
        m = re.search(r'"consulta"\s*:\s*"([^"\n]{3,500})', texto)
        return (m.group(1).strip() if m else None), {}
    linhas = texto.splitlines()
    consulta = linhas[0].strip().strip('"') if linhas else ""
    return (consulta[:500] or None), {}


def reescrever_consulta(cliente, pergunta, historico, situacao=""):
    """Só a consulta reescrita (compatibilidade). Ver reescrever_e_extrair."""
    return reescrever_e_extrair(cliente, pergunta, historico, situacao)[0]


def reescrever_e_extrair(cliente, pergunta, historico, situacao=""):
    """Usa o LLM para transformar a pergunta numa consulta rica para a busca e sugerir dados.

    `situacao`: dados que o usuário já informou na conversa (ex.: "MEI aberto em 07/2026;
    faturamento R$ 50.000,00"), extraídos por regras_fiscais — assim a busca não perde o mês de
    abertura quando ele foi dito duas mensagens atrás.
    Devolve (consulta, dados_sugeridos). Os dados ainda NÃO são confiáveis: passam por
    regras_fiscais.validar_dados_llm antes de qualquer uso.
    Se der qualquer erro, devolve (None, {}) e a busca segue só com a pergunta original.
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
            return _ler_reescrita(r.choices[0].message.content)
        except Exception as erro:  # noqa: BLE001
            if tentativa == 0 and modelo != MODELO_LLM and _erro_de_modelo(erro):
                print(f"[RAG] modelo de reescrita '{modelo}' indisponível; usando {MODELO_LLM}", flush=True)
                _modelo_reescrita = MODELO_LLM
                continue
            print(f"[RAG] reescrita da consulta falhou: {erro}", flush=True)
            return None, {}
    return None, {}


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
_trava_cache = threading.Lock()   # o gunicorn atende com várias threads ao mesmo tempo


def _chave_cache(pergunta):
    return " ".join(re.findall(r"[a-z0-9]+", _sem_acento(pergunta)))


def _cache_ler(chave):
    with _trava_cache:
        item = _cache.get(chave)
        if item and time.time() - item[0] < CACHE_VALIDADE:
            return item[1]
        _cache.pop(chave, None)
    return None


def _cache_gravar(chave, resultado):
    with _trava_cache:
        while len(_cache) >= CACHE_MAX:
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


def trechos_da_web(consulta, k=WEB_NA_RESPOSTA, pergunta=None):
    """Páginas oficiais recentes da base web local, no mesmo formato dos trechos dos PDFs."""
    if k <= 0:
        return []
    try:
        import base_web
        rica, destaques = verificador.expandir_consulta(pergunta or consulta, consulta if pergunta else "")
        achados = base_web.buscar(rica, k=k * 2, min_termos=3, limite_chars=1600, destaques=destaques)
        if destaques:      # só entra na resposta página que trata do assunto (não basta repetir "MEI")
            achados = [a for a in achados if a.get("destaque")]
        achados = achados[:k]
    except Exception as erro:  # noqa: BLE001
        print(f"[RAG] base web indisponível: {erro}", flush=True)
        return []
    return [{"fonte": "web", "pagina": 0, "url": a["url"], "titulo": a["titulo"], "site": a["site"],
             "data": a.get("data", ""), "tipo": a.get("tipo", "pagina"), "texto": a["texto"]} for a in achados]


# ------------------------------------------------- modelo reserva (limite de uso da Groq)
_reserva_ate = [0.0]   # até quando usar o reserva direto, sem tentar o principal


def eh_limite_de_uso(erro):
    """Erro 429 da Groq (limite de pedidos ou de tokens por minuto/dia)."""
    return type(erro).__name__ == "RateLimitError" or getattr(erro, "status_code", None) == 429


def _modelo_da_vez():
    return MODELO_RESERVA if MODELO_RESERVA and time.time() < _reserva_ate[0] else MODELO_LLM


def _criar_resposta(cliente, **parametros):
    """Chama o modelo principal; se a Groq recusar por limite (429), usa o MODELO_RESERVA.

    Depois de um 429, o reserva é usado direto por PAUSA_RESERVA_S segundos (não adianta insistir
    no principal nesse intervalo). Devolve (resposta_da_groq, modelo_usado). Se o reserva também
    estiver no limite, o erro sobe e a pessoa vê a mensagem de "muitas pessoas usando".
    """
    modelo = _modelo_da_vez()
    try:
        return cliente.chat.completions.create(model=modelo, **_opcoes_modelo(modelo, ESFORCO_RESPOSTA),
                                               **parametros), modelo
    except Exception as erro:  # noqa: BLE001
        if modelo != MODELO_LLM or not MODELO_RESERVA or not eh_limite_de_uso(erro):
            raise
        _reserva_ate[0] = time.time() + PAUSA_RESERVA_S
        print(f"[RAG] limite da Groq no modelo {MODELO_LLM}; usando o reserva {MODELO_RESERVA} "
              f"por {PAUSA_RESERVA_S}s", flush=True)
        return cliente.chat.completions.create(model=MODELO_RESERVA, **_opcoes_modelo(MODELO_RESERVA, ESFORCO_RESPOSTA),
                                               **parametros), MODELO_RESERVA


def _gerar_texto(cliente, mensagens):
    """Chama o modelo em streaming. Produz ("modelo", nome), ("texto", pedaço)... e ("completo", texto_inteiro)."""
    fluxo, modelo = _criar_resposta(cliente, temperature=0.2, stream=True, messages=mensagens)
    yield "modelo", modelo
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

    Privacidade: CPF, CNPJ, e-mail e telefone da pergunta e do histórico são mascarados AQUI, antes
    de logs, cache, regras e qualquer chamada à Groq (privacidade.py).

    Regras fiscais: regras_fiscais.py lê os dados da pergunta com TODO o histórico do usuário — não só
    as últimas mensagens. A reescrita da pergunta pelo modelo pode sugerir dados que as regras de texto
    não pegaram; o código só aceita o que confirma no texto do usuário (validar_dados_llm). Com os
    dados, o código calcula os fatos (limites, DAS, IRPF, desenquadramento...), que vão no prompt.
    Quando há fatos, o texto NÃO é mostrado aos pedaços: primeiro é conferido com eles (_aplicar_regras),
    para a pessoa nunca ler uma conta errada que depois seria trocada.

    Velocidade: a coleta das páginas oficiais para a conferência começa EM PARALELO com a
    resposta; no modo "depois" (padrão) a pessoa já lê a resposta enquanto ela é conferida.
    """
    pergunta = privacidade.mascarar(str(pergunta or ""))
    historico_bruto = privacidade.mascarar_historico(historico if isinstance(historico, list) else [])
    historico = _sanear_historico(historico_bruto)
    conversa = eh_conversa_simples(pergunta)
    fatos, situacao = [], ""
    if not conversa:
        try:
            fatos, sit = regras_fiscais.analisar(pergunta, historico_bruto)
            situacao = regras_fiscais.resumo_situacao(sit)
        except Exception as erro:  # noqa: BLE001  (regra quebrada não pode derrubar o chat)
            print(f"[RAG] regras fiscais falharam: {type(erro).__name__}: {erro}", flush=True)
            fatos, situacao = [], ""

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
        reescrita, sugeridos = (reescrever_e_extrair(cliente, pergunta, historico, situacao)
                                if REESCREVER_CONSULTA else (None, {}))
        if EXTRAIR_DADOS_LLM and sugeridos:
            try:
                extras = regras_fiscais.validar_dados_llm(sugeridos, pergunta, historico_bruto)
                if extras:
                    novos, sit = regras_fiscais.analisar(pergunta, historico_bruto, extras)
                    if [f["tema"] for f in novos] != [f["tema"] for f in fatos] or \
                            regras_fiscais.resumo_situacao(sit) != situacao:
                        print(f"[RAG] dados confirmados da reescrita: {sorted(extras)}", flush=True)
                    fatos, situacao = novos, regras_fiscais.resumo_situacao(sit)
            except Exception as erro:  # noqa: BLE001
                print(f"[RAG] dados da reescrita ignorados: {type(erro).__name__}: {erro}", flush=True)
        if fatos:
            chave = None
            print(f"[RAG] fatos calculados: {[f['tema'] for f in fatos]} | situação: {situacao}", flush=True)
        if reescrita:
            consultas.append(reescrita)
        elif historico or situacao:
            # Sem reescrita: junta a última pergunta do usuário e os dados já informados.
            anteriores = [m["content"] for m in historico if m["role"] == "user"]
            consultas.append(" ".join(filter(None, [anteriores[-1] if anteriores else "", situacao, pergunta])))
        print(f"[RAG] consultas: {consultas}", flush=True)
        trechos = indice.buscar_varias(consultas) + trechos_da_web(consultas[-1], pergunta=pergunta)

    conferir = verificador.ATIVO and not conversa
    # Começa já a juntar as páginas oficiais (base local -> internet), enquanto o modelo escreve.
    coleta = _executor.submit(verificador.coletar_trechos, consultas[-1], pergunta) if conferir else None
    mensagens = montar_mensagens(trechos, pergunta, historico, fatos, situacao)

    if conferir and CONFERENCIA_MODO == "antes":
        yield from _responder_com_conferencia(cliente, trechos, consultas, pergunta, mensagens, chave, coleta, fatos)
        return

    segurar = bool(fatos)
    if segurar:
        yield "status", "Calculando pelas regras oficiais…"
    completo, modelo_usado = "", MODELO_LLM
    for tipo, dado in _gerar_texto(cliente, mensagens):
        if tipo == "texto":
            if not segurar:
                yield "texto", dado
        elif tipo == "modelo":
            modelo_usado = dado
        else:
            completo = dado

    texto, sugestoes = _separar_sugestoes(completo)
    resposta, citados = _extrair_citacoes(texto, len(trechos))
    ajuste = None
    if segurar:   # confere com as regras ANTES de mostrar
        resposta, citados, ajuste = _aplicar_regras(resposta, citados, fatos)
        yield "texto", resposta
    verificacao, substituida = {"status": "nao_aplicavel"}, False
    if conferir and resposta and verificador.deve_verificar(pergunta, bool(citados)):
        yield "status", "Conferindo com o Sebrae e a Receita Federal…"
        resposta, citados, verificacao = _conferir(cliente, pergunta, consultas, resposta, citados, trechos,
                                                   coleta, fatos)
        substituida = verificacao["status"] == "atualizada"
    elif coleta:
        coleta.cancel()
    if not segurar:
        resposta, citados, ajuste = _aplicar_regras(resposta, citados, fatos)   # sem fatos: não muda nada
        substituida = substituida or ajuste == "corrigida"
    elif substituida:   # a versão dos sites também passa pelas regras
        resposta, citados, ajuste = _aplicar_regras(resposta, citados, fatos)
    if modelo_usado != MODELO_LLM:
        chave = None   # resposta do modelo reserva não vai para o cache
    yield "fim", dict(_montar_resultado(pergunta, resposta, citados, trechos, sugestoes, verificacao, chave,
                                        fatos, ajuste),
                      substituida=substituida, cache=False, modelo_reserva=modelo_usado != MODELO_LLM)


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
    r, modelo_usado = _criar_resposta(cliente, temperature=0.2, stream=False, messages=mensagens)
    if modelo_usado != MODELO_LLM:
        chave = None
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
                      substituida=False, cache=False, modelo_reserva=modelo_usado != MODELO_LLM)


def responder(indice, cliente, pergunta, historico=None):
    """Versão sem streaming (útil para testes): devolve o resultado final."""
    for tipo, dado in responder_stream(indice, cliente, pergunta, historico):
        if tipo == "fim":
            return dado
    return None
