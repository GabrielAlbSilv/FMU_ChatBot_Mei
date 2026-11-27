"""Prepara tudo o que o servidor precisa, no build do Render (ver render.yaml) ou localmente.

    python preparar_base.py

Etapas (cada uma pode ser desligada por variável de ambiente; falhas de internet NÃO param o build):
  1. ATUALIZAR_PDFS_NO_BUILD=1 -> baixa as versões mais novas dos PDFs oficiais (atualizar_base.py).
     Assim cada deploy já sai com os PDFs de 2026 mais recentes, mesmo sem commit no GitHub.
  2. Gera o índice de busca (trechos + embeddings) a partir dos PDFs — como antes.
  3. COLETAR_WEB_NO_BUILD=1 -> faz a primeira coleta dos sites do Sebrae e da Receita Federal
     (base_web.py), para o servidor já começar com a cópia local pronta.
"""
import sys
import time

import config as C
import rag

ATUALIZAR_PDFS_NO_BUILD = C.ATUALIZAR_PDFS_NO_BUILD
COLETAR_WEB_NO_BUILD = C.COLETAR_WEB_NO_BUILD


def _etapa_opcional(nome, funcao):
    inicio = time.time()
    try:
        funcao()
        print(f"[BUILD] {nome}: ok ({time.time() - inicio:.0f}s)", flush=True)
    except BaseException as erro:  # noqa: BLE001  (sem internet, site fora do ar... o build segue)
        if isinstance(erro, KeyboardInterrupt):
            raise
        print(f"[BUILD] {nome}: pulado ({type(erro).__name__}: {erro})", flush=True)


def _atualizar_pdfs():
    import atualizar_base

    atualizar_base.main([])


def _coletar_web():
    import base_web

    base_web.VERIFICAR_PDFS = False      # os PDFs acabaram de ser atualizados na etapa 1
    base_web.carregar()
    base_web.atualizar_agora()


if __name__ == "__main__":
    if ATUALIZAR_PDFS_NO_BUILD:
        print("[BUILD] Buscando versões novas dos PDFs (Sebrae e Receita Federal)...", flush=True)
        _etapa_opcional("atualização dos PDFs", _atualizar_pdfs)

    print("[BUILD] Carregando modelo de embeddings...", flush=True)
    embeddings = rag.criar_embeddings()
    print("[BUILD] Construindo índice a partir dos PDFs do MEI...", flush=True)
    rag.construir_indice(embeddings)

    if COLETAR_WEB_NO_BUILD:
        print("[BUILD] Coletando páginas oficiais (base web)...", flush=True)
        _etapa_opcional("coleta da base web", _coletar_web)
    print("[BUILD] Preparação concluída.", flush=True)
    sys.exit(0)
