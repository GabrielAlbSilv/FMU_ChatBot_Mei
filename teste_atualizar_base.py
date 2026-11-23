"""Testes offline do atualizar_base.py (sem internet).  Rode: python teste_atualizar_base.py"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atualizar_base as A


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


class Validacao(Base):
    def test_pdf_valido(self):
        ok, pag, _ = A.validar_pdf(pdf_falso(4))
        self.assertTrue(ok)
        self.assertEqual(pag, 4)

    def test_rejeita_html_e_corrompido(self):
        self.assertFalse(A.validar_pdf(b"<html>Erro 404</html>".ljust(20000, b" "))[0])
        self.assertFalse(A.validar_pdf(b"%PDF-1.4 lixo".ljust(20000, b"x"))[0])
        self.assertFalse(A.validar_pdf(b"%PDF-1.4")[0])


class Atualizacao(Base):
    def test_arquivo_novo(self):
        estado = {}
        with self.baixar(pdf_falso(3)):
            r = A.processar(self.fonte, estado, simular=False)
        self.assertEqual(r["status"], "novo")
        self.assertTrue((Path(self.tmp.name) / "5-Teste_MEI.pdf").is_file())
        self.assertEqual(estado["arquivos"]["5-Teste_MEI.pdf"]["paginas"], 3)

    def test_mesmo_conteudo_nao_regrava(self):
        estado = {}
        with self.baixar(pdf_falso(3)):
            A.processar(self.fonte, estado, False)
            r = A.processar(self.fonte, estado, False)
        self.assertEqual(r["status"], "igual")

    def test_versao_nova_substitui(self):
        estado = {}
        with self.baixar(pdf_falso(3, "2025")):
            A.processar(self.fonte, estado, False)
        with self.baixar(pdf_falso(4, "2026")):
            r = A.processar(self.fonte, estado, False)
        self.assertEqual(r["status"], "atualizado")
        self.assertIn("3 → 4", r["detalhe"])
        self.assertEqual(A.paginas_do_arquivo(Path(self.tmp.name) / "5-Teste_MEI.pdf"), 4)

    def test_queda_suspeita_de_paginas_mantem_o_antigo(self):
        estado = {}
        destino = Path(self.tmp.name) / "5-Teste_MEI.pdf"
        with self.baixar(pdf_falso(10)):
            A.processar(self.fonte, estado, False)
        antigo = destino.read_bytes()
        with self.baixar(pdf_falso(2, "outro")):
            r = A.processar(self.fonte, estado, False)
        self.assertEqual(r["status"], "erro")
        self.assertEqual(destino.read_bytes(), antigo)

    def test_pdf_invalido_nao_substitui(self):
        estado = {}
        destino = Path(self.tmp.name) / "5-Teste_MEI.pdf"
        with self.baixar(pdf_falso(3)):
            A.processar(self.fonte, estado, False)
        antigo = destino.read_bytes()
        with self.baixar(b"<html>Manutencao</html>".ljust(20000, b" ")):
            r = A.processar(self.fonte, estado, False)
        self.assertEqual(r["status"], "erro")
        self.assertEqual(destino.read_bytes(), antigo)

    def test_304_e_falha_de_rede(self):
        estado = {}
        with self.baixar(pdf_falso(3)):
            A.processar(self.fonte, estado, False)
        with self.baixar(None, codigo=304):
            self.assertEqual(A.processar(self.fonte, estado, False)["status"], "igual")
        with self.baixar(None, codigo=None):
            self.assertEqual(A.processar(self.fonte, estado, False)["status"], "erro")

    def test_simular_nao_grava(self):
        estado = {}
        with self.baixar(pdf_falso(3)):
            r = A.processar(self.fonte, estado, simular=True)
        self.assertEqual(r["status"], "novo")
        self.assertFalse((Path(self.tmp.name) / "5-Teste_MEI.pdf").exists())
        self.assertEqual(estado, {})

    def test_fonte_fora_da_lista_e_nome_invalido(self):
        with mock.patch.object(A.V, "baixar") as b:
            for f in ({**self.fonte, "url": "https://malicioso.com/x.pdf"},
                      {**self.fonte, "url": "https://www.gov.br/fazenda/x.pdf"},
                      {**self.fonte, "arquivo": "../../etc/passwd.pdf"},
                      {**self.fonte, "arquivo": "semnumero.pdf"}):
                self.assertEqual(A.processar(f, {}, False)["status"], "erro", f)
            b.assert_not_called()                                    # nem tentou baixar
        self.assertEqual(A.processar({**self.fonte, "ativo": False}, {}, False)["status"], "desligado")


class Descoberta(Base):
    def test_escolhe_o_link_mais_novo_e_ignora_fora_da_lista(self):
        html = ('<a href="/receitafederal/pt-br/p/p-r-irpf-2025-v1-00-2025-04-10.pdf/view">a</a>'
                '<a href="/receitafederal/pt-br/p/p-r-irpf-2026-v1-00-2026-04-23.pdf/view">b</a>'
                '<a href="https://malicioso.com/p-r-irpf-2027-v9.pdf">c</a>'
                '<a href="https://www.gov.br/fazenda/p-r-irpf-2030.pdf">d</a>')
        with mock.patch.object(A.V, "baixar", return_value=(200, html.encode(), {"Content-Type": "text/html"})):
            url = A.descobrir_link("https://www.gov.br/receitafederal/pt-br/p", r"p-r-irpf-\d{4}[^/]*\.pdf")
        self.assertEqual(url, "https://www.gov.br/receitafederal/pt-br/p/p-r-irpf-2026-v1-00-2026-04-23.pdf")


class Leitura_ao_vivo(unittest.TestCase):
    def test_verificador_le_pdf(self):
        import verificador as V
        V._cache_paginas.clear()
        with mock.patch.object(V, "baixar", return_value=(200, pdf_falso(2, "limite do MEI"), {"Content-Type": "application/pdf"})):
            t = V.obter_trecho("https://www8.receita.fazenda.gov.br/x/y.pdf", "limite do MEI")
        self.assertIsNotNone(t)
        self.assertIn("limite do MEI", t["texto"])
        self.assertEqual(t["site"], "Receita Federal")


if __name__ == "__main__":
    unittest.main(verbosity=1)
