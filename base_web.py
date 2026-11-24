"""Base web do FiscOn: cópia local, sempre atualizada, das páginas e PDFs oficiais.

O que faz
  - De tempos em tempos (BASE_WEB_HORAS, padrão 12 h) o servidor visita os sites do Sebrae e da
    Receita Federal (SOMENTE eles: verificador.url_permitida), lê as páginas listadas em
    fontes_oficiais.json, segue os links das páginas de "rastrear" (notícias, perguntas frequentes,
    guia do MEI...) e guarda o texto sobre MEI e Receita Federal num índice de busca local.
  - Também confere se os PDFs da base (fontes_pdfs.json) ganharam versão nova no site. Se sim, lê o
    PDF novo e o coloca nesta base na hora, sem esperar o próximo deploy.
  - Páginas encontradas em buscas ao vivo (verificador.py) também entram aqui ("aprender").

Por que deixa o chatbot mais rápido
  Na hora da pergunta, a conferência com os sites usa esta cópia local (milissegundos) em vez de
  baixar páginas pela internet (vários segundos). A busca ao vivo só acontece quando a cópia local
  não tem nada sobre o assunto.

Configuração (.env / Render), todas opcionais:
  BASE_WEB_ATIVA=1          liga/desliga tudo
  BASE_WEB_HORAS=12         intervalo entre coletas
  BASE_WEB_MAX_PAGINAS=80   páginas por coleta
  BASE_WEB_PDFS=1           confere se há PDF mais novo no site
  BASE_WEB_ATRASO_S=20      espera depois de o servidor ficar pronto, antes da 1ª coleta

Só biblioteca padrão (+ pypdf, que já está no requirements.txt).
"""
import json
import math
import os
import re
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import verificador as V

ATIVA = os.getenv("BASE_WEB_ATIVA", "1") != "0"
INTERVALO_HORAS = float(os.getenv("BASE_WEB_HORAS", "12") or 12)
MAX_PAGINAS = int(os.getenv("BASE_WEB_MAX_PAGINAS", "80") or 80)
VERIFICAR_PDFS = os.getenv("BASE_WEB_PDFS", "1") != "0"
ATRASO_INICIAL = int(os.getenv("BASE_WEB_ATRASO_S", "20") or 20)

RAIZ = Path(__file__).resolve().parent
PASTA = RAIZ / "indice_web"
ARQ_BASE = PASTA / "base_web.json"
ARQ_FONTES_PDF = RAIZ / "fontes_pdfs.json"
ARQ_ESTADO_PDF = RAIZ / "base_status.json"

TAM_PASSAGEM = 750           # caracteres por passagem indexada
MAX_PASSAGENS_PAGINA = 40    # páginas enormes não dominam a base
MAX_PASSAGENS_PDF = 900      # PDF novo (até ~650 mil caracteres)
TEMPO_PDF = 90               # segundos lendo um PDF novo (em segundo plano)
MAX_APRENDIDAS = 60          # páginas guardadas vindas de buscas ao vivo
VALIDADE_DIAS = 30           # página que não é revista há mais tempo que isso sai da base

# Só entra na base texto que fale de MEI / Receita Federal (o pedido é "somente esses assuntos").
TEMAS = re.compile(
    r"\b(mei|simei|microempreendedor|simples nacional|dasn|cnpj|cpf|imposto|tribut|receita federal|"
    r"irpf|declara|restitui|malha fina|faturamento|nota fiscal|inss|previd|e-cac|ecac|empreend)", re.I)


def _log(msg):
    print(f"[BASE_WEB] {msg}", flush=True)


def _agora_iso():
    return datetime.now(V.FUSO_BR).isoformat(timespec="seconds")


# --------------------------------------------------------------------- texto -> passagens
_DATA = re.compile(r"(?:publicad[oa]|atualizad[oa]|última modificação|ultima modificacao|modificado)\s*(?:em|:)?\s*"
                   r"(\d{1,2})/(\d{1,2})/(20\d\d)", re.I)


def data_da_pagina(texto):
    """Data mais recente de 'publicado/atualizado em dd/mm/aaaa' que aparece na página (ou '')."""
    datas = []
    for d, m, a in _DATA.findall(texto or ""):
        try:
            datas.append(datetime(int(a), int(m), int(d)))
        except ValueError:
            continue
    return max(datas).strftime("%d/%m/%Y") if datas else ""


def em_passagens(linhas, tamanho=TAM_PASSAGEM, limite=MAX_PASSAGENS_PAGINA):
    """Junta linhas seguidas em passagens de ~`tamanho` caracteres, só com as que tratam dos TEMAS."""
    passagens, atual = [], ""
    for linha in linhas:
        if len(atual) + len(linha) + 1 > tamanho and atual:
            passagens.append(atual)
            atual = ""
        atual = f"{atual}\n{linha}" if atual else linha
    if atual:
        passagens.append(atual)
    return [p for p in passagens if TEMAS.search(p)][:limite]


# ------------------------------------------------------------------------------ busca
def _tokens(texto):
    return [(t[:-1] if len(t) > 3 and t.endswith("s") else t)[:7] for t in V._palavras(texto)]


class _Indice:
    """BM25 simples sobre as passagens (leve: não usa embeddings nem memória extra)."""

    def __init__(self, passagens):
        self.passagens = passagens            # [{"url","titulo","site","data","tipo","texto"}]
        docs = [_tokens(p["titulo"] + " " + p["texto"]) for p in passagens]
        self.n = len(docs)
        self.tam = [len(d) for d in docs]
        self.media = (sum(self.tam) / self.n) if self.n else 1.0
        self.postings = defaultdict(list)
        for i, d in enumerate(docs):
            for termo, tf in Counter(d).items():
                self.postings[termo].append((i, tf))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def pontuar(self, consulta, k1=1.4, b=0.75):
        termos = set(_tokens(consulta))
        pontos, acertos = defaultdict(float), defaultdict(int)
        for termo in termos:
            for i, tf in self.postings.get(termo, ()):
                norm = tf + k1 * (1 - b + b * self.tam[i] / self.media)
                pontos[i] += self.idf[termo] * tf * (k1 + 1) / norm
                acertos[i] += 1
        return termos, pontos, acertos


_trava = threading.Lock()
_estado = {
    "paginas": {},        # url -> {"titulo","site","data","tipo","origem","coletado_em","passagens":[...]}
    "indice": _Indice([]),
    "atualizada_em": None, "rodando": False, "ultimo_erro": None, "proxima": None,
    "pdfs_novos": [], "pid": None, "ultima_coleta": {},
}


def _reindexar():
    passagens = []
    for url, pg in _estado["paginas"].items():
        for texto in pg.get("passagens", []):
            passagens.append({"url": url, "titulo": pg.get("titulo") or url, "site": pg.get("site") or V.nome_do_site(url),
                              "data": pg.get("data", ""), "tipo": pg.get("tipo", "pagina"), "texto": texto})
    _estado["indice"] = _Indice(passagens)


def buscar(consulta, k=4, min_termos=2, limite_chars=V.MAX_CHARS_TRECHO, destaques=(), preferidas=(), so_urls=None):
    """Melhores páginas da base local para a consulta.

    Devolve [{"url","titulo","site","data","tipo","texto","destaque"}] (uma entrada por página, com as
    passagens mais relevantes dela). Só entra página que tenha pelo menos `min_termos` palavras da consulta.
    destaques: expressões do assunto (verificador.expandir_consulta) — a passagem que traz uma delas
    vale bem mais; "destaque"=True diz que a página trata do assunto e não só repete palavras.
    preferidas: páginas oficiais indicadas para o assunto em fontes_oficiais.json (ganham bônus).
    so_urls: limita a busca a essas páginas (ex.: as que acabaram de ser lidas ao vivo).
    Páginas de listagem (busca do site, feed, tag...) nunca entram.
    """
    indice = _estado["indice"]
    if not consulta or not indice.n:
        return []
    termos, pontos, acertos = indice.pontuar(consulta)
    exigido = min(min_termos, len(termos)) or 1
    ano = str(datetime.now(V.FUSO_BR).year)
    preferidas = set(preferidas or ())
    melhores = []
    for i, p in pontos.items():
        pas = indice.passagens[i]
        if acertos[i] < exigido or V.pagina_de_listagem(pas["url"]) or (so_urls is not None and pas["url"] not in so_urls):
            continue
        bonus = 1.15 if ano in (pas["data"] or "") else 1.0   # conteúdo do ano corrente
        marcas = V.contar_destaques(pas["titulo"] + " " + pas["texto"], destaques)
        bonus *= 1 + 0.8 * min(marcas, 3)
        if pas["url"] in preferidas:
            bonus *= 1.3
        melhores.append((p * bonus, i, marcas > 0))
    melhores.sort(reverse=True)
    por_url = {}
    for pts, i, marca in melhores:
        pas = indice.passagens[i]
        grupo = por_url.setdefault(pas["url"], {"pontos": pts, "itens": [], "destaque": False})
        if len(grupo["itens"]) < 3:
            grupo["itens"].append(pas)
            grupo["destaque"] = grupo["destaque"] or marca
    saida = []
    for url, g in sorted(por_url.items(), key=lambda kv: -kv[1]["pontos"])[:k]:
        base = g["itens"][0]
        texto, total = [], 0
        for item in g["itens"]:
            if total + len(item["texto"]) > limite_chars and texto:
                break
            texto.append(item["texto"])
            total += len(item["texto"])
        saida.append({"url": url, "titulo": base["titulo"], "site": base["site"], "data": base["data"],
                      "tipo": base["tipo"], "texto": "\n".join(texto)[:limite_chars], "destaque": g["destaque"]})
    return saida


# ------------------------------------------------------------------------------ coleta
def _links(html, base_url, padrao):
    """Endereços (permitidos) da página que combinam com a expressão `padrao`."""
    achados = []
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html or "", re.I):
        url = urllib.parse.urljoin(base_url, href.replace("&amp;", "&").strip())
        url = re.sub(r"/(view|@@display-file/file)$", "", url)
        if (V.url_permitida(url) and re.search(padrao, url, re.I) and url not in achados and url != base_url
                and not V.pagina_de_listagem(url)):      # busca do site, feed, tag... não respondem nada
            achados.append(url)
    return achados


def ler_url(url):
    """Baixa e converte uma página HTML ou PDF permitida -> (titulo, linhas, html_ou_None, data) ou None."""
    codigo, corpo, cab = V.baixar(url, V._ACEITAR_HTML + V._ACEITAR_PDF, V.MAX_BYTES_PDF)
    if codigo != 200 or not corpo:
        return None
    try:
        if corpo[:5] == b"%PDF-":
            titulo, linhas = V.pdf_para_linhas(corpo)
            return titulo, linhas, None, data_da_pagina(" ".join(linhas[:40]))
        html = V._decodificar(corpo, (cab or {}).get("Content-Type", ""))
        if not html:
            return None
        titulo, linhas = V.completar_portal_sebrae(url, V.html_para_linhas(html))   # portal do Sebrae: JSON do AEM
        return titulo, linhas, html, data_da_pagina(" ".join(linhas))
    except Exception as erro:  # noqa: BLE001
        _log(f"não consegui ler {url}: {type(erro).__name__}")
        return None


def _carregar_fontes():
    try:
        dados = json.loads(V.ARQ_SEMENTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    simples = []
    for item in dados.get("paginas", []):
        if isinstance(item, dict):
            simples.extend(u for u in item.get("urls", []) if V.url_permitida(u) and u not in simples)
    regras = [r for r in dados.get("rastrear", []) if isinstance(r, dict) and V.url_permitida(r.get("inicio", ""))]
    return simples, regras


def _guardar_pagina(url, titulo, linhas, data, tipo="pagina", origem="coleta", limite=MAX_PASSAGENS_PAGINA):
    passagens = em_passagens(linhas, limite=limite)
    if not passagens:
        return False
    _estado["paginas"][url] = {"titulo": (titulo or url.rsplit("/", 1)[-1])[:160], "site": V.nome_do_site(url),
                               "data": data, "tipo": tipo, "origem": origem, "coletado_em": _agora_iso(),
                               "passagens": passagens}
    return True


def _verificar_pdfs_novos():
    """PDFs da base com versão mais nova no site: lê e coloca na base web (sem gravar o arquivo)."""
    if not VERIFICAR_PDFS:
        return []
    import atualizar_base as A   # import tardio: só precisa dele aqui

    try:
        manifesto = json.loads(ARQ_FONTES_PDF.read_text(encoding="utf-8"))
        estado = json.loads(ARQ_ESTADO_PDF.read_text(encoding="utf-8")) if ARQ_ESTADO_PDF.is_file() else {}
    except (OSError, ValueError):
        return []
    ano_minimo = int(manifesto.get("ano_minimo", 0) or 0)
    novos = []
    for fonte in manifesto.get("fontes", []):
        if not isinstance(fonte, dict) or fonte.get("ativo", True) is False:
            continue
        try:
            r = A.processar(fonte, json.loads(json.dumps(estado)), simular=True, ano_minimo=ano_minimo,
                            guardar_corpo=True)
        except Exception as erro:  # noqa: BLE001
            _log(f"PDF {fonte.get('arquivo')}: erro {type(erro).__name__}")
            continue
        if r["status"] not in ("novo", "atualizado") or not r.get("corpo"):
            continue
        try:
            titulo_pdf, linhas = V.pdf_para_linhas(r["corpo"], tempo=TEMPO_PDF)
        except Exception as erro:  # noqa: BLE001
            _log(f"PDF novo {fonte.get('arquivo')} não pôde ser lido: {type(erro).__name__}")
            continue
        titulo = f"{fonte.get('titulo') or titulo_pdf} — versão mais nova publicada no site"
        if _guardar_pagina(r["url"], titulo, linhas, data_da_pagina(" ".join(linhas[:60])), tipo="pdf",
                           origem="pdf_novo", limite=MAX_PASSAGENS_PDF):
            novos.append({"arquivo": fonte.get("arquivo"), "url": r["url"], "status": r["status"]})
            _log(f"PDF {r['status']} no site: {fonte.get('arquivo')} -> entrou na base web ({r['detalhe']})")
    return novos


def atualizar_agora():
    """Uma coleta completa. Páginas que falharem desta vez continuam com a versão anterior."""
    if _estado["rodando"]:
        return status()
    _estado["rodando"] = True
    inicio = time.time()
    try:
        simples, regras = _carregar_fontes()
        candidatos = list(simples)
        try:   # catálogo de endereços (sitemaps): é por ele que qualquer pergunta acha a sua página
            import pesquisa
            pesquisa.atualizar_catalogo()
        except Exception as erro:  # noqa: BLE001
            _log(f"catálogo de endereços não atualizou: {type(erro).__name__}: {erro}")
        lidos = {}

        def visitar(url):
            return url, ler_url(url)

        # 1) páginas de partida dos rastreios: delas saem os links a seguir
        with ThreadPoolExecutor(max_workers=4) as ex:
            for url, doc in ex.map(visitar, [r["inicio"] for r in regras]):
                lidos[url] = doc
        for regra in regras:
            doc = lidos.get(regra["inicio"])
            if doc and doc[2]:
                novos = _links(doc[2], regra["inicio"], regra.get("seguir", "."))[: int(regra.get("max", 10))]
                candidatos.extend(u for u in novos if u not in candidatos)
            if regra["inicio"] not in candidatos:
                candidatos.insert(0, regra["inicio"])
        try:   # tudo o que o rastreio encontrou também entra no catálogo (mesmo além do limite de páginas)
            import pesquisa
            pesquisa.adicionar_ao_catalogo(candidatos)
        except Exception:  # noqa: BLE001
            pass
        candidatos = candidatos[:MAX_PAGINAS]

        # 2) lê o que falta (em paralelo, poucos de cada vez para não sobrecarregar os sites)
        faltam = [u for u in candidatos if u not in lidos]
        with ThreadPoolExecutor(max_workers=4) as ex:
            for url, doc in ex.map(visitar, faltam):
                lidos[url] = doc

        ok = falhas = 0
        with _trava:
            for url in candidatos:
                doc = lidos.get(url)
                if doc and _guardar_pagina(url, doc[0], doc[1], doc[3]):
                    ok += 1
                else:
                    falhas += 1
        pdfs = _verificar_pdfs_novos()
        with _trava:
            _limpar_antigas()
            _reindexar()
            resumo = {"lidas": ok, "falhas": falhas, "segundos": round(time.time() - inicio, 1), "em": _agora_iso()}
            if ok or pdfs:
                _estado.update(atualizada_em=_agora_iso(), ultimo_erro=None, pdfs_novos=pdfs, ultima_coleta=resumo)
            else:   # nada lido (sem internet / sites fora do ar): mantém a data da última coleta boa
                _estado.update(ultimo_erro="nenhuma página pôde ser lida (sem internet ou sites fora do ar)",
                               ultima_coleta=resumo)
        salvar()
        _log(f"coleta concluída: {ok} página(s) lidas, {falhas} sem texto útil/inacessíveis, "
             f"{len(pdfs)} PDF(s) novo(s), {_estado['indice'].n} passagens no total, {time.time() - inicio:.0f}s")
    except Exception as erro:  # noqa: BLE001
        _estado["ultimo_erro"] = f"{type(erro).__name__}: {erro}"
        _log(f"coleta falhou: {_estado['ultimo_erro']}")
    finally:
        _estado["rodando"] = False
    return status()


def _limpar_antigas():
    """Tira páginas não revistas há mais de VALIDADE_DIAS e limita as aprendidas em buscas ao vivo."""
    agora = datetime.now(V.FUSO_BR)
    for url, pg in list(_estado["paginas"].items()):
        try:
            idade = (agora - datetime.fromisoformat(pg.get("coletado_em"))).days
        except (TypeError, ValueError):
            idade = VALIDADE_DIAS + 1
        if idade > VALIDADE_DIAS:
            del _estado["paginas"][url]
    aprendidas = sorted((pg.get("coletado_em", ""), url) for url, pg in _estado["paginas"].items()
                        if pg.get("origem") == "busca")
    for _, url in (aprendidas[:-MAX_APRENDIDAS] if len(aprendidas) > MAX_APRENDIDAS else []):
        del _estado["paginas"][url]


def aprender(url, titulo, linhas, data=""):
    """Guarda uma página lida numa busca ao vivo (assim a próxima pergunta parecida já é instantânea)."""
    if not ATIVA or not V.url_permitida(url) or url in _estado["paginas"]:
        return
    with _trava:
        if _guardar_pagina(url, titulo, linhas, data or data_da_pagina(" ".join(linhas)), origem="busca"):
            _limpar_antigas()
            _reindexar()


# --------------------------------------------------------------------- disco e status
def salvar():
    try:
        PASTA.mkdir(exist_ok=True)
        dados = {"atualizada_em": _estado["atualizada_em"], "pdfs_novos": _estado["pdfs_novos"],
                 "paginas": _estado["paginas"]}
        tmp = ARQ_BASE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ARQ_BASE)
        import pesquisa
        pesquisa.salvar_catalogo(PASTA)
    except OSError as erro:
        _log(f"não consegui salvar a base web: {erro}")


def carregar():
    """Carrega a base salva em disco (gerada no build ou na última coleta)."""
    try:
        dados = json.loads(ARQ_BASE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    with _trava:
        _estado["paginas"] = {u: p for u, p in (dados.get("paginas") or {}).items()
                              if V.url_permitida(u) and isinstance(p, dict)}
        _estado["atualizada_em"] = dados.get("atualizada_em")
        _estado["pdfs_novos"] = dados.get("pdfs_novos") or []
        _reindexar()
    try:
        import pesquisa
        pesquisa.carregar_catalogo(PASTA)
    except Exception as erro:  # noqa: BLE001
        _log(f"catálogo de endereços não carregou: {type(erro).__name__}")
    _log(f"base carregada do disco: {len(_estado['paginas'])} páginas (coleta de {_estado['atualizada_em']})")
    return True


def _horas_desde_ultima():
    try:
        return (datetime.now(V.FUSO_BR) - datetime.fromisoformat(_estado["atualizada_em"])).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")


def status():
    return {"ativa": ATIVA, "paginas": len(_estado["paginas"]), "passagens": _estado["indice"].n,
            "atualizada_em": _estado["atualizada_em"], "rodando": _estado["rodando"],
            "proxima": _estado["proxima"], "ultimo_erro": _estado["ultimo_erro"],
            "pdfs_novos": _estado["pdfs_novos"], "ultima_coleta": _estado["ultima_coleta"],
            "intervalo_horas": INTERVALO_HORAS, "catalogo": _status_catalogo()}


def _status_catalogo():
    try:
        import pesquisa
        return pesquisa.status_catalogo()
    except Exception:  # noqa: BLE001
        return {}


def _ciclo():
    time.sleep(ATRASO_INICIAL)
    while True:
        falta = INTERVALO_HORAS - _horas_desde_ultima()
        if falta > 0 and not _estado["ultimo_erro"]:                       # base do disco ainda é recente (ex.: feita no build)
            _estado["proxima"] = datetime.fromtimestamp(time.time() + falta * 3600, V.FUSO_BR).isoformat(timespec="minutes")
            time.sleep(min(falta * 3600, 6 * 3600))
            continue
        atualizar_agora()
        # coleta que não leu nada tenta de novo em 1 h (em vez de esperar o intervalo inteiro)
        espera = 3600 if _estado["ultimo_erro"] else INTERVALO_HORAS * 3600
        _estado["proxima"] = datetime.fromtimestamp(time.time() + espera, V.FUSO_BR).isoformat(timespec="minutes")
        time.sleep(espera)


def iniciar():
    """Carrega a base do disco e inicia a coleta periódica (uma vez por processo)."""
    if not ATIVA or _estado["pid"] == os.getpid():
        return
    _estado["pid"] = os.getpid()
    carregar()
    threading.Thread(target=_ciclo, name="base-web", daemon=True).start()
    _log(f"coleta periódica ligada: a cada {INTERVALO_HORAS:g} h (máx. {MAX_PAGINAS} páginas)")


if __name__ == "__main__":
    # python base_web.py  -> faz uma coleta agora e mostra o resultado
    carregar()
    print(json.dumps(atualizar_agora(), ensure_ascii=False, indent=2))
