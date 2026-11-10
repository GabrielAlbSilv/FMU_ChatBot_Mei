import os # Biblioteca para acessar variáveis de ambiente
import streamlit as st # Framework para criar a interface web do chatbot
from dotenv import load_dotenv # Função que carrega as variáveis do arquivo .env
from langchain_community.document_loaders import PyPDFLoader # Carrega documentos em formato PDF
from langchain_text_splitters import RecursiveCharacterTextSplitter # Divide textos longos em trechos menores (chunks)
from langchain_chroma import Chroma # Banco de dados vetorial utilizado no projeto
from langchain_groq import ChatGroq # Integra o Python à API da Groq para utilizar o LLM
from langchain_huggingface import HuggingFaceEmbeddings # Gera embeddings utilizando modelos do Hugging Face

#1. CONFIGURAÇÃO DA API GROQ

# Carrega as variáveis armazenadas no arquivo .env
load_dotenv()

# Verifica se a chave GROQ_API_KEY está disponível
if not os.getenv("GROQ_API_KEY"):

    # Exibe uma mensagem de erro na interface Streamlit
    st.error("Configure a chave GROQ_API_KEY no arquivo .env")

    # Interrompe a execução da aplicação
    st.stop()


#2. INTERFACE INICIAL

# Exibe o título principal da aplicação
st.title("Chatbot Trabalho Garantido")

# Exibe uma mensagem de orientação ao usuário
st.write("Faça perguntas sobre o edital do programa.")

#Para testar: python -m streamlit run app.py
#ctrl c para parar a execução do streamlit


#3. PREPARAÇÃO DO RAG

# Armazena em cache os recursos criados pela função
# Evita reconstruir o banco vetorial a cada interação
@st.cache_resource
def iniciar_rag():

    #3.1. CARREGAMENTO DO DOCUMENTO PDF

    # Cria o carregador do arquivo PDF
    carregador = PyPDFLoader("EDITAL_Trabalho_Garantido.pdf")

    # Lê o PDF e transforma seu conteúdo em objetos Document
    documentos = carregador.load()

    # Exibe a quantidade de páginas carregadas
    #st.write("Quantidade de páginas carregadas:", len(documentos))

    # Exibe o texto extraído da primeira página
    #st.subheader("Texto da primeira página")

    #st.write(documentos[0].page_content)


    #3.2. DIVISÃO DO DOCUMENTO EM CHUNKS

    # Configura a divisão dos textos em trechos menores
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,    # Tamanho máximo de cada chunk em caracteres
        chunk_overlap=200   # Sobreposição entre os chunks
    )

    # Divide os documentos em chunks
    chunks = splitter.split_documents(documentos)


    #3.3. MODELO DE EMBEDDINGS

    # Carrega o modelo de embeddings do Hugging Face. O modelo transforma textos em vetores numéricos
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )


    #3.4. BANCO VETORIAL CHROMA

    # Cria o banco vetorial a partir dos chunks
    # O Chroma utiliza o modelo configurado para gerar os embeddings e armazenar textos, vetores e metadados
    banco = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings
    )


    #3.5. CONFIGURAÇÃO DO RETRIEVER

    # Configura o recuperador de informações do Chroma
    # Ele será responsável por buscar os chunks mais
    # relevantes para cada pergunta do usuário
    # o recuperador buscará os cinco chunks mais semelhantes à pergunta
    retriever = banco.as_retriever(search_kwargs={"k": 5})


    #3.6. CONFIGURAÇÃO DO LLM

    # Configura o modelo de linguagem acessado pela Groq
    # O LLM será responsável por gerar as respostas
    # com base nos trechos recuperados do edital
    # Configura a temperatura do modelo, que influencia a variabilidade das respostas.
    # Valores menores tendem a produzir respostas mais consistentes e menos variadas.
    llm = ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0
    )

    # Retorna os componentes que serão utilizados no chatbot
    return retriever, llm


#4. INICIALIZAÇÃO DO CHATBOT

try:

    # Exibe uma mensagem enquanto o RAG é preparado
    with st.spinner("Preparando o chatbot..."):

        # Executa a função e recebe o retriever e o LLM
        retriever, llm = iniciar_rag()

except Exception as erro:

    # Exibe a mensagem caso ocorra algum erro
    st.error(f"Erro ao iniciar: {erro}")

    # Interrompe a aplicação
    st.stop()


#5. HISTÓRICO DE MENSAGENS

# Verifica se a lista de mensagens já existe na sessão
if "mensagens" not in st.session_state:

    # Cria uma lista vazia para armazenar o histórico
    st.session_state.mensagens = []


# Percorre as mensagens armazenadas no histórico
for mensagem in st.session_state.mensagens:

    # Cria um balão de conversa para cada mensagem
    # "user" representa o usuário
    # "assistant" representa o chatbot
    with st.chat_message(mensagem["role"]):

        # Exibe o conteúdo da mensagem na interface
        st.markdown(mensagem["content"])


#6. RECEBER A PERGUNTA DO USUÁRIO

# Exibe o campo para o usuário digitar sua pergunta
if pergunta := st.chat_input("Digite sua pergunta..."):

    # Exibe a pergunta na interface do chatbot
    with st.chat_message("user"):
        st.write(pergunta)

    # Armazena a pergunta no histórico de mensagens
    st.session_state.mensagens.append({
        "role": "user",
        "content": pergunta
    })


    #7. RECUPERAR INFORMAÇÕES E GERAR RESPOSTA

    # Cria o espaço para a resposta do chatbot
    with st.chat_message("assistant"):

        # Exibe uma mensagem durante o processamento
        with st.spinner("Consultando o edital..."):

            try:
                # Recupera os chunks relevantes para a pergunta
                documentos = retriever.invoke(pergunta)

                # Reúne os textos recuperados em um único contexto
                contexto = "\n\n".join(
                    doc.page_content for doc in documentos
                )

                # Monta as instruções enviadas ao modelo de linguagem
                prompt = f"""
                Você é um assistente do Programa Trabalho Garantido.

                Responda utilizando apenas as informações
                do edital fornecidas abaixo.

                Se não encontrar a resposta, informe que
                a informação não foi localizada no edital.

                Contexto:
                {contexto}

                Pergunta:
                {pergunta}
                """

                # Envia o prompt ao LLM e recebe a resposta
                resposta = llm.invoke(prompt).content

                # Exibe a resposta na interface
                st.markdown(resposta)

                # Armazena a resposta no histórico
                st.session_state.mensagens.append({
                    "role": "assistant",
                    "content": resposta
                })

            except Exception as erro:
                # Exibe uma mensagem caso ocorra algum erro
                st.error(f"Erro ao responder: {erro}")