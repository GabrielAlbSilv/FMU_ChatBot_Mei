"""Configuração central do FiscOn: TODAS as variáveis de ambiente lidas pelo projeto ficam aqui.

Cada módulo importa daqui (ex.: `import config as C; ATIVO = C.VERIFICAR_SITES`), então este arquivo
é a lista completa e única do que pode ser ajustado no .env (local) ou em Render > Environment.
Os valores são lidos uma vez, quando o módulo é importado (mesmo comportamento de antes).

Ver também .env.example (mesma lista, com explicações para quem configura o servidor).
"""
import os

try:   # lê o .env também quando um script é rodado sozinho (preparar_base.py, base_web.py...)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:   # testes no CI podem rodar sem python-dotenv
    pass


def texto(nome, padrao=""):
    valor = os.getenv(nome)
    return padrao if valor is None or valor.strip() == "" else valor.strip()


def flag(nome, padrao=True):
    """'0' desliga, qualquer outro valor liga; vazio/ausente = padrão."""
    valor = os.getenv(nome)
    if valor is None or valor.strip() == "":
        return padrao
    return valor.strip() != "0"


def inteiro(nome, padrao):
    try:
        return int(float(texto(nome, str(padrao))))
    except ValueError:
        return padrao


def decimal(nome, padrao):
    try:
        return float(texto(nome, str(padrao)))
    except ValueError:
        return padrao


# ----------------------------------------------------------------- segredos (nunca vão para o status)
GROQ_API_KEY = texto("GROQ_API_KEY")
BASE_WEB_TOKEN = texto("BASE_WEB_TOKEN")        # POST /api/base-web/atualizar (GitHub Actions)
STATUS_TOKEN = texto("STATUS_TOKEN") or BASE_WEB_TOKEN   # detalhes técnicos de /api/status (cabeçalho X-Token)

# ------------------------------------------------------------------------------------- servidor
PORTA = inteiro("PORT", 5000)
COMMIT = texto("RENDER_GIT_COMMIT", "?")[:8]
TAMANHO_MAX_PERGUNTA = inteiro("TAMANHO_MAX_PERGUNTA", 1000)
LIMITE_POR_IP = inteiro("LIMITE_POR_IP", 15)            # perguntas por IP...
LIMITE_GLOBAL = inteiro("LIMITE_GLOBAL", 60)            # ...e no total (todas as pessoas juntas)...
JANELA_SEGUNDOS = inteiro("JANELA_SEGUNDOS", 60)        # ...a cada N segundos
# Cabeçalho com o IP real de quem acessa. No Render, o Cloudflare (na frente do servidor) preenche
# True-Client-IP e o cliente não consegue falsificar. O X-Forwarded-For NÃO é usado: o Render só
# acrescenta valores a ele, então o primeiro IP pode ser inventado por quem faz o pedido.
CABECALHO_IP = texto("CABECALHO_IP", "True-Client-IP")

# ------------------------------------------------------------------------------------- modelos
MODELO_EMBEDDINGS = texto("MODELO_EMBEDDINGS", "sentence-transformers/all-MiniLM-L6-v2")
MODELO_LLM = texto("MODELO_LLM", "openai/gpt-oss-120b")
MODELO_REESCRITA = texto("MODELO_REESCRITA", "openai/gpt-oss-20b")
MODELO_VERIFICACAO = texto("MODELO_VERIFICACAO", MODELO_LLM)
MODELO_TRANSCRICAO = texto("MODELO_TRANSCRICAO", "whisper-large-v3-turbo")
ESFORCO_RESPOSTA = texto("ESFORCO_RESPOSTA", "low")
REESCREVER_CONSULTA = flag("REESCREVER_CONSULTA", True)
# A reescrita da pergunta também sugere dados (faturamento, mês de abertura...). O código só aceita
# o que conseguir confirmar no texto do usuário. 0 = usa só as regras de texto (regex).
EXTRAIR_DADOS_LLM = flag("EXTRAIR_DADOS_LLM", True)

# --------------------------------------------------------------------------- resposta e conferência
CONFERENCIA_MODO = texto("CONFERENCIA_MODO", "depois").lower()
WEB_NA_RESPOSTA = inteiro("WEB_NA_RESPOSTA", 2)
VERIFICAR_SITES = flag("VERIFICAR_SITES", True)
VERIFICAR_BUSCA_DESCOBERTA = flag("VERIFICAR_BUSCA_DESCOBERTA", False)
PESQUISA_ATIVA = flag("PESQUISA_ATIVA", True)
PESQUISA_LIMIAR = decimal("PESQUISA_LIMIAR", 0.5)
# Máximo de trechos (de 8) que um único PDF pode ocupar na resposta. Evita que o PDF do IRPF
# (340 páginas, ~65% da base) tome todas as vagas em perguntas sobre MEI. Perguntas de IR não têm limite.
MAX_TRECHOS_POR_PDF = inteiro("MAX_TRECHOS_POR_PDF", 5)

# ------------------------------------------------------------------------------------- base web
BASE_WEB_ATIVA = flag("BASE_WEB_ATIVA", True)
BASE_WEB_HORAS = decimal("BASE_WEB_HORAS", 12)
BASE_WEB_MAX_PAGINAS = inteiro("BASE_WEB_MAX_PAGINAS", 80)
BASE_WEB_PDFS = flag("BASE_WEB_PDFS", True)
BASE_WEB_ATRASO_S = inteiro("BASE_WEB_ATRASO_S", 20)

# ------------------------------------------------------------------------------------- build
ATUALIZAR_PDFS_NO_BUILD = flag("ATUALIZAR_PDFS_NO_BUILD", True)
COLETAR_WEB_NO_BUILD = flag("COLETAR_WEB_NO_BUILD", True)

# ------------------------------------------------------------------------------------- voz
MAX_MB_AUDIO = decimal("MAX_MB_AUDIO", 6)


def publico():
    """Configuração atual SEM segredos (aparece nos detalhes de /api/status para quem tem o token)."""
    nomes = ["MODELO_EMBEDDINGS", "MODELO_LLM", "MODELO_REESCRITA", "MODELO_VERIFICACAO", "MODELO_TRANSCRICAO",
             "ESFORCO_RESPOSTA", "REESCREVER_CONSULTA", "EXTRAIR_DADOS_LLM", "CONFERENCIA_MODO", "WEB_NA_RESPOSTA",
             "VERIFICAR_SITES", "PESQUISA_ATIVA", "PESQUISA_LIMIAR", "MAX_TRECHOS_POR_PDF", "BASE_WEB_ATIVA",
             "BASE_WEB_HORAS", "BASE_WEB_MAX_PAGINAS", "LIMITE_POR_IP", "LIMITE_GLOBAL", "JANELA_SEGUNDOS",
             "CABECALHO_IP"]
    return {n: globals()[n] for n in nomes}
