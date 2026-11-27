"""atualizar_base.py: validação, troca e filtro de ano dos PDFs oficiais."""
from tests.apoio import *  # noqa: F401,F403


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


class FiltroDeAno(Base):
    def test_edicao_antiga_e_recusada_e_nova_aceita(self):
        destino = Path(self.tmp.name) / "5-Teste_MEI.pdf"
        with self.baixar(pdf_falso(3, "edicao 2025")):
            r = A.processar(self.fonte, {}, False, ano_minimo=2026)
        self.assertEqual(r["status"], "antigo")
        self.assertIn("2025", r["detalhe"])
        self.assertFalse(destino.exists())                      # nada gravado
        with self.baixar(pdf_falso(3, "edicao 2026")):
            self.assertEqual(A.processar(self.fonte, {}, False, ano_minimo=2026)["status"], "novo")

    def test_sem_ano_na_capa_e_recusado(self):
        with self.baixar(pdf_falso(3, "sem data")):
            self.assertEqual(A.processar(self.fonte, {}, False, ano_minimo=2026)["status"], "antigo")

    def test_auditar_arquiva_sem_apagar(self):
        raiz = Path(self.tmp.name)
        (raiz / "1-Velho.pdf").write_bytes(pdf_falso(2, "edicao 2024"))
        (raiz / "2-Novo.pdf").write_bytes(pdf_falso(2, "edicao 2026"))
        with mock.patch.object(A, "PASTA_ARQUIVADOS", raiz / "pdfs_arquivados"), \
             mock.patch.object(A, "ARQ_ESTADO", raiz / "base_status.json"):
            A.auditar(2026, arquivar=True)
        self.assertFalse((raiz / "1-Velho.pdf").exists())
        self.assertTrue((raiz / "pdfs_arquivados" / "1-Velho.pdf").is_file())   # guardado, não apagado
        self.assertTrue((raiz / "2-Novo.pdf").is_file())


class Descoberta(Base):
    def test_escolhe_o_link_mais_novo_e_ignora_fora_da_lista(self):
        html = ('<a href="/receitafederal/pt-br/p/p-r-irpf-2025-v1-00-2025-04-10.pdf/view">a</a>'
                '<a href="/receitafederal/pt-br/p/p-r-irpf-2026-v1-00-2026-04-23.pdf/view">b</a>'
                '<a href="https://malicioso.com/p-r-irpf-2027-v9.pdf">c</a>'
                '<a href="https://www.gov.br/fazenda/p-r-irpf-2030.pdf">d</a>')
        with mock.patch.object(A.V, "baixar", return_value=(200, html.encode(), {"Content-Type": "text/html"})):
            url = A.descobrir_link("https://www.gov.br/receitafederal/pt-br/p", r"p-r-irpf-\d{4}[^/]*\.pdf")
        self.assertEqual(url, "https://www.gov.br/receitafederal/pt-br/p/p-r-irpf-2026-v1-00-2026-04-23.pdf")

class PdfNovoNoSite(Base):
    def test_url_alternativa_e_corpo_para_a_base_web(self):
        respostas = {"https://www8.receita.fazenda.gov.br/a.pdf": (None, None, None),
                     "https://www8.receita.fazenda.gov.br/b.pdf": (200, pdf_falso(3, "edicao 2026"), {})}
        fonte = dict(self.fonte, url="https://www8.receita.fazenda.gov.br/a.pdf",
                     urls_alternativas=["https://www8.receita.fazenda.gov.br/b.pdf"])
        with mock.patch.object(A.V, "baixar", side_effect=lambda url, *a, **k: respostas[url]):
            r = A.processar(fonte, {}, simular=True, guardar_corpo=True)
        self.assertEqual(r["status"], "novo")
        self.assertEqual(r["url"], "https://www8.receita.fazenda.gov.br/b.pdf")
        self.assertTrue(r["corpo"].startswith(b"%PDF"))
        self.assertFalse((Path(self.tmp.name) / "5-Teste_MEI.pdf").exists())    # simulação: nada gravado
