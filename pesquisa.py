"""Pesquisa GERAL nas páginas oficiais (SOMENTE Sebrae e Receita Federal) — para qualquer pergunta.

Por que existe
  A conferência com os sites dependia de (1) páginas já conhecidas em fontes_oficiais.json e (2) do
  DuckDuckGo para achar o resto. O DuckDuckGo bloqueia o servidor (anti-robô), então qualquer
  assunto fora da lista ficava sem página e a conferência terminava em "nenhuma trata deste ponto".

O que faz
  1. Catálogo de endereços: lê os sitemaps dos sites oficiais (lista em fontes_oficiais.json,
     "sitemaps") e guarda endereço + título de milhares de páginas. Atualizado a cada coleta.
  2. Busca interna dos sites que têm API aberta (ex.: blog do Sebrae RN, WordPress), "buscas" em
     fontes_oficiais.json.
  3. Pesquisa pelos termos da PRÓPRIA RESPOSTA: para conferir "não pode ser sócio de outra empresa,
     exercer atividade permitida...", procura páginas que tragam esses termos. Funciona para
     qualquer assunto, sem lista de perguntas.
  4. Relevância por cobertura ponderada: uma página só conta como "sobre o assunto" se trouxer boa
     parte dos termos ESPECÍFICOS (palavras raras valem mais que "MEI", que está em todas).

Tudo o que é baixado passa por verificador.baixar() (só https de Sebrae e Receita Federal, cada
redirecionamento validado). Os resultados de busca/sitemap só fornecem ENDEREÇOS; o texto usado na
conferência vem sempre das próprias páginas oficiais.
"""
import json
import math
import os
import re
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config as C
import verificador as V

ATIVA = C.PESQUISA_ATIVA   # 0 = só a base local e fontes_oficiais.json (testes offline)
LIMIAR_RELEVANCIA = C.PESQUISA_LIMIAR   # cobertura mínima "sobre o assunto"
MAX_CATALOGO = 6000          # endereços guardados
MAX_FILHOS_SITEMAP = 8       # sitemaps de um índice de sitemaps
MAX_PAGINAS_INDICE = 30      # páginas-índice de 2º nível lidas por coleta (só para tirar links)
TEMPO_REFORCO = 14           # segundos para ler páginas novas depois do rascunho
MAX_LEITURAS = 4             # páginas novas lidas por pergunta

# Usados se fontes_oficiais.json não tiver "sitemaps" / "buscas".
SITEMAPS_PADRAO = [
    {"url": "https://sebrae.com.br/sitemap.xml", "filtro": r"guiaparaomei|subsites/mei|empreendedores/conteudos"},
    {"url": "https://blog.rn.sebrae.com.br/wp-sitemap.xml", "seguir": r"post-sitemap|page-sitemap"},
    {"url": "https://www.gov.br/receitafederal/sitemap.xml", "filtro": r"/receitafederal/pt-br/"},
]
# Páginas cheias de links (menus, listas de serviços): os links entram no CATÁLOGO, com o texto do
# link como título (a página em si não é guardada). Cobre o que os sitemaps não listam.
PAGINAS_DE_LINKS_PADRAO = [
    {"url": "https://www.gov.br/receitafederal/pt-br/assuntos", "seguir": r"/receitafederal/pt-br/(assuntos|servicos)/"},
    {"url": "https://www.gov.br/receitafederal/pt-br/servicos", "seguir": r"/receitafederal/pt-br/(assuntos|servicos)/"},
    {"url": "https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cadastros",
     "seguir": r"/receitafederal/pt-br/(assuntos|servicos)/"},
    {"url": "https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/perguntas-frequentes",
     "seguir": r"/receitafederal/pt-br/acesso-a-informacao/perguntas-frequentes/"},
    {"url": "https://www8.receita.fazenda.gov.br/SimplesNacional/", "seguir": r"SimplesNacional/"},
]
BUSCAS_PADRAO = [
    {"nome": "Blog do Sebrae RN", "url": "https://blog.rn.sebrae.com.br/wp-json/wp/v2/search?per_page=6&search={q}",
     "tipo": "wp-json"},
]

_trava = threading.Lock()
_catalogo = {"itens": {}, "atualizado_em": None, "idf": {}, "tokens": {}}


def _log(msg):
    print(f"[PESQUISA] {msg}", flush=True)


def _base_web():
    import base_web
    return base_web


def _tokens(texto):
    return _base_web()._tokens(texto)


# ================================================================== catálogo de endereços
def _config():
    try:
        dados = json.loads(V.ARQ_SEMENTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    sitemaps = [s for s in dados.get("sitemaps", SITEMAPS_PADRAO) if isinstance(s, dict) and s.get("url")]
    buscas = [b for b in dados.get("buscas", BUSCAS_PADRAO) if isinstance(b, dict) and b.get("url")]
    return sitemaps, buscas


def _paginas_de_links():
    try:
        dados = json.loads(V.ARQ_SEMENTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    return [p for p in dados.get("paginas_de_links", PAGINAS_DE_LINKS_PADRAO) if isinstance(p, dict) and p.get("url")]


def links_com_titulo(url, seguir=None):
    """[(endereço, texto do link)] de uma página oficial (menus e listas de serviços)."""
    codigo, corpo, cab = V.baixar(url)
    if codigo != 200 or not corpo:
        return []
    html = V._decodificar(corpo, (cab or {}).get("Content-Type", "")) or ""
    saida, vistos = [], set()
    for href, miolo in re.findall(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", html, re.S | re.I):
        endereco = urllib.parse.urljoin(url, href.replace("&amp;", "&").strip())
        endereco = re.sub(r"/(view|@@display-file/file)$", "", endereco)
        texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", miolo)).strip()
        if (endereco in vistos or not V.url_permitida(endereco) or V.pagina_de_listagem(endereco)
                or (seguir and not re.search(seguir, endereco, re.I))):
            continue
        vistos.add(endereco)
        saida.append((endereco, texto[:160]))
    return saida


def _titulo_do_endereco(url):
    """'.../querosermei/atividades-permitidas-no-mei' -> 'querosermei atividades permitidas no mei'."""
    caminho = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    partes = [p for p in caminho.strip("/").split("/") if p and not re.fullmatch(r"pt-br|20\d\d|\d+", p)][-3:]
    return re.sub(r"[-_.]+", " ", " ".join(partes)).strip()


def ler_sitemap(url, filtro=None, seguir=None, _nivel=0):
    """[(endereço, data)] de um sitemap (segue índices de sitemaps um nível)."""
    codigo, corpo, cab = V.baixar(url, aceitar=("xml", "text/plain", "html"), max_bytes=8_000_000)
    if codigo != 200 or not corpo:
        return []
    if corpo[:2] == b"\x1f\x8b":
        import gzip
        try:
            corpo = gzip.decompress(corpo)
        except OSError:
            return []
    xml = corpo.decode("utf-8", "replace")
    if "<sitemapindex" in xml and _nivel == 0:
        filhos = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
                  if V.url_permitida(u) and (not seguir or re.search(seguir, u))][:MAX_FILHOS_SITEMAP]
        saida = []
        for filho in filhos:
            saida.extend(ler_sitemap(filho, filtro, None, _nivel + 1))
        return saida
    saida = []
    for bloco in re.findall(r"<url>(.*?)</url>", xml, re.S):
        m = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", bloco)
        if not m:
            continue
        endereco = m.group(1).replace("&amp;", "&")
        if not V.url_permitida(endereco) or V.pagina_de_listagem(endereco):
            continue
        if filtro and not re.search(filtro, endereco, re.I):
            continue
        data = re.search(r"<lastmod>\s*([^<\s]+)", bloco)
        saida.append((endereco, data.group(1)[:10] if data else ""))
    return saida


def _reindexar_catalogo():
    itens = _catalogo["itens"]
    tokens = {u: set(_tokens(f"{i.get('titulo', '')} {_titulo_do_endereco(u)}")) for u, i in itens.items()}
    df = Counter(t for ts in tokens.values() for t in ts)
    n = len(tokens) or 1
    _catalogo["tokens"] = tokens
    _catalogo["idf"] = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}


def adicionar_ao_catalogo(enderecos, origem="coleta"):
    """Acrescenta endereços (ex.: links achados na coleta) ao catálogo."""
    with _trava:
        for item in enderecos:
            endereco, titulo = (item, "") if isinstance(item, str) else item
            if V.url_permitida(endereco) and not V.pagina_de_listagem(endereco):
                atual = _catalogo["itens"].setdefault(endereco, {"titulo": "", "data": "", "origem": origem})
                if titulo and not atual.get("titulo"):
                    atual["titulo"] = titulo[:160]
        _reindexar_catalogo()


def atualizar_catalogo():
    """Lê todos os sitemaps configurados. Mantém os endereços antigos se um site estiver fora do ar."""
    sitemaps, _ = _config()
    novos = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for lista in ex.map(lambda s: ler_sitemap(s["url"], s.get("filtro"), s.get("seguir")), sitemaps):
            for endereco, data in lista:
                novos[endereco] = {"titulo": "", "data": data, "origem": "sitemap"}
        def juntar(lista):
            for endereco, titulo in lista:
                atual = novos.setdefault(endereco, {"titulo": "", "data": "", "origem": "links"})
                if titulo and not atual["titulo"]:
                    atual["titulo"] = titulo

        paginas = _paginas_de_links()
        filhos = []
        for pagina, lista in zip(paginas, ex.map(lambda p: links_com_titulo(p["url"], p.get("seguir")), paginas)):
            juntar(lista)
            # 2º nível: páginas "índice" logo abaixo da inicial (ex.: perguntas-frequentes/cadastros -> .../cpf)
            for _ in range(int(pagina.get("profundidade", 2)) - 1):
                inicio = pagina["url"].rstrip("/") + "/"
                filhos.extend((u, pagina.get("seguir")) for u, _t in lista
                              if u.startswith(inicio) and u.rstrip("/").count("/") == inicio.count("/"))
        filhos = list(dict.fromkeys(filhos))[:MAX_PAGINAS_INDICE]
        for lista in ex.map(lambda f: links_com_titulo(*f), filhos):
            juntar(lista)
    with _trava:
        for endereco, info in novos.items():
            antigo = _catalogo["itens"].get(endereco, {})
            _catalogo["itens"][endereco] = dict(info, titulo=info.get("titulo") or antigo.get("titulo", ""))
        if len(_catalogo["itens"]) > MAX_CATALOGO:   # fica com os mais recentes
            ordem = sorted(_catalogo["itens"].items(), key=lambda kv: kv[1].get("data", ""), reverse=True)
            _catalogo["itens"] = dict(ordem[:MAX_CATALOGO])
        if novos:
            _catalogo["atualizado_em"] = datetime.now(V.FUSO_BR).isoformat(timespec="seconds")
        _reindexar_catalogo()
    _log(f"catálogo: {len(novos)} endereço(s) lidos dos sitemaps; {len(_catalogo['itens'])} no total")
    return len(novos)


def salvar_catalogo(pasta):
    try:
        pasta.mkdir(exist_ok=True)
        dados = {"atualizado_em": _catalogo["atualizado_em"], "itens": _catalogo["itens"]}
        tmp = pasta / "catalogo.tmp"
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, pasta / "catalogo.json")
    except OSError as erro:
        _log(f"não consegui salvar o catálogo: {erro}")


def carregar_catalogo(pasta):
    try:
        dados = json.loads((pasta / "catalogo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    with _trava:
        _catalogo["itens"] = {u: i for u, i in (dados.get("itens") or {}).items()
                              if V.url_permitida(u) and isinstance(i, dict)}
        _catalogo["atualizado_em"] = dados.get("atualizado_em")
        _reindexar_catalogo()
    return True


def status_catalogo():
    return {"enderecos": len(_catalogo["itens"]), "atualizado_em": _catalogo["atualizado_em"]}


_ENTIDADES = {"mei", "microem", "cpf", "cnpj", "das", "dasmei", "dasn", "simei", "irpf", "dirpf", "inss", "nfs", "nfse",
              "ccmei", "darf", "pgmei", "ecac", "caepf", "dirf", "esocial", "reinf"}


def titulos_fortes(termos_pergunta, enderecos=None, k=2, excluir=()):
    """Endereços cujo TÍTULO/endereço traz o assunto da pergunta (ex.: '.../como-funciona-o-mei-caminhoneiro').

    Regras (valem para qualquer pergunta):
    - se a pergunta cita uma ENTIDADE (MEI, CPF, CNPJ, DAS, IRPF...), o título precisa citá-la também
      ("diferença entre MEI e ME" ≠ "diferença entre agricultor e produtor rural");
    - e o título precisa ter a palavra mais específica da pergunta cobrindo 40% dela, OU nomear a
      entidade da pergunta (que não seja só "MEI") cobrindo 15% (ex.: ".../orientacao-tributaria/cpf"
      para "como regularizar CPF suspenso?").
    Sem contar o sujeito genérico ("mei"): se a pergunta só tem ele, não há título "forte".
    `enderecos`: lista a avaliar (padrão: o catálogo inteiro).
    """
    sujeito = _ENTIDADES & set(termos_pergunta)               # MEI, CPF, CNPJ...
    especificas = sujeito - {"mei", "microem"}
    pesos = {t: p for t, p in termos_pergunta.items() if t not in ("mei", "microem")}
    total = sum(pesos.values())
    if not total:
        return []
    excluir = set(excluir)
    fontes = enderecos if enderecos is not None else list(_catalogo["tokens"])
    mais_especifico = max(pesos, key=pesos.get)     # ex.: "licitação" em "posso vender ao governo em licitação?"
    pontos = []
    for endereco in fontes:
        if endereco in excluir or V.pagina_de_listagem(endereco):
            continue
        ts = _catalogo["tokens"].get(endereco) or set(_tokens(_titulo_do_endereco(endereco)))
        if sujeito and not sujeito & ts:      # "diferença entre MEI e ME" ≠ "diferença entre agricultor e..."
            continue
        cobre = sum(p for t, p in pesos.items() if t in ts) / total
        if (mais_especifico in ts and cobre >= 0.4) or (especificas & ts and cobre >= 0.15):
            pontos.append((cobre, -len(ts), endereco))   # empate: título mais curto (mais focado)
    pontos.sort(reverse=True)
    return [u for _, _, u in pontos[:k]]


def buscar_catalogo(consulta, k=6, excluir=()):
    """Endereços do catálogo cujo título/endereço mais combina com a consulta (palavras raras pesam mais)."""
    termos = set(_tokens(consulta))
    idf = _catalogo["idf"]
    if not termos or not idf:
        return []
    excluir = set(excluir)
    pontos = []
    for endereco, ts in _catalogo["tokens"].items():
        if endereco in excluir:
            continue
        comuns = termos & ts
        if len(comuns) < 2:          # 1 palavra só (quase sempre "mei") não indica o assunto
            continue
        pontos.append((sum(idf.get(t, 0) for t in comuns), endereco))
    pontos.sort(reverse=True)
    return [u for _, u in pontos[:k]]


# ======================================================== busca interna dos sites (APIs)
def _consulta_curta(pergunta, termos=()):
    """Poucas palavras para buscas internas (elas exigem TODAS as palavras: frase longa = 0 resultados)."""
    palavras = [p for p in re.findall(r"[\wÀ-ÿ-]+", pergunta or "") if len(p) > 2][:6]
    return " ".join(palavras) or " ".join(list(termos)[:3])


def _resultados_wp(corpo):
    try:
        dados = json.loads(corpo.decode("utf-8", "replace"))
    except ValueError:
        return []
    return [str(i.get("url") or i.get("link") or "") for i in dados if isinstance(i, dict)] if isinstance(dados, list) else []


def buscar_nos_sites(pergunta, termos=(), k=5):
    """Endereços devolvidos pela busca interna dos sites oficiais (só os que passam em url_permitida)."""
    _, buscas = _config()
    achados = []
    tentativas = [_consulta_curta(pergunta, termos)]
    if termos:
        tentativas.append(" ".join(list(termos)[:3]))
    for busca in buscas:
        for q in dict.fromkeys(t for t in tentativas if t):
            endereco = busca["url"].replace("{q}", urllib.parse.quote_plus(q))
            codigo, corpo, _ = V.baixar(endereco, aceitar=("json", "html"), max_bytes=2_000_000)
            if codigo != 200 or not corpo:
                continue
            if busca.get("tipo") == "wp-json":
                urls = _resultados_wp(corpo)
            else:
                html = V._decodificar(corpo) or ""
                urls = [urllib.parse.urljoin(endereco, h) for h in re.findall(r'href=["\']([^"\'#]+)', html)]
            novos = [u for u in urls if V.url_permitida(u) and not V.pagina_de_listagem(u) and u not in achados]
            achados.extend(novos)
            if novos:
                break                     # a 1ª formulação já achou: não precisa da 2ª
    return achados[:k]


# ============================================================= termos e relevância (geral)
# Palavras que aparecem em quase toda pergunta/página do tema e não dizem qual é o ASSUNTO.
_GENERICOS = {"mei", "microem", "individ", "empreen", "empresa", "pode", "voce", "sobre", "tambem", "receita",
              "federal", "sebrae", "brasil", "ano", "ser", "ter", "fazer", "cada", "outro", "outra", "pela", "pelo",
              "funcion", "precisa", "existe", "devo", "deve", "consigo", "sendo", "saber", "duvida", "ajuda",
              "porque", "entao", "todo", "toda", "agora", "hoje",
              # números por extenso ("dois CNPJ") casam com qualquer "dois tipos de..." e não dizem o assunto
              "doi", "dua", "tre", "quatro", "cinco", "primeir", "segund", "vario", "muita", "muito"}
# Palavras gramaticais que o filtro básico (verificador._STOP) deixa passar ("entre", "durante"...).
_PALAVRAS_VAZIAS = """entre ate apos desde durante depois antes ainda mesmo nem pois sobre atraves contra cada algum
alguma alguns algumas qualquer quaisquer tudo nada apenas somente bem mal assim aonde quanto quanta quantos quantas
coisa coisas forma caso casos tipo tipos vez vezes sempre nunca tambem demais pouco pouca outros outras dele dela
deles delas nesse nessa neste nesta disso desse dessa deste desta aquele aquela aquilo seja sao estao foram fosse
vai vou vamos queria gostaria poderia preciso sei qual quais tal tais via sai sair saem saiu acontece fica ficar
consegue conseguir faz faco feito tenho temos tem quer quero
dar dou da dando tirar tiro pegar pego ver vejo veja usar uso botar colocar levar trazer achar acho gente
usando fazendo tendo sendo estando indo vindo podendo pedindo pedir""".split()


def _raiz(palavra):
    """Mesma redução usada nos índices (sem acento, sem 's' final, até 7 letras)."""
    t = V._sem_acento(palavra)
    return (t[:-1] if len(t) > 3 and t.endswith("s") else t)[:7]


_GENERICOS |= {_raiz(p) for p in _PALAVRAS_VAZIAS}
_VERBOS_PERGUNTA = _GENERICOS - {"mei", "microem", "individ", "empreen", "empresa"}   # o sujeito (MEI) fica


def _idf(termo):
    """Quão específico é um termo (palavras raras nos sites oficiais valem mais)."""
    indice = _base_web()._estado["indice"]
    if termo in indice.idf:
        return indice.idf[termo]
    if termo in _catalogo["idf"]:
        return _catalogo["idf"][termo]
    return 2.5                    # termo que nenhum documento tem: específico, mas sem prova


def _limpos(texto, excluir):
    return [t for t in _tokens(texto) if t not in excluir and not t.isdigit() and len(t) > 1]


def termos_da_pergunta(pergunta):
    """{termo: peso} das palavras de conteúdo da PERGUNTA (inclui o sujeito, ex.: "mei")."""
    return {t: max(_idf(t), 0.7) for t in _limpos(pergunta, _VERBOS_PERGUNTA)}


def termos_chave(pergunta, rascunho="", n=14):
    """{termo: peso} com as palavras que caracterizam o ASSUNTO (pergunta + afirmações da resposta).

    As palavras da pergunta entram sempre (peso dobrado); da resposta, as mais específicas.
    Números soltos e palavras genéricas do tema ("MEI", "empresa") não entram.
    """
    pesos = {t: 2 * _idf(t) for t in _limpos(pergunta, _GENERICOS)}
    candidatos = Counter(_limpos(re.sub(r"\[\w+\]", " ", rascunho or ""), _GENERICOS))
    ordenados = sorted(candidatos, key=lambda t: -(_idf(t) * (1 + math.log(candidatos[t]))))
    for t in ordenados:
        if len(pesos) >= n:
            break
        pesos.setdefault(t, _idf(t))
    return pesos


def cobertura(texto, pesos):
    """Fração (0–1), ponderada pela especificidade, dos termos do assunto que aparecem no texto."""
    if not pesos:
        return 0.0
    presentes = set(_tokens(texto))
    total = sum(pesos.values())
    return sum(p for t, p in pesos.items() if t in presentes) / total if total else 0.0


def avaliar(trecho, termos_pergunta, termos_assunto):
    """(relevância 0–1, é_sobre_o_assunto) de um trecho oficial — vale para qualquer pergunta.

    - a página precisa trazer as palavras da PERGUNTA (ponderadas: "caminhoneiro" pesa muito mais que
      "MEI"); sem isso, compartilhar palavras comuns da resposta ("atividade", "sócio") não basta;
    - quanto mais termos específicos da RESPOSTA ela traz, melhor;
    - título/endereço com o assunto é um bom sinal (ex.: .../atividades-permitidas-mei/).
    """
    titulo = f"{trecho.get('titulo', '')} {_titulo_do_endereco(trecho.get('url', ''))}"
    texto = f"{titulo} {trecho.get('texto', '')}"
    cp = cobertura(texto, termos_pergunta) if termos_pergunta else 1.0
    cr = cobertura(texto, termos_assunto)
    principais = set(termos_pergunta) | {t for t, _ in sorted(termos_assunto.items(), key=lambda kv: -kv[1])[:3]}
    tokens_titulo = set(_tokens(titulo))
    no_titulo = tokens_titulo & (principais | {"mei", "microem"})
    bonus = 0.15 if no_titulo - {"mei", "microem"} else (0.0 if no_titulo else -0.1)
    relevancia = max(0.0, min(1.0, 0.45 * cp + 0.55 * cr + bonus))
    sobre = cp >= 0.5 and relevancia >= LIMIAR_RELEVANCIA
    # Sinônimo: a pessoa diz "empréstimo" e a página oficial diz "crédito". Se a página cobre bem os
    # termos da RESPOSTA e tem um deles no título, ela trata do assunto mesmo sem a palavra da pergunta.
    so_resposta = {t: w for t, w in termos_assunto.items() if t not in termos_pergunta}
    cr2 = cobertura(texto, so_resposta)
    if cr2 >= 0.35 and tokens_titulo & set(so_resposta):
        relevancia = max(relevancia, min(1.0, 0.85 * cr2 + 0.15))
        sobre = sobre or relevancia >= LIMIAR_RELEVANCIA
    return round(relevancia, 3), sobre


# ======================================================= reforço da conferência (pós-rascunho)
def reforcar(trechos_web, pergunta, consulta, rascunho, tempo=TEMPO_REFORCO):
    """Melhora os trechos oficiais da conferência usando os termos da resposta.

    1. procura na base local pelos termos da resposta;
    2. se ainda faltam páginas sobre o assunto, descobre endereços (catálogo dos sitemaps, busca
       interna dos sites, fontes_oficiais.json) e lê as páginas na hora;
    3. ordena por relevância e marca "destaque" (sobre o assunto) em cada trecho.
    """
    if not ATIVA:
        return trechos_web or []
    pesos = termos_chave(pergunta, rascunho)
    termos_p = termos_da_pergunta(pergunta)
    if not pesos and not termos_p:
        return trechos_web or []
    consulta_rica = " ".join(dict.fromkeys([pergunta or "", " ".join(pesos), consulta or ""]))
    candidatos = {t["url"]: t for t in (trechos_web or []) if t.get("url")}
    try:
        for t in _base_web().buscar(consulta_rica, k=6, min_termos=2):
            candidatos.setdefault(t["url"], dict(t, origem="base_local"))
    except Exception as erro:  # noqa: BLE001
        _log(f"base local indisponível: {type(erro).__name__}")

    def pontuar():
        for t in candidatos.values():
            t["relevancia"], t["destaque"] = avaliar(t, termos_p, pesos)
        return [t for t in candidatos.values() if t["destaque"]]

    relevantes = pontuar()
    # página cujo título É o assunto da pergunta (catálogo) — vale ler mesmo com outras razoáveis
    fortes = titulos_fortes(termos_p, excluir=set(candidatos))
    if not fortes:
        # sinônimos: a pessoa diz "empréstimo", o site diz "crédito"/"Pronampe" — os termos mais
        # específicos da RESPOSTA também servem para achar a página pelo título
        da_resposta = [(t, w) for t, w in sorted(pesos.items(), key=lambda kv: -kv[1]) if t not in termos_p]
        sujeito = {t: termos_p[t] for t in termos_p if t in _ENTIDADES}
        for termo, peso in da_resposta[:6]:        # um termo por vez: "Pronampe"? "crédito"?...
            fortes = titulos_fortes(dict(sujeito, **{termo: peso}), excluir=set(candidatos), k=1)
            if fortes:
                break
    if fortes:
        _log(f"páginas com o assunto no título: {fortes}")
        for t in _ler_varias(fortes, consulta_rica, tempo):
            candidatos[t["url"]] = t
        relevantes = pontuar()
    if len(relevantes) < V.MIN_TRECHOS_LOCAIS:
        enderecos = descobrir(pergunta, consulta_rica, pesos, excluir=set(candidatos))
        _log(f"{len(relevantes)} página(s) sobre o assunto; lendo ao vivo: {enderecos}")
        for t in _ler_varias(enderecos, consulta_rica, tempo):
            candidatos[t["url"]] = t
        relevantes = pontuar()
    ordem, textos_vistos = [], set()
    for t in sorted(candidatos.values(), key=lambda t: -t.get("relevancia", 0)):
        assinatura = re.sub(r"\W+", "", t.get("texto", "")[:400].lower())
        if assinatura in textos_vistos:      # mesma página em dois endereços
            continue
        textos_vistos.add(assinatura)
        ordem.append(t)
    # se há páginas sobre o assunto, as genéricas saem (só confundiriam o verificador)
    escolhidos = [t for t in ordem if t["destaque"]]
    if escolhidos:   # entre as que tratam do assunto, descarta as bem mais fracas que a melhor
        escolhidos = [t for t in escolhidos if t["relevancia"] >= 0.8 * escolhidos[0]["relevancia"]]
    else:
        escolhidos = ordem
    _log(f"conferência com {len(escolhidos[:V.MAX_PAGINAS])} página(s): "
         + ", ".join(f"{t['relevancia']:.2f} {t['url']}" for t in escolhidos[:V.MAX_PAGINAS]))
    return escolhidos[:V.MAX_PAGINAS]


def descobrir(pergunta, consulta_rica, pesos=None, excluir=()):
    """Endereços oficiais candidatos para QUALQUER pergunta (sem buscador externo)."""
    if not ATIVA:
        return []
    excluir = set(excluir)
    termos_ordenados = [t for t, _ in sorted((pesos or {}).items(), key=lambda kv: -kv[1])]
    listas = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sites = ex.submit(buscar_nos_sites, pergunta, termos_ordenados)
        listas.append(buscar_catalogo(consulta_rica, k=6, excluir=excluir))
        listas.append(V.urls_das_sementes(f"{pergunta} {consulta_rica}"))
        try:
            listas.append(f_sites.result(timeout=V.TIMEOUT_PAGINA + 2))
        except Exception:  # noqa: BLE001
            pass
    # intercala as origens (catálogo, sementes, busca interna) para não depender de uma só
    saida = []
    for grupo in zip(*[lst + [None] * (6 - len(lst)) for lst in listas]):
        for u in grupo:
            if u and u not in excluir and u not in saida and not V.pagina_de_listagem(u):
                saida.append(u)
    return saida[:MAX_LEITURAS]


def _ler_varias(enderecos, consulta, tempo):
    """Lê páginas ao vivo. Cada página lida entra na base local (aprender) e o trecho é escolhido pela
    mesma busca da base local (BM25, palavras raras valem mais) — e não só contando palavras."""
    lidos = _ler_ao_vivo(enderecos, consulta, tempo)
    if not lidos:
        return lidos
    try:
        melhores = {t["url"]: t for t in _base_web().buscar(consulta, k=len(lidos), min_termos=1,
                                                             so_urls={t["url"] for t in lidos})}
    except Exception:  # noqa: BLE001
        melhores = {}
    return [dict(t, texto=melhores[t["url"]]["texto"]) if t["url"] in melhores else t for t in lidos]


def _ler_ao_vivo(enderecos, consulta, tempo):
    achados = []
    if not enderecos:
        return achados
    limite = time.time() + tempo
    with ThreadPoolExecutor(max_workers=MAX_LEITURAS) as ex:
        futuros = [ex.submit(V.obter_trecho, u, consulta) for u in enderecos]
        for f in futuros:
            try:
                r = f.result(timeout=max(0.1, limite - time.time()))
            except Exception:  # noqa: BLE001
                continue
            if r:
                achados.append(r)
    return achados
