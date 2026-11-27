"""Regras fiscais determinísticas (MEI, DAS, desenquadramento, IRPF, DIRF) por ano-calendário.

Por que existe: nos testes, os erros vieram do modelo de linguagem fazendo contas e aplicando
regras numéricas sozinho (34 mil "acima" de 35.584; teto anual no lugar do proporcional; ISS do
MEI tratado como alíquota municipal...). Aqui o CÓDIGO faz as contas a partir de
regras_fiscais.json (editável pela equipe do NAF) e devolve "fatos" prontos. O rag.py:
  1. coloca os fatos no prompt, com prioridade sobre os trechos dos PDFs;
  2. confere a resposta do modelo com `garantir()`: completa o que faltou e, se a resposta
     contradiz um fato, troca pela resposta montada só com os fatos.
O verificador.py usa `contradiz()` para recusar uma "Resposta atualizada" que contradiga um fato.

Dados da conversa: as regras de texto (regex) leem faturamento, mês de abertura etc. A reescrita da
pergunta feita pelo modelo pode SUGERIR dados que as regex não pegaram (`validar_dados_llm`), mas o
código só aceita o que confirmar no texto do usuário (o número tem de estar lá; a atividade precisa de
uma citação literal). O modelo nunca faz conta: só ajuda a ler.

Só biblioteca padrão. Valores em Decimal (sem erro de arredondamento no limite de 20%).
"""
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ARQ_REGRAS = Path(__file__).resolve().parent / "regras_fiscais.json"
FUSO_BR = timezone(timedelta(hours=-3))

MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
         "setembro", "outubro", "novembro", "dezembro"]
MESES_EXIBICAO = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                  "setembro", "outubro", "novembro", "dezembro"]
_ABREV_MES = {m[:3]: i + 1 for i, m in enumerate(MESES)}

_regras_cache = {}


def carregar_regras(caminho=ARQ_REGRAS):
    """Lê regras_fiscais.json (com cache pela data de modificação do arquivo)."""
    caminho = Path(caminho)
    try:
        mtime = caminho.stat().st_mtime
    except OSError:
        return {}
    item = _regras_cache.get(str(caminho))
    if item and item[0] == mtime:
        return item[1]
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        print(f"[REGRAS] não consegui ler {caminho.name}: {erro}", flush=True)
        dados = {}
    _regras_cache[str(caminho)] = (mtime, dados)
    return dados


def _sem_acento(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()


def _d(valor):
    return Decimal(str(valor))


def brl(valor):
    """Decimal/float -> 'R$ 40.500,00'."""
    v = _d(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    inteiro, centavos = f"{v:.2f}".split(".")
    sinal = "-" if inteiro.startswith("-") else ""
    inteiro = inteiro.lstrip("-")
    grupos = []
    while inteiro:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    return f"R$ {sinal}{'.'.join(grupos)},{centavos}"


def _milhar(valor):
    """'40.500' (sem centavos) — forma curta que a resposta do modelo costuma usar."""
    return brl(valor)[3:].split(",")[0]


def _alts(valor):
    """Formas aceitas de um valor na resposta: '40.500' e, se for redondo, '40,5 mil' / '81 mil'."""
    curto = _milhar(valor)
    alts = [curto]
    v = _d(valor)
    if v >= 1000 and v % 100 == 0:
        mil = (v / 1000).normalize()
        alts.append(f"{mil:f}".replace(".", ",") + " mil")
    return alts


def pct(valor):
    v = _d(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{v:.2f}".replace(".", ",") + "%"


def hoje():
    return datetime.now(FUSO_BR).date()


# =============================================================== extração de dados da conversa
_DINHEIRO = re.compile(
    r"(?<![\d/.,])(r\$\s*)?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?(\s*(?:mil\b|k\b))?(?!\s*%)(?![\d/])")
_ANO = re.compile(r"(?<![\d/.,])(20[0-4]\d)(?![\d.,])")

_CTX_FATURAMENTO = re.compile(r"fatur|receita bruta|vend[ei]|vendas|receita do mei|movimentei|faturei")
_CTX_RENDIMENTO = re.compile(r"salari|rendiment|tributave|recebi|ganhei|aposentadoria|aluguel|pro-?labore|"
                             r"imposto de renda|irpf|dirpf")


def _valores_monetarios(texto):
    """[(posição, Decimal)] dos valores em reais do texto (ignora anos, datas e percentuais)."""
    t = _sem_acento(texto)
    achados = []
    for m in _DINHEIRO.finditer(t):
        prefixo, inteiro, cent, mil = m.group(1), m.group(2), m.group(3), m.group(4)
        numero = _d(inteiro.replace(".", "") + ("." + cent if cent else ""))
        if mil:
            numero *= 1000
        elif not prefixo and "." not in inteiro:
            # número solto: só vale como dinheiro se for grande e não parecer um ano
            if numero < 1000 or (2000 <= numero <= 2049 and not cent):
                continue
        achados.append((m.start(), numero))
    return achados


def _classificar_valor(texto_sa, pos):
    """'faturamento', 'rendimento' ou None, pelas palavras ao redor do valor."""
    janela = texto_sa[max(0, pos - 90):pos + 60]
    if re.search(r"\b(teto|limite)\b[^.?!]{0,30}$", texto_sa[max(0, pos - 40):pos]):
        return None          # "teto de 81 mil", "limite de R$ 40.500": é o limite, não o valor da pessoa
    fat, ren = _CTX_FATURAMENTO.search(janela), _CTX_RENDIMENTO.search(janela)
    if fat and not ren:
        return "faturamento"
    if ren and not fat:
        return "rendimento"
    if fat and ren:   # o termo mais próximo do número decide
        antes = texto_sa[max(0, pos - 90):pos]
        ult_fat = max((x.end() for x in _CTX_FATURAMENTO.finditer(antes)), default=-1)
        ult_ren = max((x.end() for x in _CTX_RENDIMENTO.finditer(antes)), default=-1)
        return "faturamento" if ult_fat >= ult_ren else "rendimento"
    if re.search(r"\bmei\b|simei|\bdas\b", texto_sa):
        return "faturamento"
    return None


_ABERTURA = re.compile(
    r"\b(abri|abriu|abrimos|aberto|aberta|abertura|formaliz\w*|inscrev\w*|registr\w*|comecei|comecou|"
    r"inici\w*|criei|criou|desde)\b[^.?!\n]{0,40}?"
    r"(?:\b(?P<nome>janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\b"
    r"(?:\s*(?:de|/)\s*(?P<ano1>20[0-4]\d))?"
    # abreviações ("jul/2026") só com o ano junto: "dez" sozinho é o número dez
    r"|\b(?P<abrev>jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\s*/\s*(?P<ano3>20[0-4]\d)\b"
    r"|\b(?P<mes>0?[1-9]|1[0-2])\s*/\s*(?P<ano2>20[0-4]\d)\b)")


def _abertura(texto_sa):
    m = _ABERTURA.search(texto_sa)
    if not m:
        return None
    if m.group("nome"):
        mes = _ABREV_MES[m.group("nome")[:3]]
        ano = m.group("ano1")
    elif m.group("abrev"):
        mes, ano = _ABREV_MES[m.group("abrev")], m.group("ano3")
    else:
        mes, ano = int(m.group("mes")), m.group("ano2")
    if not ano:   # "abri em julho" -> procura o ano logo depois ("... de 2026")
        resto = _ANO.search(texto_sa, m.end(), m.end() + 30)
        ano = resto.group(1) if resto else None
    return {"mes": mes, "ano": int(ano) if ano else None}


def _atividade(texto_sa):
    servico = re.search(r"servic|prestador|prestadora|presto|prestar|presta\b", texto_sa)
    comercio = re.search(r"comerci|industr|\bvend[eo]\b|vendas|\bloja\b|revend|produtos?\b", texto_sa)
    if re.search(r"exclusivamente (de )?servic|so (presto|prest\w*) servic|apenas servic|somente servic", texto_sa):
        return "servicos"
    if servico and comercio:
        return "misto"
    if servico:
        return "servicos"
    if comercio:
        return "comercio"
    return None


_TEMAS = {
    "irpf": re.compile(r"imposto de renda|\birpf\b|\bdirpf\b|declaracao de ajuste|\bir\b.{0,25}(declar|entreg)"),
    "excesso": re.compile(r"excesso|exced|ultrapass|estour|passei do limite|passou do limite|desenquadr|"
                          r"acima do limite"),
    "teto": re.compile(r"\bteto\b|limite (de faturamento|anual|do mei|maximo)|quanto (o mei )?pode faturar|"
                       r"faturamento maximo|\blc\b.{0,10}238|complementar n?.{0,4}238"),
    "limite": re.compile(r"\blimite\b|quanto (eu )?(posso|pode|consigo) faturar|faturamento permitido"),
    "das_valor": re.compile(r"(quanto|valor|custa|paga|pagar|preco)[^.?!]{0,50}\bdas\b|\bdas\b[^.?!]{0,30}"
                            r"(valor|custa|quanto)|boleto (mensal )?do mei"),
    "sem_faturamento": re.compile(r"sem (nenhum )?faturamento|nao fatur\w*|faturamento (zero|zerado)|"
                                  r"receita (zero|zerada)|sem (nenhuma )?receita|sem movimento|nao vendi|"
                                  r"sem vender|nao teve faturamento|sem ter faturado"),
    "dasn": re.compile(r"\bdasn\b|declaracao anual|declaracao do mei"),
    "dirf": re.compile(r"\bdirf\b"),
    # LC 238/2026
    "empregados": re.compile(r"empregad|funcionari|ajudante|\bcontrat\w*\b[^.?!]{0,25}\b(pessoa|alguem|empregad|"
                             r"funcionari|ajudante|colaborador)"),
    "remissao": re.compile(r"perdao|perdoa|remissao|remitid|anistia|\blc\b.{0,10}238[^.?!]{0,40}divida|"
                           r"dividas?[^.?!]{0,40}(cancelad|perdoad|zerad)|cancela\w* (as |das |de )?dividas?"),
}
TEMAS_VALIDOS = set(_TEMAS)


def _temas(texto_sa):
    temas = {nome for nome, rx in _TEMAS.items() if rx.search(texto_sa)}
    if "irpf" in temas and re.search(r"\bmei\b|simei|\bdasn\b", texto_sa) and not re.search(
            r"imposto de renda|\birpf\b|\bdirpf\b", texto_sa):
        temas.discard("irpf")
    return temas


def _ano_irpf(texto_sa):
    """(ano-calendário, exercício) citados numa pergunta de IRPF; None quando não há pista."""
    exercicio = re.search(r"(?:irpf|dirpf|imposto de renda|declaracao(?: de ajuste)?(?: anual)?)\s*(?:de\s*)?"
                          r"(20[0-4]\d)", texto_sa)
    ac = None
    for m in re.finditer(r"\b(?:em|no ano de|durante|ano-calendario|ano calendario|ano base|ano-base)\s*(20[0-4]\d)",
                         texto_sa):
        if not exercicio or m.start(1) != exercicio.start(1):
            ac = int(m.group(1))
            break
    if ac:
        return ac, ac + 1
    if exercicio:
        return int(exercicio.group(1)) - 1, int(exercicio.group(1))
    return None


def _mensagens_usuario(historico):
    saida = []
    for m in historico or []:
        if not isinstance(m, dict):
            continue
        papel = m.get("papel") or m.get("role")
        if papel in ("user", "usuario"):
            texto = str(m.get("texto") or m.get("content") or "").strip()
            if texto:
                saida.append(texto)
    return saida


def extrair_situacao(pergunta, historico=None):
    """Junta os dados citados pelo USUÁRIO em toda a conversa (o mais recente prevalece).

    Devolve {"temas", "abertura", "faturamento", "rendimento", "atividade", "anos", "ano_irpf"}.
    Os temas são os da pergunta atual; se ela não tem tema (ex.: "e quando começa?"), herda os
    da última pergunta que tinha.
    """
    situacao = {"temas": set(), "abertura": None, "faturamento": None, "rendimento": None,
                "atividade": None, "anos": [], "ano_irpf": None}
    textos = _mensagens_usuario(historico) + [str(pergunta or "")]
    temas_anteriores = set()
    for i, texto in enumerate(textos):
        t = _sem_acento(texto)
        atual = i == len(textos) - 1
        ab = _abertura(t)
        if ab:
            if ab["ano"] is None and situacao["abertura"] and situacao["abertura"].get("ano"):
                ab["ano"] = situacao["abertura"]["ano"]
            situacao["abertura"] = ab
        for pos, valor in _valores_monetarios(texto):
            tipo = _classificar_valor(t, pos)
            if tipo:
                situacao[tipo] = valor
        if re.search(r"\b(sem|zero|nao)\b[^.?!]{0,20}fatur|faturamento (zero|zerado)|receita (zero|zerada)", t) \
                and not _valores_monetarios(texto):
            situacao["faturamento"] = Decimal("0")
        at = _atividade(t)
        if at:
            situacao["atividade"] = at
        anos = [int(a) for a in _ANO.findall(t)]
        if anos:
            situacao["anos"] = anos
        ai = _ano_irpf(t)
        if ai:
            situacao["ano_irpf"] = ai
        temas = _temas(t)
        if atual:
            situacao["temas"] = temas or (temas_anteriores if len(texto) <= 200 else set())
        elif temas:
            temas_anteriores = temas
    if situacao["abertura"] and situacao["abertura"]["ano"] is None:
        situacao["abertura"]["ano"] = situacao["anos"][-1] if situacao["anos"] else hoje().year
    return situacao


def resumo_situacao(situacao):
    """Frase curta para a busca e para a reescrita da pergunta (ex.: 'MEI aberto em 07/2026; ...')."""
    partes = []
    if situacao.get("abertura"):
        partes.append(f"MEI aberto em {situacao['abertura']['mes']:02d}/{situacao['abertura']['ano']}")
    if situacao.get("faturamento") is not None:
        partes.append(f"faturamento {brl(situacao['faturamento'])}")
    if situacao.get("rendimento") is not None:
        partes.append(f"rendimentos tributáveis {brl(situacao['rendimento'])}")
    if situacao.get("atividade"):
        partes.append({"servicos": "presta serviços", "comercio": "comércio/indústria",
                       "misto": "comércio e serviços"}[situacao["atividade"]])
    return "; ".join(partes)


# ======================================================================== calculadoras
CAMPOS_DAS = ("salario_minimo", "inss_percentual", "icms", "iss")
CAMPOS_LIMITE = ("teto_anual", "limite_mensal_proporcional")


def dados_mei(ano, regras=None, exige=CAMPOS_DAS):
    """Regras do MEI para o ano. Se o ano não está em vigor/cadastrado, usa o último vigente.

    `exige`: campos que precisam estar preenchidos (o DAS precisa do salário mínimo; o limite de
    faturamento só do teto). Assim, quando a lei orçamentária confirmar o teto de 2027, basta trocar
    "vigente" para true — o limite novo passa a valer mesmo antes de existir o salário mínimo de 2027.
    Devolve (dados, ano_usado, aviso).
    """
    regras = regras or carregar_regras()
    tabela = regras.get("mei", {})
    item = tabela.get(str(ano))

    def serve(v):
        return v.get("vigente", True) and all(v.get(c) is not None for c in exige)

    if item and serve(item):
        return item, ano, ""
    vigentes = sorted(int(a) for a, v in tabela.items() if a.isdigit() and serve(v) and int(a) <= ano)
    if not vigentes:
        return None, None, ""
    usado = vigentes[-1]
    aviso = ""
    if item and item.get("vigente") is False:
        aviso = f"o teto de {brl(item['teto_anual'])} para {ano} ainda não está em vigor ({item.get('condicao', '')})"
    elif str(ano) not in tabela:
        aviso = f"ainda não há valores oficiais cadastrados para {ano}; usei os de {usado}"
    elif item:
        aviso = f"ainda faltam valores oficiais de {ano} no cadastro (ex.: salário mínimo); usei os de {usado}"
    return tabela[str(usado)], usado, aviso


def valores_das(ano, regras=None):
    """{'inss','comercio','servicos','misto'} em Decimal para o ano (None se não cadastrado)."""
    dados, usado, aviso = dados_mei(ano, regras, CAMPOS_DAS)
    if not dados:
        return None
    inss = (_d(dados["salario_minimo"]) * _d(dados["inss_percentual"]) / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    icms, iss = _d(dados["icms"]), _d(dados["iss"])
    return {"ano": usado, "aviso": aviso, "salario_minimo": _d(dados["salario_minimo"]),
            "inss": inss, "icms": icms, "iss": iss,
            "comercio": inss + icms, "servicos": inss + iss, "misto": inss + icms + iss,
            "fonte": dados.get("fonte"), "validado_naf": bool(dados.get("validado_naf"))}


def limite_mei(ano, mes_abertura=None, regras=None):
    """Limite de faturamento do ano: proporcional (meses desde a abertura, contando o mês) ou anual.

    "previsto": valor que valeria pela lei nova ainda não em vigor para o ano (ex.: LC 238/2026 em
    2027), para a resposta poder avisar; None quando o ano já está em vigor ou não tem previsão.
    """
    regras = regras or carregar_regras()
    dados, usado, aviso = dados_mei(ano, regras, CAMPOS_LIMITE)
    if not dados:
        return None
    meses = 12 - int(mes_abertura) + 1 if mes_abertura else 12

    def calcular(d):
        return _d(d["limite_mensal_proporcional"]) * meses if mes_abertura else _d(d["teto_anual"])

    previsto = None
    item = regras.get("mei", {}).get(str(ano)) or {}
    if usado != ano and item.get("vigente") is False and all(item.get(c) is not None for c in CAMPOS_LIMITE):
        previsto = {"valor": calcular(item), "ano": ano, "condicao": item.get("condicao", ""),
                    "mensal": _d(item["limite_mensal_proporcional"])}
    return {"valor": calcular(dados), "meses": meses, "proporcional": bool(mes_abertura), "ano": usado,
            "aviso": aviso, "fonte": dados.get("fonte"), "previsto": previsto}


def avaliar_excesso(faturamento, ano, mes_abertura=None, ano_abertura=None, regras=None, usar_previsto=False):
    """Calcula excesso e efeitos do desenquadramento. mes_abertura só vale se for o ano de abertura.

    usar_previsto=True: faz a conta com o limite da lei nova ainda não em vigor (cenário "se for confirmado").
    """
    regras = regras or carregar_regras()
    ano_de_abertura = bool(mes_abertura and ano_abertura == ano)
    lim = limite_mei(ano, mes_abertura if ano_de_abertura else None, regras)
    if not lim:
        return None
    if usar_previsto:
        if not lim.get("previsto"):
            return None
        lim = dict(lim, valor=lim["previsto"]["valor"], ano=ano, aviso="", previsto=None)
    faturamento = _d(faturamento)
    excesso = faturamento - lim["valor"]
    regra = regras.get("desenquadramento", {})
    tolerancia = _d(regra.get("percentual_tolerancia", 20))
    r = {"limite": lim, "faturamento": faturamento, "excesso": max(excesso, Decimal("0")),
         "houve_excesso": excesso > 0, "percentual": Decimal("0"), "acima_tolerancia": False,
         "ano": ano, "ano_de_abertura": ano_de_abertura, "mes_abertura": mes_abertura,
         "efeito": None, "efeito_texto": "", "fonte": regra.get("fonte") or lim.get("fonte"),
         "comunicacao": regra.get("comunicacao", "")}
    if excesso <= 0:
        return r
    r["percentual"] = excesso / lim["valor"] * 100
    r["acima_tolerancia"] = r["percentual"] > tolerancia
    if not r["acima_tolerancia"]:
        r["efeito"] = f"01/01/{ano + 1}"
        r["efeito_texto"] = f"a partir de 1º de janeiro de {ano + 1} ({regra.get('ate_tolerancia', '')})"
    elif ano_de_abertura:
        r["efeito"] = f"{int(mes_abertura):02d}/{ano}"
        r["efeito_texto"] = (f"retroativos ao início da atividade, em {MESES_EXIBICAO[int(mes_abertura) - 1]} "
                             f"de {ano}")
    else:
        r["efeito"] = f"01/01/{ano}"
        r["efeito_texto"] = f"retroativos a 1º de janeiro de {ano}, o próprio ano do excesso"
    return r


def obrigatoriedade_irpf(ano_calendario, rendimento_tributavel, regras=None):
    regras = regras or carregar_regras()
    item = regras.get("irpf", {}).get(str(ano_calendario))
    if not item:
        return None
    limite = _d(item["rendimentos_tributaveis"])
    obrigado = None if rendimento_tributavel is None else _d(rendimento_tributavel) > limite
    return {"ano_calendario": ano_calendario, "exercicio": item.get("exercicio", ano_calendario + 1),
            "limite": limite, "rendimento": rendimento_tributavel, "obrigado": obrigado,
            "igual_ao_limite": rendimento_tributavel is not None and _d(rendimento_tributavel) == limite,
            "prazo": item.get("prazo"), "isentos": item.get("isentos_exclusivos"),
            "bens": item.get("bens_direitos"), "rural": item.get("atividade_rural"),
            "fonte": item.get("fonte"), "validado_naf": bool(item.get("validado_naf"))}


# ====================================================================== fatos para a resposta
def _fato(tema, texto, resumo, chaves, fonte, ano, conclusao=None, proibidos=None, validado=False):
    """chaves: lista de grupos; cada grupo = alternativas, e ao menos uma deve aparecer na resposta."""
    return {"tema": tema, "texto": texto, "resumo": resumo, "chaves": chaves, "fonte": fonte,
            "ano": ano, "conclusao": conclusao, "proibidos": proibidos or [], "validado_naf": validado}


def _fato_irpf(sit, regras):
    ac, _ = sit["ano_irpf"] or (hoje().year - 1, hoje().year)
    r = obrigatoriedade_irpf(ac, sit["rendimento"], regras)
    if not r:
        return None
    lim, ex = brl(r["limite"]), r["exercicio"]
    prazo = f" O prazo de entrega da declaração de {ex} (ano-calendário {ac}) foi **{r['prazo']}**." if r["prazo"] else ""
    outros = (f"Os outros critérios de obrigatoriedade incluem: rendimentos isentos, não tributáveis ou "
              f"tributados só na fonte acima de {brl(r['isentos'])}; bens e direitos acima de {brl(r['bens'])} "
              f"em 31/12/{ac}; receita bruta de atividade rural acima de {brl(r['rural'])}; operações em bolsa "
              f"ou ganho de capital, entre outros.")
    chaves = [_alts(r["limite"])]
    if r["prazo"]:
        dia, mes, _a = r["prazo"].split("/")
        chaves.append([r["prazo"], f"{int(dia)} de {MESES_EXIBICAO[int(mes) - 1]}"])
    if r["obrigado"] is None:
        texto = (f"Para o IRPF {ex} (ano-calendário {ac}), é **obrigado a declarar quem recebeu rendimentos "
                 f"tributáveis acima de {lim}** no ano.{prazo} {outros}")
        return _fato("irpf", texto, f"IRPF {ex}: obrigatório para rendimentos tributáveis acima de {lim}.{prazo}",
                     chaves, r["fonte"], ac, validado=r["validado_naf"])
    rend = brl(r["rendimento"])
    if r["obrigado"]:
        texto = (f"**Sim, você é obrigado a entregar a declaração do IRPF {ex}.** Em {ac} você recebeu {rend} em "
                 f"rendimentos tributáveis, valor **acima do limite de {lim}**.{prazo}")
    else:
        comparacao = "igual ao limite" if r["igual_ao_limite"] else "abaixo do limite"
        texto = (f"**Não: pelo critério de rendimentos tributáveis, você está dispensado de entregar a declaração "
                 f"do IRPF {ex}.** Em {ac} você recebeu {rend}, valor {comparacao} de {lim} — a entrega só é "
                 f"obrigatória para quem recebeu **acima** desse valor. Se nenhum outro critério se aplica, "
                 f"declarar é opcional (vale a pena se houve imposto retido na fonte, para receber a restituição)."
                 f"{prazo} {outros}")
    return _fato("irpf", texto, texto.split("**")[1] if "**" in texto else texto, chaves, r["fonte"], ac,
                 conclusao={"tipo": "irpf_obrigado", "valor": r["obrigado"]}, validado=r["validado_naf"])


def _ano_faturamento(sit):
    if sit["abertura"] and sit["abertura"].get("ano"):
        return sit["abertura"]["ano"]
    return sit["anos"][-1] if sit["anos"] else hoje().year


def _resumo_excesso(r):
    lim = r["limite"]
    base = f"limite {'proporcional ' if lim['proporcional'] else ''}de {brl(lim['valor'])}"
    if not r["houve_excesso"]:
        return f"{base}: {brl(r['faturamento'])} fica dentro, **sem excesso**."
    faixa = "acima de 20%" if r["acima_tolerancia"] else "até 20%"
    return (f"{base}: **excesso de {brl(r['excesso'])} ({pct(r['percentual'])})**, {faixa} — desenquadramento com "
            f"efeitos {r['efeito_texto'].split(' (')[0]}.")


def _fato_excesso(sit, regras):
    if sit["faturamento"] is None or sit["faturamento"] <= 0:
        return None
    ano = _ano_faturamento(sit)
    ab = sit["abertura"] or {}
    r = avaliar_excesso(sit["faturamento"], ano, ab.get("mes"), ab.get("ano"), regras)
    if not r:
        return None
    previsto = avaliar_excesso(sit["faturamento"], ano, ab.get("mes"), ab.get("ano"), regras, usar_previsto=True)
    if previsto and (previsto["houve_excesso"], previsto["acima_tolerancia"]) != (r["houve_excesso"],
                                                                                  r["acima_tolerancia"]):
        # A conclusão muda conforme o teto novo (LC 238/2026) seja ou não confirmado: mostra os dois
        # cenários e NÃO fixa uma conclusão (para não "corrigir" uma resposta que cite o teto novo).
        texto = (f"**A resposta depende de o novo teto de {ano} (Lei Complementar nº 238/2026) entrar em vigor**, o "
                 f"que ainda depende da lei orçamentária. Faturamento informado: {brl(r['faturamento'])}.\n"
                 f"- **Pela regra em vigor hoje:** {_resumo_excesso(r)}\n"
                 f"- **Se o teto novo for confirmado:** {_resumo_excesso(previsto)}\n"
                 f"{r['comunicacao']}").strip()
        resumo = (f"Depende do teto de {ano}: regra atual — {_resumo_excesso(r)} Com a LC 238/2026 — "
                  f"{_resumo_excesso(previsto)}").replace("**", "")
        return _fato("excesso", texto, resumo, [_alts(r["limite"]["valor"]), _alts(previsto["limite"]["valor"])],
                     r["fonte"], ano)
    lim = r["limite"]
    if lim["proporcional"]:
        mes_nome = MESES_EXIBICAO[int(ab["mes"]) - 1]
        base = (f"Como o MEI foi aberto em **{mes_nome} de {ano}**, o limite de {ano} é **proporcional**: "
                f"{brl(_d(lim['valor']) / lim['meses'])} por mês × {lim['meses']} meses (contando o mês de abertura) = "
                f"**{brl(lim['valor'])}** — e não o teto anual.")
    else:
        base = f"O limite de faturamento do MEI em {ano} é de **{brl(lim['valor'])}** no ano."
    aviso = f" Observação: {lim['aviso']}." if lim.get("aviso") else ""
    aviso += _texto_previsto(lim)
    chaves = [_alts(lim["valor"])]
    if not r["houve_excesso"]:
        texto = (f"**Não houve excesso.** {base} O faturamento informado, {brl(r['faturamento'])}, está dentro desse "
                 f"limite.{aviso}")
        return _fato("excesso", texto, f"Sem excesso: {brl(r['faturamento'])} dentro do limite de {brl(lim['valor'])}.",
                     chaves, r["fonte"], ano, conclusao={"tipo": "houve_excesso", "valor": False})
    exc, p = brl(r["excesso"]), pct(r["percentual"])
    chaves.append(_alts(r["excesso"]))
    proibidos = []
    if r["acima_tolerancia"]:
        faixa = "**mais de 20%** do limite"
        if r["ano_de_abertura"]:
            mes_nome = MESES_EXIBICAO[int(ab["mes"]) - 1]
            chaves.append([f"{mes_nome} de {ano}", f"{int(ab['mes']):02d}/{ano}", f"{mes_nome}/{ano}",
                           f"{mes_nome[:3]}/{ano}"])
        else:
            chaves.append([f"01/01/{ano}", f"janeiro de {ano}"])
        proibidos = [rf"\b0?1/0?1/{ano + 1}\b", rf"\b1(?:º|o|°)?\s*de\s*janeiro\s*de\s*{ano + 1}\b",
                     rf"\bjaneiro\s*de\s*{ano + 1}\b", rf"\bnao\s+(?:e|sera|ha)\s+retroativ"]
        consequencia = (f"Por isso o desenquadramento tem efeitos **{r['efeito_texto']}**: desde então, os tributos "
                        f"são recalculados como microempresa (ME) do Simples Nacional, e não pelo valor fixo do DAS do MEI.")
    else:
        faixa = "**até 20%** do limite"
        chaves.append([f"01/01/{ano + 1}", f"janeiro de {ano + 1}"])
        proibidos = [r"\bretroativ"]
        consequencia = f"Por isso o desenquadramento tem efeitos **{r['efeito_texto']}**."
    texto = (f"**Sim, houve excesso de {exc} ({p})**, que corresponde a {faixa}. {base} {consequencia} "
             f"{r['comunicacao']}{aviso}").strip()
    resumo = (f"Limite {'proporcional ' if lim['proporcional'] else ''}{brl(lim['valor'])}; excesso {exc} ({p}); "
              f"desenquadramento com efeitos {r['efeito_texto'].split(' (')[0]}.")
    return _fato("excesso", texto, resumo, chaves, r["fonte"], ano,
                 conclusao={"tipo": "houve_excesso", "valor": True}, proibidos=proibidos)


def _texto_previsto(lim):
    """Frase sobre o limite da lei nova ainda não em vigor (ex.: 2027 pela LC 238/2026)."""
    p = lim.get("previsto") if lim else None
    if not p:
        return ""
    conta = (f"{brl(p['mensal'])} × {lim['meses']} meses = **{brl(p['valor'])}**" if lim.get("proporcional")
             else f"**{brl(p['valor'])}**")
    return (f" Se o novo teto de {p['ano']} da Lei Complementar nº 238/2026 entrar em vigor (depende da lei "
            f"orçamentária), o limite passa a ser {conta}; até lá, vale o cálculo acima.")


_PROIBIDOS_ISS = [r"\biss\b[^.\n]{0,40}r\$\s*6(?:,00)?\b", r"r\$\s*6(?:,00)?\b[^.\n]{0,20}\biss\b",
                  r"\biss\b[^.\n]{0,60}\b(?:depende|varia|conforme)\b[^.\n]{0,40}(?:municip|aliquota|cidade)",
                  r"\biss\b[^.\n]{0,60}\b2\s*%\s*(?:e|a)\s*5\s*%"]


def _fato_das(sit, regras):
    ano = sit["anos"][-1] if sit["anos"] else hoje().year
    v = valores_das(ano, regras)
    if not v:
        return None
    regra = regras.get("das_regras", {})
    aviso = f" Observação: {v['aviso']}." if v["aviso"] else ""
    inss = f"{brl(v['inss'])} de INSS (5% do salário mínimo de {brl(v['salario_minimo'])})"
    tipos = {"servicos": (f"**{brl(v['servicos'])} por mês** para o MEI que presta só serviços: {inss} + "
                          f"{brl(v['iss'])} de ISS", v["servicos"]),
             "comercio": (f"**{brl(v['comercio'])} por mês** para o MEI de comércio ou indústria: {inss} + "
                          f"{brl(v['icms'])} de ICMS", v["comercio"]),
             "misto": (f"**{brl(v['misto'])} por mês** para o MEI de comércio e serviços: {inss} + {brl(v['icms'])} "
                       f"de ICMS + {brl(v['iss'])} de ISS", v["misto"])}
    ativ = sit["atividade"]
    escolhidos = [ativ] if ativ in tipos else ["comercio", "servicos", "misto"]
    linhas = [tipos[a][0] for a in escolhidos]
    if len(linhas) == 1:
        texto = f"Em {v['ano']}, o DAS do MEI é de {linhas[0]}."
    else:
        texto = f"Em {v['ano']}, o DAS do MEI (valor fixo mensal) é de:\n" + "\n".join(f"- {l}" for l in linhas)
    texto += (f" {regra.get('texto_iss', '')} O vencimento é no {regra.get('vencimento', 'dia 20 de cada mês')}, "
              f"e o DAS é devido mesmo nos meses sem faturamento.{aviso}")
    chaves = [[f"{tipos[a][1]:.2f}".replace(".", ",")] for a in escolhidos]
    return _fato("das_valor", texto.strip(), "; ".join(l.replace("**", "") for l in linhas) + ".", chaves,
                 v["fonte"], v["ano"], proibidos=_PROIBIDOS_ISS, validado=v["validado_naf"])


def _fato_sem_faturamento(sit, regras):
    ano = sit["anos"][-1] if sit["anos"] else hoje().year - 1
    dados, usado, _ = dados_mei(ano, regras)
    v = valores_das(ano, regras)
    if not dados or not v:
        return None
    prazo = (regras.get("mei", {}).get(str(ano)) or {}).get("prazo_dasn") or f"31/05/{ano + 1}"
    texto = (f"**Sim, as duas obrigações continuam valendo mesmo sem faturamento.**\n"
             f"- **DAS mensal: é devido todos os meses** em que o MEI ficou ativo em {ano}, mesmo com faturamento "
             f"zero (em {v['ano']}: {brl(v['comercio'])} comércio/indústria, {brl(v['servicos'])} serviços, "
             f"{brl(v['misto'])} comércio e serviços). Os meses não pagos viram dívida com multa e juros.\n"
             f"- **Declaração anual (DASN-SIMEI)**: deve ser entregue informando receita **zero**, até **{prazo}**.")
    return _fato("sem_faturamento", texto,
                 f"Sem faturamento, o DAS mensal continua devido e a DASN-SIMEI de {ano} deve ser entregue com "
                 f"receita zero até {prazo}.",
                 [[prazo, f"31 de maio de {prazo[-4:]}"], ["das"], ["devid*", "pagar", "pago", "paga", "obrigat*"]],
                 v["fonte"], ano, conclusao={"tipo": "das_sem_faturamento", "valor": True})


def _fato_teto(sit, regras):
    tabela = regras.get("mei", {})
    anos = sorted({a for a in sit["anos"] if str(a) in tabela}) or \
        sorted({a for a in (hoje().year, hoje().year + 1, hoje().year + 2) if str(a) in tabela})
    if not anos:
        return None
    linhas, chaves, fonte = [], [], None
    for a in anos:
        item = tabela[str(a)]
        valor = brl(item["teto_anual"])
        if item.get("vigente") is False:
            linhas.append(f"- **{a}: {valor} previsto**, {item.get('condicao', '')}.")
        else:
            linhas.append(f"- **{a}: {valor} por ano** (ou {brl(item['limite_mensal_proporcional'])} por mês de "
                          f"atividade no ano de abertura).")
        chaves.append(_alts(item["teto_anual"]))
        fonte = fonte or item.get("fonte")
    texto = "Teto de faturamento do MEI:\n" + "\n".join(linhas)
    return _fato("teto", texto, " ".join(l.lstrip("- ").replace("**", "") for l in linhas), chaves,
                 tabela[str(anos[-1])].get("fonte") or fonte, anos[-1])


def _fato_dasn(sit, regras):
    ano = sit["anos"][-1] if sit["anos"] else hoje().year - 1
    item = regras.get("mei", {}).get(str(ano))
    if not item or not item.get("prazo_dasn"):
        return None
    texto = (f"A declaração anual do MEI (DASN-SIMEI) referente a {ano} deve ser entregue até "
             f"**{item['prazo_dasn']}**, mesmo que o faturamento tenha sido zero.")
    return _fato("dasn", texto, texto.replace("**", ""), [[item["prazo_dasn"]]], item.get("fonte"), ano)


def _fato_dirf(sit, regras):
    item = regras.get("dirf")
    if not item:
        return None
    ano = int(item.get("extinta_a_partir_de", 2025))
    anos_ref = [a for a in sit["anos"] if a >= ano]
    inicio = "**Não.** " if anos_ref or not sit["anos"] else ""
    texto = inicio + item["texto"]
    return _fato("dirf", texto, item["texto"], [["esocial"], ["reinf"]], item.get("fonte"), ano,
                 conclusao={"tipo": "dirf_extinta", "valor": True} if inicio else None)


def _fato_empregados(sit, regras):
    """Quantos empregados o MEI pode ter, por ano (LC 238/2026: até 2 a partir de 01/01/2027)."""
    tabela = {a: v for a, v in regras.get("empregados", {}).items() if a.isdigit() and isinstance(v, dict)}
    if not tabela:
        return None
    pedidos = sorted({a for a in sit["anos"] if str(a) in tabela})
    if not pedidos:
        pedidos = sorted({a for a in (hoje().year, hoje().year + 1) if str(a) in tabela})
    if not pedidos:   # ano pedido sem cadastro: usa o último ano cadastrado que já vale
        anteriores = sorted(int(a) for a in tabela if int(a) <= (sit["anos"][-1] if sit["anos"] else hoje().year))
        pedidos = anteriores[-1:]
    if not pedidos:
        return None
    linhas, chaves, validado = [], [], True
    for a in pedidos:
        item = tabela[str(a)]
        n = int(item["maximo"])
        quando = f"A partir de {item['vigencia']}" if item.get("vigencia") else f"Em {a}"
        if n == 1:
            linhas.append(f"**{quando}, o MEI pode ter 1 empregado**, que receba exclusivamente 1 salário mínimo ou "
                          f"o piso da categoria.")
            chaves.append(["1 empregado", "um empregado", "1 funcionario", "um funcionario", "um unico", "apenas um"])
        else:
            extra = f" Também {item['observacao']}." if item.get("observacao") else ""
            linhas.append(f"**{quando}, o MEI pode ter até {n} empregados** (Lei Complementar nº 238/2026), cada um "
                          f"recebendo exclusivamente 1 salário mínimo ou o piso da categoria.{extra}")
            chaves.append([f"{n} empregados", "dois empregados", f"{n} funcionarios", "dois funcionarios",
                           f"ate {n}", "ate dois"])
        validado = validado and bool(item.get("validado_naf"))
    texto = linhas[0] if len(linhas) == 1 else "\n".join(f"- {l}" for l in linhas)
    ultimo = tabela[str(pedidos[-1])]
    return _fato("empregados", texto, " ".join(l.replace("**", "") for l in linhas), chaves, ultimo.get("fonte"),
                 pedidos[-1], validado=validado)


def _fato_remissao(sit, regras):
    """Perdão das dívidas do MEI com a União (LC 238/2026)."""
    item = regras.get("remissao_lc238")
    if not item or not item.get("texto"):
        return None
    venc = item.get("vencimento_ate", "")
    efeito = item.get("efeitos_a_partir_de", "")
    chaves = []
    if venc:
        dia, mes, ano = venc.split("/")
        chaves.append([venc, f"{int(dia)} de {MESES_EXIBICAO[int(mes) - 1]}"])
    chaves.append(["automatic*", "de oficio", "sem pedido", "sem precisar pedir", "nao precisa pedir",
                   "nao e preciso pedir", "sem necessidade de pedido"])
    resumo = (f"LC 238/2026: dívidas da contribuição previdenciária do MEI vencidas até {venc} são perdoadas "
              f"automaticamente a partir de {efeito} (ICMS e ISS não entram); valores pagos não são devolvidos.")
    ano = int(efeito[-4:]) if efeito[-4:].isdigit() else None
    return _fato("remissao", item["texto"], resumo, chaves, item.get("fonte"), ano,
                 validado=bool(item.get("validado_naf")))


def _fato_limite_abertura(sit, regras):
    """Limite proporcional do ano de abertura, quando a pessoa ainda não disse quanto faturou."""
    ab = sit["abertura"]
    lim = limite_mei(ab["ano"], ab["mes"], regras)
    dados, _, _ = dados_mei(ab["ano"], regras, CAMPOS_LIMITE)
    if not lim or not dados or not lim["proporcional"]:
        return None
    mes_nome = MESES_EXIBICAO[int(ab["mes"]) - 1]
    ano = ab["ano"]
    tolerancia = _d(regras.get("desenquadramento", {}).get("percentual_tolerancia", 20))
    teto_tol = lim["valor"] * (1 + tolerancia / 100)
    periodo = f"{mes_nome} a dezembro" if int(ab["mes"]) < 12 else "dezembro"
    texto = (f"Como o MEI foi aberto em **{mes_nome} de {ano}**, o limite de faturamento de {ano} é proporcional: "
             f"{brl(dados['limite_mensal_proporcional'])} × {lim['meses']} meses ({periodo}, contando o mês de "
             f"abertura) = **{brl(lim['valor'])}**. Nesse primeiro ano não vale o teto anual de "
             f"{brl(dados['teto_anual'])}. Se o faturamento passar do limite em até 20% (até {brl(teto_tol)}), o "
             f"desenquadramento vale a partir de 1º de janeiro de {ano + 1}; acima disso, é retroativo ao início "
             f"da atividade ({mes_nome} de {ano}).")
    if lim.get("aviso"):
        texto += f" Observação: {lim['aviso']}."
    texto += _texto_previsto(lim)
    proibidos = [r"\blimite\b[^.]{0,30}\b(?:e|sera|fica|continua)\b[^.]{0,25}\b81(?:[.,]000| mil)"]
    return _fato("limite_abertura", texto,
                 f"Limite proporcional de {ano} para MEI aberto em {mes_nome}: {brl(lim['valor'])} "
                 f"({lim['meses']} × {brl(dados['limite_mensal_proporcional'])}).",
                 [_alts(lim["valor"])], lim.get("fonte"), ano, proibidos=proibidos)


_CALCULADORAS = [("irpf", _fato_irpf), ("excesso", _fato_excesso), ("limite_abertura", _fato_limite_abertura),
                 ("sem_faturamento", _fato_sem_faturamento),
                 ("das_valor", _fato_das), ("teto", _fato_teto), ("dasn", _fato_dasn), ("dirf", _fato_dirf),
                 ("empregados", _fato_empregados), ("remissao", _fato_remissao)]


# ============================================================ dados sugeridos pelo modelo (validados)
def _numero(valor):
    """50000, 50000.0, "50.000,00", "50 mil" -> Decimal; None se não der para ler."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return _d(valor)
    achados = _valores_monetarios(f"R$ {valor}")
    return achados[0][1] if achados else None


def _inteiro(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def validar_dados_llm(dados, pergunta, historico=None):
    """Fica só com o que a reescrita do modelo sugeriu E dá para confirmar no texto do USUÁRIO.

    - faturamento/rendimento: o mesmo valor tem de aparecer como dinheiro numa mensagem do usuário;
    - mês/ano de abertura: o mês (nome, abreviação ou "07/2026") e o ano têm de estar no texto;
    - atividade: precisa de "evidencia_atividade", um trecho LITERAL do usuário (ex.: "sou manicure");
    - temas: só os nomes conhecidos (TEMAS_VALIDOS).
    Devolve um dicionário só com os campos aceitos (vazio se nada confere).
    """
    if not isinstance(dados, dict):
        return {}
    textos = _mensagens_usuario(historico) + [str(pergunta or "")]
    todo = _sem_acento("\n".join(textos))
    valores = {v for t in textos for _, v in _valores_monetarios(t)}
    aceitos = {}
    for campo in ("faturamento", "rendimento"):
        v = _numero(dados.get(campo))
        if v is not None and v > 0 and v in valores:
            aceitos[campo] = v
    mes, ano = _inteiro(dados.get("mes_abertura")), _inteiro(dados.get("ano_abertura"))
    if mes and 1 <= mes <= 12:
        nome = MESES[mes - 1]
        if re.search(rf"\b{nome}\b|\b{nome[:3]}\s*/\s*20\d\d|\b0?{mes}\s*/\s*20\d\d\b", todo):
            if ano and not re.search(rf"(?<!\d){ano}(?!\d)", todo):
                ano = None
            aceitos["abertura"] = {"mes": mes, "ano": ano}
    atividade, evidencia = dados.get("atividade"), dados.get("evidencia_atividade")
    if atividade in ("servicos", "comercio", "misto") and isinstance(evidencia, str):
        ev = re.sub(r"\s+", " ", _sem_acento(evidencia)).strip(" .,!?\"'")
        if len(ev) >= 3 and ev in re.sub(r"\s+", " ", todo):
            aceitos["atividade"] = atividade
    temas = dados.get("temas")
    if isinstance(temas, list):
        validos = {t for t in temas if isinstance(t, str) and t in TEMAS_VALIDOS}
        if validos:
            aceitos["temas"] = validos
    return aceitos


def completar_situacao(sit, extras):
    """Junta os dados aceitos do modelo à situação lida pelas regex (o que a regex leu sempre vale)."""
    if not extras:
        return sit
    for campo, outro in (("faturamento", "rendimento"), ("rendimento", "faturamento")):
        v = extras.get(campo)
        if v is not None and sit.get(campo) is None and sit.get(outro) != v:
            sit[campo] = v
    if extras.get("abertura") and not sit.get("abertura"):
        ab = dict(extras["abertura"])
        if ab.get("ano") is None:
            ab["ano"] = sit["anos"][-1] if sit.get("anos") else hoje().year
        sit["abertura"] = ab
    if extras.get("atividade") and not sit.get("atividade"):
        sit["atividade"] = extras["atividade"]
    if extras.get("temas") and not sit.get("temas"):
        sit["temas"] = set(extras["temas"])
    return sit


def analisar(pergunta, historico=None, extras=None, regras=None):
    """(fatos, situação) da pergunta. `extras` = dados do modelo já validados (validar_dados_llm)."""
    regras = regras or carregar_regras()
    sit = completar_situacao(extrair_situacao(pergunta, historico), extras)
    return (_fatos_da_situacao(sit, pergunta, regras) if regras else []), sit


def fatos_para(pergunta, historico=None, regras=None, extras=None):
    """Lista de fatos calculados para a pergunta (vazia se nenhuma regra se aplica)."""
    return analisar(pergunta, historico, extras, regras)[0]


def _fatos_da_situacao(sit, pergunta, regras):
    temas = set(sit["temas"])
    tem_faturamento = sit["faturamento"] is not None and sit["faturamento"] > 0
    if tem_faturamento and (sit["abertura"] or "teto" in temas or "limite" in temas):
        if re.search(r"fatur", _sem_acento(pergunta)) or temas & {"excesso", "limite"}:
            temas.add("excesso")
    if "excesso" in temas and tem_faturamento:
        temas.discard("teto")          # o fato do excesso já traz o limite certo (proporcional ou anual)
    elif sit["abertura"] and "excesso" in temas:
        temas.add("limite_abertura")   # "houve excesso?" sem o valor: mostra o limite que vale
    elif sit["abertura"] and temas & {"teto", "limite"}:
        # "qual o limite de quem abriu em julho de 2026?" -> limite PROPORCIONAL, não o teto anual
        outros_anos = [a for a in sit["anos"] if a != sit["abertura"]["ano"]]
        temas.add("limite_abertura")
        if not outros_anos:
            temas.discard("teto")
    elif "limite" in temas:
        temas.add("teto")
    if "sem_faturamento" in temas:
        temas.discard("das_valor")
        temas.discard("dasn")          # já fazem parte do fato "sem faturamento"
    if "irpf" in temas:
        temas.discard("dasn")
    fatos = []
    for tema, calc in _CALCULADORAS:
        if tema in temas:
            try:
                f = calc(sit, regras)
            except (KeyError, ValueError, TypeError, IndexError) as erro:   # regra mal preenchida no JSON
                print(f"[REGRAS] não consegui calcular '{tema}': {type(erro).__name__}: {erro}", flush=True)
                f = None
            if f:
                fatos.append(f)
    return fatos


# =================================================================== conferência da resposta
_NAO_ACHEI = re.compile(r"nao (encontrei|consegui encontrar|localizei|achei)|nao (foi|foram) encontrad|"
                        r"nao (consta|constam|ha informac)|nao tenho (essa|esta|a) informac")
_HIPOTESE = re.compile(r"\b(se|caso|seria|seriam|fosse|fossem|hipotese|outras empresas|demais empresas)\b")


def _normalizar_resposta(texto):
    t = _sem_acento(texto).replace("*", "").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", t)


def _primeira_frase(t):
    t = t.strip()
    m = re.search(r"(?<=[.!?:])\s|\n", t)
    return t[:m.start()] if m and m.start() > 15 else t[:300]


def _frases(t):
    return [f for f in re.split(r"(?<=[.!?;])\s+|\n+", t) if f.strip()]


def _postura(frase, tipo):
    """True (afirma), False (nega) ou None, para a conclusão `tipo` na frase."""
    if tipo == "irpf_obrigado":
        neg = re.search(r"\bnao\b[^.]{0,25}\b(precisa|e obrigad|esta obrigad|sera obrigad|tem obrigac|deve|"
                        r"e necessari|e obrigatori)|dispensad|desobrigad|\bopcional\b|facultativ|nao esta entre", frase)
        pos = re.search(r"^\W*sim\b|\b(precisa|deve) (sim )?(entregar|declarar|fazer)|\b(e|esta|sera) obrigad|"
                        r"obrigatori[oa] (entregar|declarar|a entrega)|(supera|ultrapassa|excede)\w* o limite", frase)
    elif tipo == "houve_excesso":
        neg = re.search(r"\bnao\b[^.]{0,15}\b(houve|ha|teve|ocorreu|existe)\b[^.]{0,10}excesso|\bnao\b[^.]{0,15}"
                        r"(ultrapass|exced|passou|estour)|dentro do limite|abaixo do limite|sem excesso", frase)
        pos = re.search(r"^\W*sim\b|\b(houve|ha|teve|ocorreu)\b[^.]{0,10}excesso|\b(ultrapass|exced|estour)", frase)
    elif tipo == "das_sem_faturamento":
        neg = re.search(r"\bnao\b[^.]{0,30}\b(precisa|tem que|deve|e devido|e obrigat)[^.]{0,20}\b(pag|das)|"
                        r"\b(isento|dispensad)\w*[^.]{0,20}\bdas", frase)
        pos = re.search(r"\b(e devido|continua devido|deve pagar|precisa pagar|tem que pagar|obrigatori)", frase)
    elif tipo == "dirf_extinta":
        neg = re.search(r"^\W*nao\b|extint|substituid|deixou de|nao (existe|e mais)", frase)
        pos = re.search(r"^\W*sim\b|\b(ainda|continua)\b[^.]{0,20}(entreg|obrigat|precis)", frase)
        return False if neg and not pos else (True if pos else None)   # True aqui = "ainda precisa"
    else:
        return None
    if neg:
        return False
    if pos:
        return True
    return None


def contradiz(resposta, fatos):
    """Lista de motivos em que a resposta contradiz algum fato (vazia = sem contradição)."""
    if not resposta or not fatos:
        return []
    t = _normalizar_resposta(resposta)
    motivos = []
    if _NAO_ACHEI.search(t[:400]):
        motivos.append("disse que não encontrou a informação, mas a regra está cadastrada")
    frase1 = _primeira_frase(t)
    for f in fatos:
        c = f.get("conclusao")
        if c:
            esperado = c["valor"]
            if c["tipo"] == "dirf_extinta":
                postura = _postura(frase1, c["tipo"])
                if postura is True:
                    motivos.append("disse que a DIRF ainda é exigida")
            else:
                postura = _postura(frase1, c["tipo"])
                if postura is not None and postura != esperado:
                    motivos.append(f"conclusão oposta à regra ({c['tipo']} = {esperado})")
        for padrao in f.get("proibidos", []):
            for frase in _frases(t):
                if re.search(padrao, frase) and not _HIPOTESE.search(frase):
                    motivos.append(f"afirmação incompatível com a regra de {f['tema']}: {frase[:120]!r}")
                    break
    return list(dict.fromkeys(motivos))


def faltando(resposta, fatos):
    """Fatos cujos valores-chave não aparecem na resposta."""
    t = _normalizar_resposta(resposta or "")
    saida = []
    for f in fatos:
        for grupo in f.get("chaves", []):
            if not any(_contem(t, alt) for alt in grupo):
                saida.append(f)
                break
    return saida


def _contem(texto_normalizado, alternativa):
    """Procura a alternativa como palavra inteira ('das' não casa com 'dasn'); 'obrigat*' = prefixo."""
    alt = _sem_acento(alternativa).strip()
    prefixo = alt.endswith("*")
    alt = alt.rstrip("*")
    padrao = r"(?<![a-z0-9])" + r"\s*".join(map(re.escape, alt.split())) + ("" if prefixo else r"(?![a-z0-9])")
    return re.search(padrao, texto_normalizado) is not None


def resposta_dos_fatos(fatos):
    """Resposta montada só com os fatos (usada quando a do modelo contradiz a regra)."""
    corpo = "\n\n".join(f["texto"] for f in fatos)
    anos = sorted({f["ano"] for f in fatos if f.get("ano")})
    ref = f" de {', '.join(map(str, anos))}" if anos else ""
    return (f"{corpo}\n\n_Resposta calculada com as regras oficiais{ref} cadastradas no FiscOn. Para analisar o "
            f"seu caso, fale com o NAF da FMU._")


def garantir(resposta, fatos):
    """Confere a resposta com os fatos. Devolve (resposta_final, ajuste, motivos).

    ajuste: None (resposta já correta), "complementada" (acrescentou o que faltava) ou
            "corrigida" (a resposta contradizia a regra e foi trocada pela dos fatos).
    """
    if not fatos:
        return resposta, None, []
    motivos = contradiz(resposta, fatos)
    if motivos:
        return resposta_dos_fatos(fatos), "corrigida", motivos
    faltam = faltando(resposta, fatos)
    if not faltam:
        return resposta, None, []
    extra = "\n".join(f"- {f['resumo']}" for f in faltam)
    return (f"{(resposta or '').rstrip()}\n\n**Conferência automática (regras oficiais):**\n{extra}",
            "complementada", [f"faltou: {f['tema']}" for f in faltam])


def fatos_publicos(fatos):
    """Versão dos fatos enviada à tela (sem padrões internos)."""
    return [{"tema": f["tema"], "resumo": f["resumo"], "fonte": f.get("fonte") or "", "ano": f.get("ano"),
             "validado_naf": bool(f.get("validado_naf"))} for f in fatos]


def bloco_prompt(fatos):
    """Texto dos fatos para o prompt do modelo."""
    if not fatos:
        return ""
    linhas = "\n\n".join(f"FATO {i} ({f['tema']}, ano {f.get('ano')}):\n{f['texto']}" for i, f in enumerate(fatos, 1))
    return ("FATOS CALCULADOS pelas regras oficiais cadastradas (valores, limites, prazos e conclusões já "
            "conferidos; PREVALECEM sobre os trechos dos documentos e sobre o seu conhecimento):\n" + linhas)


def pendentes_validacao(regras=None):
    """Regras cadastradas que a equipe do NAF ainda não conferiu (validado_naf != true).

    Aparece nos detalhes de /api/status e no aviso do servidor ao iniciar, para ninguém esquecer.
    Ex.: ["mei/2026", "irpf/2025", "remissao_lc238"].
    """
    regras = regras or carregar_regras()
    pendentes = []
    for chave, valor in regras.items():
        if not isinstance(valor, dict) or chave.startswith("_"):
            continue
        if "validado_naf" in valor and not valor.get("validado_naf"):
            pendentes.append(chave)
        for sub, item in valor.items():
            if isinstance(item, dict) and "validado_naf" in item and not item.get("validado_naf"):
                pendentes.append(f"{chave}/{sub}")
    return pendentes
