"""Peças compartilhadas pelos testes: clientes falsos da Groq, PDFs falsos e páginas de exemplo.

Os módulos de teste fazem `from tests.apoio import *`.
"""
import json  # noqa: F401
import os
import sys
import tempfile
import types  # noqa: F401
import unittest
import urllib.error  # noqa: F401
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# A pesquisa geral (pesquisa.py) baixa sitemaps e páginas: nos testes ela fica desligada, exceto na
# classe Pesquisa, que a liga com a internet simulada.
os.environ.setdefault("PESQUISA_ATIVA", "0")

import atualizar_base as A  # noqa: E402
import verificador as v  # noqa: E402


class Resposta:
    def __init__(self, texto):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=texto))]


class ClienteFalso:
    def __init__(self, texto):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **k: Resposta(texto)))


PDFS = [{"fonte": "1-Cartilha_MEI.pdf", "pagina": 3, "texto": "O DAS custa R$ 67,00."}]
WEB = [{"url": "https://sebrae.com.br/a", "titulo": "DAS", "site": "Sebrae",
        "texto": "O valor do DAS do MEI em 2026 é de R$ 82,05."}]


def pdf_falso(paginas=3, palavra="MEI"):
    """Cria um PDF mínimo, mas válido, com texto em cada página."""
    texto = ("(" + (f"Perguntas e respostas sobre {palavra} e Receita Federal. " * 8) + ") Tj").encode()
    objs, ids_pag = [], []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(paginas))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {paginas} >>".encode())
    fonte_id = 3 + paginas * 2
    for i in range(paginas):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + i * 2} 0 R "
                    f"/Resources << /Font << /F1 {fonte_id} 0 R >> >> >>".encode())
        fluxo = b"BT /F1 10 Tf 20 700 Td " + texto + b" ET"
        objs.append(b"<< /Length %d >>\nstream\n" % len(fluxo) + fluxo + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    saida, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, start=1):
        offsets.append(len(saida))
        saida += b"%d 0 obj\n" % n + o + b"\nendobj\n"
    xref = len(saida)
    saida += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        saida += b"%010d 00000 n \n" % off
    saida += b"trailer\n<< /Root 1 0 R /Size %d >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return saida + b"%" + b"x" * (A.MIN_BYTES + 100) + b"\n"      # comentário final: passa do tamanho mínimo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(A, "RAIZ", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.fonte = {"arquivo": "5-Teste_MEI.pdf", "titulo": "Teste",
                      "url": "https://www.gov.br/receitafederal/pt-br/x/teste-2026.pdf"}

    def baixar(self, corpo, codigo=200, cab=None):
        return mock.patch.object(A.V, "baixar", return_value=(codigo, corpo, cab or {"ETag": '"abc"'}))


PAGINA_INICIO = """<html><head><title>Notícias - Receita Federal</title></head><body>
<a href="/receitafederal/pt-br/assuntos/noticias/2026/maio/mei-tem-ate-31-de-maio-para-entregar-declaracao">MEI</a>
<a href="/receitafederal/pt-br/assuntos/noticias/2026/maio/receita-lanca-curso-de-libras">fora do tema</a>
<a href="https://malicioso.com/receitafederal/pt-br/assuntos/noticias/2026/mei-golpe">golpe</a>
<p>Página de notícias da Receita Federal sobre impostos e serviços ao cidadão brasileiro.</p></body></html>"""
PAGINA_MEI = """<html><head><title>MEI tem até 31 de maio para entregar a declaração anual</title></head><body>
<p>Publicado em 02/05/2026 10h00. Atualizado em 05/05/2026 14h12.</p>
<p>O Microempreendedor Individual (MEI) deve entregar a DASN-SIMEI até 31 de maio de 2026, informando o faturamento de 2025.</p>
<p>A multa por atraso na entrega da declaração anual do MEI é de 2% ao mês, limitada a 20%, com valor mínimo de R$ 50,00.</p>
</body></html>"""
PAGINA_SEM_TEMA = "<html><head><title>Futebol</title></head><body><p>O campeonato de futebol começa no domingo com muitos jogos.</p></body></html>"
