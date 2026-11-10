import re
import shutil
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_chroma import Chroma

# ============================================================
# PREPARAÇÃO DA BASE VETORIAL DURANTE O DEPLOY
# ============================================================

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_BANCO = PASTA_PROJETO / "chroma_db"
PASTA_MODELO = PASTA_PROJETO / ".fastembed_cache"
NOME_COLECAO = "mei_documentos"

PADRAO_ARQUIVOS = re.compile(r"^[1-5]-.*_mei\.pdf$", re.IGNORECASE)


def listar_pdfs():
    return sorted(
        arquivo for arquivo in PASTA_PROJETO.iterdir()
        if arquivo.is_file() and PADRAO_ARQUIVOS.match(arquivo.name)
    )


def main():
    arquivos_pdf = listar_pdfs()

    if not arquivos_pdf:
        raise FileNotFoundError(
            "Nenhum PDF MEI encontrado. Os arquivos devem estar na raiz "
            "e seguir o padrão 1-..._MEI.pdf até 5-..._MEI.pdf."
        )

    print(f"Encontrados {len(arquivos_pdf)} PDFs. Lendo documentos...")
    documentos = []

    for arquivo in arquivos_pdf:
        print(f"Lendo: {arquivo.name}")
        paginas = PyPDFLoader(str(arquivo)).load()

        for pagina in paginas:
            pagina.metadata["fonte"] = arquivo.name

        documentos.extend(paginas)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
    )
    chunks = splitter.split_documents(documentos)

    if not chunks:
        raise RuntimeError("Os PDFs foram encontrados, mas nenhum texto pôde ser extraído.")

    print(f"Foram gerados {len(chunks)} trechos.")
    print("Baixando/carregando o modelo de embeddings e gerando os vetores...")

    embeddings = FastEmbedEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(PASTA_MODELO),
        threads=2,
        doc_embed_type="passage",
        query_embed_type="query",
    )

    # O banco é recriado no build para refletir sempre os PDFs atuais.
    if PASTA_BANCO.exists():
        shutil.rmtree(PASTA_BANCO)

    Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        collection_name=NOME_COLECAO,
        persist_directory=str(PASTA_BANCO),
    )

    print("Base vetorial criada com sucesso.")
    print(f"Banco: {PASTA_BANCO}")
    print(f"Modelo: {PASTA_MODELO}")


if __name__ == "__main__":
    main()
