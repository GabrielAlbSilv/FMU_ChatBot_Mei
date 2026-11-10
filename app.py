import os
import re
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_groq import ChatGroq
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

# ============================================================
# CONFIGURAÇÕES
# ============================================================

load_dotenv()

PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_BANCO = PASTA_PROJETO / "chroma_db"
PASTA_MODELO = PASTA_PROJETO / ".fastembed_cache"
NOME_COLECAO = "mei_documentos"

st.set_page_config(page_title="Chatbot MEI", page_icon="💬", layout="centered")

if not os.getenv("GROQ_API_KEY"):
    st.error("Configure a variável GROQ_API_KEY nas Environment Variables do Render ou no arquivo .env.")
    st.stop()

st.title("Chatbot MEI")
st.write("Faça perguntas sobre o Microempreendedor Individual (MEI).")

# ============================================================
# RECURSOS COMPARTILHADOS E CACHEADOS
# ============================================================

@st.cache_resource(show_spinner="Carregando mecanismo de busca...")
def carregar_embeddings():
    """Carrega o modelo ONNX do FastEmbed uma única vez por processo."""
    if not PASTA_MODELO.exists():
        raise FileNotFoundError(
            "Modelo de embeddings não encontrado. Execute preparar_base.py "
            "durante o Build Command do Render."
        )

    return FastEmbedEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(PASTA_MODELO),
        threads=2,
        doc_embed_type="passage",
        query_embed_type="query",
    )


@st.cache_resource(show_spinner="Conectando à base de conhecimento...")
def carregar_rag():
    """Abre a base vetorial pronta; não relê PDFs nem recalcula seus vetores."""
    if not PASTA_BANCO.exists() or not any(PASTA_BANCO.iterdir()):
        raise FileNotFoundError(
            "Banco vetorial não encontrado. Verifique se o comando "
            "'python preparar_base.py' foi executado no Build Command do Render."
        )

    embeddings = carregar_embeddings()

    banco = Chroma(
        collection_name=NOME_COLECAO,
        persist_directory=str(PASTA_BANCO),
        embedding_function=embeddings,
    )

    retriever = banco.as_retriever(
        search_type="similarity",
        search_kwargs={"k": 4},
    )

    llm = ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY"),
    )

    return retriever, llm


# ============================================================
# INICIALIZAÇÃO
# ============================================================

try:
    retriever, llm = carregar_rag()
except Exception as erro:
    st.error(f"Erro ao iniciar o chatbot: {erro}")
    st.stop()

# ============================================================
# HISTÓRICO DA CONVERSA
# ============================================================

if "mensagens" not in st.session_state:
    st.session_state.mensagens = []

for mensagem in st.session_state.mensagens:
    with st.chat_message(mensagem["role"]):
        st.markdown(mensagem["content"])

# ============================================================
# PERGUNTAS E RESPOSTAS
# ============================================================

if pergunta := st.chat_input("Digite sua pergunta..."):
    st.session_state.mensagens.append({
        "role": "user",
        "content": pergunta,
    })

    with st.chat_message("user"):
        st.markdown(pergunta)

    with st.chat_message("assistant"):
        with st.spinner("Consultando os documentos do MEI..."):
            try:
                documentos = retriever.invoke(pergunta)

                contexto = "\n\n".join(
                    f"[Fonte: {doc.metadata.get('fonte', 'desconhecida')}]\n"
                    f"{doc.page_content}"
                    for doc in documentos
                )

                prompt = f"""
Você é um assistente especializado em Microempreendedor Individual (MEI).

Responda utilizando apenas as informações dos documentos do MEI fornecidos abaixo.
Não utilize conhecimento externo.

Se não encontrar a resposta nos trechos fornecidos, informe que a informação
não foi localizada nos documentos do MEI.

Contexto:
{contexto}

Pergunta:
{pergunta}
"""

                resposta = llm.invoke(prompt).content
                st.markdown(resposta)

                st.session_state.mensagens.append({
                    "role": "assistant",
                    "content": resposta,
                })

            except Exception as erro:
                st.error(f"Erro ao responder: {erro}")
