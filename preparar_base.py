"""Compatibilidade: utiliza o mesmo processo de construção do índice do Render."""
from construir_indice import rag


if __name__ == "__main__":
    print("[BUILD] Carregando modelo de embeddings...", flush=True)
    embeddings = rag.criar_embeddings()
    print("[BUILD] Construindo índice a partir dos PDFs do MEI...", flush=True)
    rag.construir_indice(embeddings)
    print("[BUILD] Preparação concluída.", flush=True)
