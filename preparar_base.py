"""Gera o índice de busca (trechos + embeddings) a partir dos PDFs.

Roda no build do Render (ver render.yaml) para que o servidor não precise
processar os PDFs a cada inicialização. Localmente:  python preparar_base.py
"""
import rag


if __name__ == "__main__":
    print("[BUILD] Carregando modelo de embeddings...", flush=True)
    embeddings = rag.criar_embeddings()
    print("[BUILD] Construindo índice a partir dos PDFs do MEI...", flush=True)
    rag.construir_indice(embeddings)
    print("[BUILD] Preparação concluída.", flush=True)
