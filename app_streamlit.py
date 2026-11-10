"""Servidor web do Chatbot MEI (Flask).

- Serve a página (static/index.html, style.css, script.js)
- Expõe a API:  GET /api/status   e   POST /api/chat
Para testar localmente:  python app.py   ->  http://localhost:5000
"""
import os
import threading
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

TAMANHO_MAX_PERGUNTA = 1000
LIMITE_REQUISICOES = 15      # por IP...
JANELA_SEGUNDOS = 60         # ...a cada 60 segundos

estado = {"pronto": False, "erro": None, "retriever": None, "llm": None}
_acessos = defaultdict(deque)


def _preparar_rag():
    """Roda em segundo plano para o servidor abrir a porta imediatamente."""
    try:
        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError("Variável de ambiente GROQ_API_KEY não configurada.")
        import rag

        estado["retriever"], estado["llm"] = rag.iniciar_rag()
        estado["pronto"] = True
    except Exception as erro:  # noqa: BLE001
        estado["erro"] = str(erro)
        print(f"[ERRO] Falha ao iniciar o RAG: {erro}", flush=True)


threading.Thread(target=_preparar_rag, daemon=True).start()


def _excedeu_limite(ip):
    agora = time.time()
    fila = _acessos[ip]
    while fila and agora - fila[0] > JANELA_SEGUNDOS:
        fila.popleft()
    if len(fila) >= LIMITE_REQUISICOES:
        return True
    fila.append(agora)
    return False


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    # Sempre 200 (serve também de health check do Render)
    return jsonify(pronto=estado["pronto"], erro=estado["erro"])


@app.post("/api/chat")
def chat():
    if estado["erro"]:
        return jsonify(erro="O chatbot não pôde ser iniciado. Tente novamente mais tarde."), 503
    if not estado["pronto"]:
        return jsonify(erro="O chatbot ainda está sendo preparado. Aguarde alguns segundos."), 503

    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    if _excedeu_limite(ip):
        return jsonify(erro="Muitas perguntas em pouco tempo. Aguarde um instante."), 429

    dados = request.get_json(silent=True) or {}
    pergunta = str(dados.get("pergunta", "")).strip()
    if not pergunta:
        return jsonify(erro="Digite uma pergunta."), 400
    if len(pergunta) > TAMANHO_MAX_PERGUNTA:
        return jsonify(erro=f"A pergunta deve ter até {TAMANHO_MAX_PERGUNTA} caracteres."), 400

    try:
        import rag

        resposta, fontes = rag.responder(estado["retriever"], estado["llm"], pergunta)
        return jsonify(resposta=resposta, fontes=fontes)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Ao responder: {erro}", flush=True)
        return jsonify(erro="Não foi possível gerar a resposta agora. Tente novamente."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
import os # Biblioteca para acessar variáveis de ambiente e listar arquivos
import re # Biblioteca para localizar os PDFs pelo padrão do nome
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
st.title("Chatbot MEI")

# Exibe uma mensagem de orientação ao usuário
st.write("Faça perguntas sobre o Microempreendedor Individual (MEI).")

#Para testar: python -m streamlit run app.py
#ctrl c para parar a execução do streamlit


#3. FONTES DE DADOS (PDFs NA RAIZ DO PROJETO)

# Os PDFs devem estar na mesma pasta do app.py e seguir o padrão:
# número de 1 a 5, hífen, qualquer nome, terminando em "_MEI.pdf"
# Exemplos: 1-Cartilha_MEI.pdf, 2-Perguntas_Respostas_MEI.pdf ... 5-Impostos_PF_MEI.pdf
# A busca ignora diferença entre maiúsculas e minúsculas (_MEI.pdf ou _Mei.pdf)
PADRAO_ARQUIVOS = re.compile(r"^[1-5]-.*_mei\.pdf$", re.IGNORECASE)

# Pasta onde está o app.py (raiz do projeto)
PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))


def listar_pdfs():

    # Lista os arquivos da raiz do projeto que seguem o padrão e os ordena (1, 2, 3, 4, 5)
    return sorted(
        arquivo for arquivo in os.listdir(PASTA_PROJETO)
        if PADRAO_ARQUIVOS.match(arquivo)
    )


#4. PREPARAÇÃO DO RAG

# Armazena em cache os recursos criados pela função
# Evita reconstruir o banco vetorial a cada interação
@st.cache_resource
def iniciar_rag():

    #4.1. CARREGAMENTO DOS DOCUMENTOS PDF

    # Localiza os PDFs do MEI na raiz do projeto
    arquivos_pdf = listar_pdfs()

    # Interrompe se nenhum PDF for encontrado
    if not arquivos_pdf:
        raise FileNotFoundError(
            "Nenhum PDF encontrado na raiz do projeto. "
            "Esperado: arquivos de 1 a 5 terminando em '_MEI.pdf'."
        )

    # Lista que reunirá as páginas de todos os PDFs
    documentos = []

    for nome_arquivo in arquivos_pdf:

        # Cria o carregador do arquivo PDF
        carregador = PyPDFLoader(os.path.join(PASTA_PROJETO, nome_arquivo))

        # Lê o PDF e transforma seu conteúdo em objetos Document (um por página)
        paginas = carregador.load()

        # Registra o nome do arquivo de origem nos metadados de cada página
        for pagina in paginas:
            pagina.metadata["fonte"] = nome_arquivo

        # Adiciona as páginas à lista geral
        documentos.extend(paginas)


    #4.2. DIVISÃO DOS DOCUMENTOS EM CHUNKS

    # Configura a divisão dos textos em trechos menores
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,    # Tamanho máximo de cada chunk em caracteres
        chunk_overlap=200   # Sobreposição entre os chunks
    )

    # Divide os documentos em chunks
    chunks = splitter.split_documents(documentos)


    #4.3. MODELO DE EMBEDDINGS

    # Carrega o modelo de embeddings do Hugging Face. O modelo transforma textos em vetores numéricos
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )


    #4.4. BANCO VETORIAL CHROMA

    # Cria o banco vetorial a partir dos chunks
    # O Chroma utiliza o modelo configurado para gerar os embeddings e armazenar textos, vetores e metadados
    banco = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings
    )


    #4.5. CONFIGURAÇÃO DO RETRIEVER

    # Configura o recuperador de informações do Chroma
    # Ele será responsável por buscar os chunks mais
    # relevantes para cada pergunta do usuário
    # o recuperador buscará os cinco chunks mais semelhantes à pergunta
    retriever = banco.as_retriever(search_kwargs={"k": 5})


    #4.6. CONFIGURAÇÃO DO LLM

    # Configura o modelo de linguagem acessado pela Groq
    # O LLM será responsável por gerar as respostas
    # com base nos trechos recuperados dos documentos do MEI
    # Configura a temperatura do modelo, que influencia a variabilidade das respostas.
    # Valores menores tendem a produzir respostas mais consistentes e menos variadas.
    llm = ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0
    )

    # Retorna os componentes que serão utilizados no chatbot
    return retriever, llm


#5. INICIALIZAÇÃO DO CHATBOT

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


#6. HISTÓRICO DE MENSAGENS

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


#7. RECEBER A PERGUNTA DO USUÁRIO

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


    #8. RECUPERAR INFORMAÇÕES E GERAR RESPOSTA

    # Cria o espaço para a resposta do chatbot
    with st.chat_message("assistant"):

        # Exibe uma mensagem durante o processamento
        with st.spinner("Consultando os documentos do MEI..."):

            try:
                # Recupera os chunks relevantes para a pergunta
                documentos = retriever.invoke(pergunta)

                # Reúne os textos recuperados em um único contexto,
                # identificando o arquivo de origem de cada trecho
                contexto = "\n\n".join(
                    f"[Fonte: {doc.metadata.get('fonte', 'desconhecida')}]\n{doc.page_content}"
                    for doc in documentos
                )

                # Monta as instruções enviadas ao modelo de linguagem
                prompt = f"""
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
                # Exibe uma mensagem caso ocorra um erro
                st.error(f"Erro ao responder: {erro}")