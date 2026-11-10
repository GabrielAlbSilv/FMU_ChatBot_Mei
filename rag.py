"""Lógica do RAG (mesma ideia do app.py original em Streamlit).

Fica separada do servidor para poder ser usada também no build do Render
(construir_indice.py), deixando o chatbot rápido para iniciar.
"""
import os
import re

PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))
PASTA_BANCO = os.path.join(PASTA_PROJETO, "chroma_db")
PASTA_MODELO = os.path.join(PASTA_PROJETO, "modelo_cache")

# Mesmo padrão do projeto original: 1-...._MEI.pdf até 5-...._MEI.pdf
PADRAO_ARQUIVOS = re.compile(r"^[1-5]-.*_mei\.pdf$", re.IGNORECASE)


def listar_pdfs():
    return sorted(
        a for a in os.listdir(PASTA_PROJETO) if PADRAO_ARQUIVOS.match(a)
    )


def criar_embeddings():
    # FastEmbed roda o MESMO modelo (all-MiniLM-L6-v2) via ONNX, sem PyTorch.
    # Usa muito menos memória, o que é essencial nos 512 MB do Render.
    from langchain_community.embeddings import FastEmbedEmbeddings

    return FastEmbedEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=PASTA_MODELO,
    )


def construir_indice(embeddings):
    """Lê os PDFs, divide em chunks e grava o banco vetorial em disco."""
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_chroma import Chroma

    arquivos = listar_pdfs()
    if not arquivos:
        raise FileNotFoundError(
            "Nenhum PDF encontrado na raiz do projeto "
            "(esperado: arquivos de 1 a 5 terminando em '_MEI.pdf')."
        )

    documentos = []
    for nome in arquivos:
        paginas = PyPDFLoader(os.path.join(PASTA_PROJETO, nome)).load()
        for pagina in paginas:
            pagina.metadata["fonte"] = nome
        documentos.extend(paginas)

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = splitter.split_documents(documentos)

    return Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=PASTA_BANCO,
    )


def iniciar_rag():
    """Carrega o banco já pronto (ou cria, se não existir) e configura o LLM."""
    from langchain_chroma import Chroma
    from langchain_groq import ChatGroq

    embeddings = criar_embeddings()

    if os.path.isdir(PASTA_BANCO) and os.listdir(PASTA_BANCO):
        banco = Chroma(persist_directory=PASTA_BANCO, embedding_function=embeddings)
    else:
        banco = construir_indice(embeddings)

    retriever = banco.as_retriever(search_kwargs={"k": 5})
    llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0)
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
    documentos = retriever.invoke(pergunta)
    contexto = "\n\n".join(
        f"[Fonte: {d.metadata.get('fonte', 'desconhecida')}]\n{d.page_content}"
        for d in documentos
    )
    resposta = llm.invoke(montar_prompt(contexto, pergunta)).content
    fontes = sorted({d.metadata.get("fonte", "desconhecida") for d in documentos})
    return resposta, fontes
