"""Proteção de dados pessoais (LGPD) nas perguntas do FiscOn.

O que faz: troca CPF, CNPJ, e-mail e telefone por marcadores ("[CPF]", "[CNPJ]"...) ANTES de a pergunta
ser usada — ou seja, antes de ir para os logs do servidor, para a inteligência artificial (Groq) e para
o cache. O bot não precisa desses números para responder (o prompt já pede que eles nunca sejam
repetidos), e valores em reais, datas e anos continuam intactos para os cálculos de regras_fiscais.py.

Só biblioteca padrão.
"""
import re

# Ordem importa: formatos com pontuação primeiro, depois as sequências de dígitos "soltas".
_PADROES = [
    (re.compile(r"(?<![\w@.])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])"), "[e-mail]"),
    # CNPJ: 12.345.678/0001-90 (também com o novo CNPJ alfanumérico: AB.CDE.FGH/IJKL-90)
    (re.compile(r"(?<![\w/])[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}(?!\d)"), "[CNPJ]"),
    # CPF: 123.456.789-09 ou 123456789-09
    (re.compile(r"(?<![\d.])\d{3}\.?\d{3}\.?\d{3}-\d{2}(?!\d)"), "[CPF]"),
    # Telefone com DDD: (11) 91234-5678, 11 91234-5678, +55 11 912345678
    (re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-\d{4}(?!\d)"), "[telefone]"),
    # sequências soltas de dígitos (mas não "12345678909,00", que é dinheiro, nem partes de datas)
    (re.compile(r"(?<![\d/])(?<!\d[.,])\d{14}(?![\d/]|[.,]\d)"), "[CNPJ]"),              # 14 dígitos
    (re.compile(r"(?<![\d/])(?<!\d[.,])\d{11}(?![\d/]|[.,]\d)"), "[CPF ou telefone]"),   # 11 dígitos
]


def mascarar(texto):
    """Texto com CPF, CNPJ, e-mail e telefone trocados por marcadores. Aceita None (devolve "")."""
    if not texto:
        return "" if texto is None else texto
    texto = str(texto)
    for padrao, marcador in _PADROES:
        texto = padrao.sub(marcador, texto)
    return texto


def mascarar_historico(historico):
    """Mesma proteção em cada mensagem do histórico enviado pelo navegador (estrutura preservada)."""
    if not isinstance(historico, list):
        return []
    saida = []
    for m in historico:
        if isinstance(m, dict):
            m = dict(m)
            for campo in ("texto", "content"):
                if isinstance(m.get(campo), str):
                    m[campo] = mascarar(m[campo])
        saida.append(m)
    return saida


def tem_dado_pessoal(texto):
    return mascarar(texto) != (texto or "")
