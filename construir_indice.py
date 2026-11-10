"""Gera o índice vetorial durante o build do Render.

O processamento dos PDFs e a geração dos embeddings acontecem no deploy,
para que o servidor não precise reconstruir a base a cada inicialização.
"""
import rag


if __name__ == "__main__":
    print("[BUILD] Carregando modelo de embeddings...", flush=True)
    embeddings = rag.criar_embeddings()
    print("[BUILD] Construindo índice a partir dos PDFs do MEI...", flush=True)
    rag.construir_indice(embeddings)
    print("[BUILD] Preparação concluída.", flush=True)
