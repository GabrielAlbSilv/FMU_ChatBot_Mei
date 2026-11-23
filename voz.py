"""Voz do FiscOn: transforma o áudio gravado no navegador em texto (Groq Whisper).

A outra metade da voz — ler as respostas em voz alta — roda no próprio navegador (Web Speech API,
speechSynthesis), sem custo e sem enviar nada ao servidor.

Uso (app.py):  texto = voz.transcrever(cliente_groq, bytes_do_audio, "pergunta.webm", "audio/webm")

Configuração opcional (.env / Render):
  MODELO_TRANSCRICAO=whisper-large-v3-turbo   (rápido e barato; se a Groq recusar, usa whisper-large-v3)
  MAX_MB_AUDIO=6                              (≈ 1 a 2 minutos de fala em webm/ogg)
"""
import os
import re
import unicodedata

MODELO_TRANSCRICAO = os.getenv("MODELO_TRANSCRICAO", "whisper-large-v3-turbo")
MODELO_RESERVA = "whisper-large-v3"
MAX_BYTES_AUDIO = int(float(os.getenv("MAX_MB_AUDIO", "6")) * 1024 * 1024)
MIN_BYTES_AUDIO = 1200          # menos que isso é clique sem fala

# Tipos de áudio que os navegadores gravam (Chrome/Edge: webm; Firefox: ogg; Safari: mp4) e que o
# Whisper aceita. Parâmetros como ";codecs=opus" são ignorados na comparação.
TIPOS_ACEITOS = {
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/x-m4a": "m4a",
    "audio/m4a": "m4a", "audio/aac": "m4a", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
}

# O "prompt" do Whisper serve de dica de vocabulário e grafia: melhora muito siglas como DASN-SIMEI.
VOCABULARIO = ("Pergunta sobre MEI e Receita Federal: DAS, DASN-SIMEI, Simei, CNPJ, CPF, Simples Nacional, "
               "Imposto de Renda, IRPF, INSS, nota fiscal, NFS-e, e-CAC, gov.br, faturamento, restituição, "
               "malha fina, desenquadramento, microempreendedor individual.")

# Frases que o Whisper "inventa" quando o áudio é só silêncio ou ruído (vícios de legendas do YouTube).
_ALUCINACOES = re.compile(
    r"legendas? (pela|por) comunidade|amara\.org|inscreva-se|obrigad[oa] por assistir|"
    r"tchau,? tchau,? tchau", re.I)


class ErroVoz(ValueError):
    """Problema no áudio enviado (mensagem pronta para mostrar ao usuário)."""


def _sem_acento(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def extensao_do_tipo(tipo):
    return TIPOS_ACEITOS.get((tipo or "").split(";")[0].strip().lower())


def validar_audio(conteudo, tipo):
    """Confere tamanho e formato. Devolve a extensão do arquivo; levanta ErroVoz se não servir."""
    ext = extensao_do_tipo(tipo)
    if not ext:
        raise ErroVoz("Formato de áudio não suportado por este navegador.")
    if not conteudo or len(conteudo) < MIN_BYTES_AUDIO:
        raise ErroVoz("Não ouvi nada. Toque no microfone e fale a sua pergunta.")
    if len(conteudo) > MAX_BYTES_AUDIO:
        raise ErroVoz("O áudio ficou muito longo. Tente uma pergunta mais curta.")
    return ext


def limpar_transcricao(texto):
    """Tira espaços, aspas e as frases que o Whisper inventa em áudio sem fala."""
    texto = re.sub(r"\s+", " ", str(texto or "")).strip().strip('"“”').strip()
    if not texto or _ALUCINACOES.search(_sem_acento(texto)):
        return ""
    return texto[:1000]


def _erro_de_modelo(erro):
    status = getattr(erro, "status_code", None)
    texto = str(erro).lower()
    return status in (400, 404) and ("model" in texto or "decommission" in texto)


_modelo_atual = MODELO_TRANSCRICAO


def transcrever(cliente, conteudo, nome="pergunta.webm", tipo="audio/webm"):
    """Áudio -> texto em português. Levanta ErroVoz (áudio ruim) ou o erro da Groq (limite, rede...)."""
    global _modelo_atual
    ext = validar_audio(conteudo, tipo)
    nome = f"pergunta.{ext}"
    for tentativa in range(2):
        modelo = _modelo_atual
        try:
            r = cliente.audio.transcriptions.create(
                file=(nome, conteudo),
                model=modelo,
                language="pt",
                prompt=VOCABULARIO,
                response_format="json",
                temperature=0,
            )
            return limpar_transcricao(getattr(r, "text", None) if not isinstance(r, dict) else r.get("text"))
        except Exception as erro:  # noqa: BLE001
            if tentativa == 0 and modelo != MODELO_RESERVA and _erro_de_modelo(erro):
                print(f"[VOZ] modelo '{modelo}' indisponível; usando {MODELO_RESERVA}", flush=True)
                _modelo_atual = MODELO_RESERVA
                continue
            raise
    return ""
