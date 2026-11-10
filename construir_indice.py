"""Executado no BUILD do Render: baixa o modelo de embeddings e cria o banco vetorial.

Assim, quando o servidor sobe (ou acorda após ficar inativo), ele só
carrega o banco pronto em vez de reprocessar todos os PDFs.
"""
import rag

if __name__ == "__main__":
    print("Baixando modelo de embeddings...")
    embeddings = rag.criar_embeddings()
    print("Construindo banco vetorial a partir dos PDFs...")
    rag.construir_indice(embeddings)
    print("Índice criado com sucesso.")
