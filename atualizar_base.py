"""Atualiza os PDFs da base com as versões mais recentes do Sebrae e da Receita Federal.

Uso:
    python atualizar_base.py              # baixa o que mudou e substitui os PDFs
    python atualizar_base.py --simular    # só mostra o que mudaria (não grava nada)
    python atualizar_base.py --reindexar  # depois de atualizar, refaz o índice de busca (uso local)
    python atualizar_base.py --auditar    # mostra o ano de cada PDF da base e aponta os anteriores a 2026
    python atualizar_base.py --arquivar-antigos   # move os PDFs anteriores a 2026 para pdfs_arquivados/

Como funciona:
  - fontes_pdfs.json diz, para cada PDF da base, de onde baixar a versão atual.
  - Só endereços do Sebrae e da Receita Federal são aceitos (verificador.url_permitida), inclusive
    nos redirecionamentos.
  - Só entram PDFs de 2026 em diante ("ano_minimo" em fontes_pdfs.json): o ano vem dos metadados do
    PDF e das primeiras páginas (capa/edição). Edições antigas são recusadas ("antigo"), e passam a
    ser aceitas sozinhas quando a fonte publicar a edição nova.
  - Cada arquivo baixado é validado (é PDF? abre? tem texto? não encolheu demais?) antes de
    substituir o anterior; se algo estranho acontecer, o PDF antigo é mantido.
  - base_status.json guarda data, hash e ETag de cada PDF (evita baixar de novo o que não mudou).
  - O Render refaz o índice sozinho no deploy (preparar_base.py); por isso o fluxo recomendado é
    rodar este script no GitHub Actions (.github/workflows/atualizar-base.yml), que abre um Pull
    Request com os PDFs novos. Ao aprovar, o Render publica a base atualizada.

Só biblioteca padrão + pypdf (já está no requirements.txt).
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import verificador as V

RAIZ = Path(__file__).resolve().parent
ARQ_FONTES = RAIZ / "fontes_pdfs.json"
ARQ_ESTADO = RAIZ / "base_status.json"
ARQ_RELATORIO = Path(os.getenv("RELATORIO_ATUALIZACAO", RAIZ / "relatorio_atualizacao.md"))

MAX_BYTES = 60_000_000        # maior PDF aceito
MIN_BYTES = 10_000            # menor que isso não é uma publicação de verdade
TIMEOUT_DOWNLOAD = 90         # segundos
FRACAO_MINIMA_PAGINAS = 0.5   # PDF novo com menos da metade das páginas do atual é suspeito
ANO_MINIMO_PADRAO = 2026
PASTA_ARQUIVADOS = RAIZ / "pdfs_arquivados"      # PDFs antigos guardados fora do índice
NOME_VALIDO = re.compile(r"^\d+-[\w&.-]+\.pdf$", re.IGNORECASE)   # mesmo padrão do rag.py/app.py


def agora():
    return datetime.now(V.FUSO_BR)


def sha256(corpo):
    return hashlib.sha256(corpo).hexdigest()


# ------------------------------------------------------------------------- validação
def validar_pdf(corpo):
    """Devolve (ok, paginas, motivo)."""
    if len(corpo) < MIN_BYTES:
        return False, 0, f"arquivo muito pequeno ({len(corpo)} bytes)"
    if corpo[:5] != b"%PDF-":
        return False, 0, "não é um PDF (cabeçalho inválido; pode ser uma página de erro)"
    try:
        from pypdf import PdfReader

        leitor = PdfReader(io.BytesIO(corpo))
        paginas = len(leitor.pages)
        if paginas == 0:
            return False, 0, "PDF sem páginas"
        texto = ""
        for i in range(min(paginas, 8)):
            texto += leitor.pages[i].extract_text() or ""
        if len(texto.strip()) < 200:
            return False, paginas, "PDF sem texto extraível (escaneado?): o chatbot não conseguiria ler"
    except Exception as erro:  # noqa: BLE001
        return False, 0, f"PDF corrompido ({type(erro).__name__})"
    return True, paginas, ""


def ano_do_pdf(corpo):
    """Ano de referência do PDF: o maior entre os metadados (criação/modificação) e a capa
    (primeiras 3 páginas, onde costa a edição). Devolve (ano|None, de_onde)."""
    from pypdf import PdfReader

    leitor = PdfReader(io.BytesIO(corpo))
    anos = []
    try:
        meta = leitor.metadata or {}
        for chave in ("/ModDate", "/CreationDate"):
            m = re.search(r"(?:D:)?(20[0-3]\d)", str(meta.get(chave) or ""))
            if m:
                anos.append((int(m.group(1)), "metadados"))
    except Exception:  # noqa: BLE001
        pass
    capa = ""
    for i in range(min(len(leitor.pages), 3)):
        try:
            capa += " " + (leitor.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    for a in re.findall(r"\b(20[0-3]\d)\b", capa):
        anos.append((int(a), "capa"))
    return max(anos) if anos else (None, "sem ano identificável")


def paginas_do_arquivo(caminho):
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(caminho)).pages)
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------- descoberta do link novo
def _chave_natural(url):
    """Ordena links 'mais novos' primeiro: compara todos os números do endereço (ano, versão, data)."""
    return [int(n) for n in re.findall(r"\d+", urllib.parse.unquote(url))]


def descobrir_link(pagina_indice, padrao):
    """Procura na página (permitida) o PDF mais novo cujo endereço combine com `padrao`."""
    codigo, corpo, cab = V.baixar(pagina_indice, timeout=30)
    if codigo != 200:
        return None
    html = V._decodificar(corpo, (cab or {}).get("Content-Type", "")) or ""
    achados = set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = urllib.parse.urljoin(pagina_indice, href.replace("&amp;", "&"))
        url = re.sub(r"/(view|@@display-file/file|download)$", "", url)   # gov.br: página "/view" -> arquivo
        if V.url_permitida(url) and re.search(padrao, url, re.IGNORECASE):
            achados.add(url)
    return max(achados, key=_chave_natural) if achados else None


# ------------------------------------------------------------------------ atualização
def processar(fonte, estado, simular, ano_minimo=0):
    """Atualiza UM PDF. Devolve {"arquivo","status","detalhe","url"}; muda `estado` se gravou."""
    nome = fonte.get("arquivo", "")
    res = {"arquivo": nome, "titulo": fonte.get("titulo", nome), "url": fonte.get("url", ""),
           "status": "erro", "detalhe": ""}

    if fonte.get("ativo", True) is False:
        return dict(res, status="desligado", detalhe="ativo=false em fontes_pdfs.json")
    if not NOME_VALIDO.match(nome):
        return dict(res, detalhe="nome de arquivo inválido (use algo como 7-Nome.pdf)")

    url = fonte.get("url")
    if fonte.get("pagina_indice") and fonte.get("padrao"):
        novo = descobrir_link(fonte["pagina_indice"], fonte["padrao"])
        if novo:
            url = novo
    if not url or not V.url_permitida(url):
        return dict(res, detalhe=f"endereço fora da lista de sites permitidos (Sebrae/Receita): {url}")
    res["url"] = url

    destino = RAIZ / nome
    anterior = estado.get("arquivos", {}).get(nome, {})
    cond = {}
    if destino.is_file() and anterior.get("url") == url:       # só pergunta "mudou?" se for a mesma URL
        if anterior.get("etag"):
            cond["If-None-Match"] = anterior["etag"]
        if anterior.get("last_modified"):
            cond["If-Modified-Since"] = anterior["last_modified"]

    codigo, corpo, cab = V.baixar(url, ("pdf", "octet-stream"), MAX_BYTES, extra=cond, timeout=TIMEOUT_DOWNLOAD)
    if codigo == 304:
        return dict(res, status="igual", detalhe="servidor informou que não mudou (304)")
    if codigo != 200:
        return dict(res, detalhe="não foi possível baixar (site fora do ar, bloqueio ou tipo inesperado)")

    ok, paginas, motivo = validar_pdf(corpo)
    if not ok:
        return dict(res, detalhe=f"rejeitado: {motivo}")

    minimo = fonte.get("ano_minimo", ano_minimo) or 0
    if minimo:
        ano, origem = ano_do_pdf(corpo)
        if ano is None or ano < minimo:
            return dict(res, status="antigo",
                        detalhe=f"edição de {ano or '?'} ({origem}); só entram PDFs de {minimo} em diante. "
                                "Será aceito sozinho quando a fonte publicar a edição nova.")

    hash_novo = sha256(corpo)
    novo_arquivo = not destino.is_file()
    if not novo_arquivo:
        if sha256(destino.read_bytes()) == hash_novo:
            return dict(res, status="igual", detalhe=f"mesmo conteúdo ({paginas} páginas)")
        antigas = paginas_do_arquivo(destino)
        if antigas and paginas < antigas * FRACAO_MINIMA_PAGINAS:
            return dict(res, detalhe=f"rejeitado: a versão nova tem {paginas} páginas e a atual {antigas} "
                                     "(queda suspeita). Confira manualmente.")
    else:
        antigas = 0

    res["status"] = "novo" if novo_arquivo else "atualizado"
    res["detalhe"] = (f"{paginas} páginas" if novo_arquivo else f"{antigas} → {paginas} páginas")
    if not simular:
        tmp = destino.with_suffix(".pdf.tmp")
        tmp.write_bytes(corpo)
        os.replace(tmp, destino)                      # troca atômica: nunca fica um PDF pela metade
        estado.setdefault("arquivos", {})[nome] = {
            "titulo": fonte.get("titulo", nome), "url": url, "sha256": hash_novo, "paginas": paginas,
            "bytes": len(corpo), "baixado_em": agora().isoformat(timespec="seconds"),
            "etag": (cab or {}).get("ETag", ""), "last_modified": (cab or {}).get("Last-Modified", ""),
        }
    return res


def auditar(ano_minimo, arquivar=False, simular=False):
    """Lista os PDFs da base com o ano de cada um; opcionalmente move os antigos para pdfs_arquivados/."""
    import shutil

    antigos = []
    print(f"Ano mínimo: {ano_minimo}\n")
    for caminho in sorted(p for p in RAIZ.iterdir() if p.is_file() and NOME_VALIDO.match(p.name)):
        ano, origem = ano_do_pdf(caminho.read_bytes())
        velho = ano is None or ano < ano_minimo
        print(f"{'🕓 ANTIGO ' if velho else '✅ ok     '} {caminho.name}  — {ano or '?'} ({origem})")
        if velho:
            antigos.append(caminho)
    if arquivar and antigos and not simular:
        PASTA_ARQUIVADOS.mkdir(exist_ok=True)
        estado = carregar_json(ARQ_ESTADO, {"arquivos": {}})
        for c in antigos:
            shutil.move(str(c), PASTA_ARQUIVADOS / c.name)
            estado.get("arquivos", {}).pop(c.name, None)
            print(f"→ arquivado: {c.name}")
        ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    elif antigos:
        print(f"\n{len(antigos)} PDF(s) anterior(es) a {ano_minimo}. Use --arquivar-antigos para tirá-los da base.")
    return 0


def carregar_json(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def relatorio(resultados, simular):
    icone = {"novo": "🆕", "atualizado": "🔄", "igual": "✅", "erro": "⚠️", "desligado": "⏸️", "antigo": "🕓"}
    linhas = [f"# Atualização da base de PDFs — {agora().strftime('%d/%m/%Y %H:%M')}"
              + (" (SIMULAÇÃO)" if simular else ""), "",
              "| | Arquivo | Resultado | Fonte |", "|---|---|---|---|"]
    for r in resultados:
        linhas.append(f"| {icone.get(r['status'], '')} | `{r['arquivo']}` | **{r['status']}** — {r['detalhe']} | {r['url']} |")
    mudou = [r for r in resultados if r["status"] in ("novo", "atualizado")]
    linhas += ["", f"**{len(mudou)} arquivo(s) novo(s)/atualizado(s).**" if mudou else "Nenhuma mudança nas fontes.",
               "", "Só Sebrae e Receita Federal são aceitos como origem (ver `verificador.py`)."]
    return "\n".join(linhas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simular", action="store_true", help="não grava nada; só mostra o que mudaria")
    ap.add_argument("--reindexar", action="store_true", help="refaz o índice de busca após atualizar (local)")
    ap.add_argument("--auditar", action="store_true", help="mostra o ano de cada PDF da base")
    ap.add_argument("--arquivar-antigos", action="store_true",
                    help="move para pdfs_arquivados/ os PDFs da base anteriores ao ano mínimo")
    ap.add_argument("--estrito", action="store_true", help="sai com código 1 se alguma fonte falhar")
    args = ap.parse_args(argv)

    manifesto = carregar_json(ARQ_FONTES, {})
    ano_minimo = int(manifesto.get("ano_minimo", ANO_MINIMO_PADRAO))
    if args.auditar or args.arquivar_antigos:
        return auditar(ano_minimo, arquivar=args.arquivar_antigos, simular=args.simular)
    fontes = manifesto.get("fontes", [])
    if not fontes:
        print("fontes_pdfs.json vazio ou ausente.", file=sys.stderr)
        return 1
    estado = carregar_json(ARQ_ESTADO, {"arquivos": {}})

    resultados = [processar(f, estado, args.simular, ano_minimo) for f in fontes]
    texto = relatorio(resultados, args.simular)
    print(texto)
    ARQ_RELATORIO.write_text(texto, encoding="utf-8")

    mudou = any(r["status"] in ("novo", "atualizado") for r in resultados)
    if mudou and not args.simular:
        estado["atualizado_em"] = agora().isoformat(timespec="seconds")
        ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"mudou={'true' if mudou and not args.simular else 'false'}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(texto + "\n")

    if args.reindexar and mudou and not args.simular:
        import rag

        print("[ATUALIZAR] refazendo o índice de busca...", flush=True)
        rag.construir_indice(rag.criar_embeddings())
    return 1 if args.estrito and any(r["status"] == "erro" for r in resultados) else 0


if __name__ == "__main__":
    sys.exit(main())
