"""Lógica RAG do Chatbot MEI.

O índice vetorial é gerado durante o build do Render por construir_indice.py.
Na inicialização do servidor, apenas carregamos o índice persistido.
"""
import os
import re
import shutil

from pathlib import Path

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_BANCO = PASTA_PROJETO / "chroma_db"
PASTA_MODELO = PASTA_PROJETO / "modelo_cache"
NOME_COLECAO = "mei_documentos"

# Mantém o padrão dos cinco documentos oficiais utilizados pelo projeto.
PADRAO_ARQUIVOS = re.compile(r"^[1-5]-.*_mei\.pdf$", re.IGNORECASE)


def listar_pdfs():
    """Localiza e ordena os PDFs do MEI na raiz do projeto."""
    return sorted(
        arquivo
        for arquivo in PASTA_PROJETO.iterdir()
        if arquivo.is_file() and PADRAO_ARQUIVOS.match(arquivo.name)
    )


def criar_embeddings():
    """Cria o modelo leve ONNX e reutiliza o cache baixado no build."""
    from langchain_community.embeddings import FastEmbedEmbeddings

    return FastEmbedEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(PASTA_MODELO),
        threads=2,
        doc_embed_type="passage",
        query_embed_type="query",
    )


def construir_indice(embeddings):
    """Lê os PDFs, gera os chunks e grava o índice vetorial em disco."""
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_chroma import Chroma

    arquivos = listar_pdfs()
    if not arquivos:
        raise FileNotFoundError(
            "Nenhum PDF MEI encontrado na raiz do projeto. "
            "Esperado: arquivos de 1 a 5 terminando em '_MEI.pdf'."
        )

    documentos = []
    for arquivo in arquivos:
        print(f"[INDEX] Lendo {arquivo.name}...", flush=True)
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
        raise RuntimeError(
            "Os PDFs foram encontrados, mas nenhum texto pôde ser extraído."
        )

    print(f"[INDEX] {len(chunks)} trechos preparados.", flush=True)

    # Evita misturar dados de uma versão anterior dos documentos.
    if PASTA_BANCO.exists():
        shutil.rmtree(PASTA_BANCO)

    banco = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        collection_name=NOME_COLECAO,
        persist_directory=str(PASTA_BANCO),
    )

    print("[INDEX] Banco vetorial criado com sucesso.", flush=True)
    return banco


def iniciar_rag():
    """Carrega o índice persistido e configura o modelo de linguagem."""
    from langchain_chroma import Chroma
    from langchain_groq import ChatGroq

    embeddings = criar_embeddings()

    # Fallback útil para execução local ou caso o build não tenha criado o índice.
    if not PASTA_BANCO.is_dir() or not any(PASTA_BANCO.iterdir()):
        print("[RAG] Índice não encontrado; criando a base...", flush=True)
        banco = construir_indice(embeddings)
    else:
        print("[RAG] Carregando índice vetorial persistido...", flush=True)
        banco = Chroma(
            collection_name=NOME_COLECAO,
            persist_directory=str(PASTA_BANCO),
            embedding_function=embeddings,
        )

    retriever = banco.as_retriever(search_kwargs={"k": 5})
    llm = ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0,
    )
    return retriever, llm


def montar_prompt(contexto, pergunta):
    return f"""
Você é um assistente especializado em Microempreendedor Individual (MEI).

Responda utilizando apenas as informações
dos documentos do MEI fornecidos abaixo.
Não utilize conhecimento externo.

Se não encontrar a resposta, informe que
a informação não foi localizada nos documentos do MEI.

Contexto:
{contexto}

Pergunta:
{pergunta}
"""


def responder(retriever, llm, pergunta):
    """Recupera os trechos relevantes e gera a resposta com indicação das fontes."""
    documentos = retriever.invoke(pergunta)
    contexto = "\n\n".join(
        f"[Fonte: {documento.metadata.get('fonte', 'desconhecida')}]\n"
        f"{documento.page_content}"
        for documento in documentos
    )
    resposta = llm.invoke(montar_prompt(contexto, pergunta)).content
    fontes = sorted({
        documento.metadata.get("fonte", "desconhecida")
        for documento in documentos
    })
    return resposta, fontes
