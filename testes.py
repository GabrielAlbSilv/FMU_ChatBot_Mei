"""Testes offline (sem internet e sem Groq) do verificador e do atualizar_base.

Rode:  python testes.py
"""
import json
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import atualizar_base as A
import verificador as v


class ListaDeSites(unittest.TestCase):
    def test_permitidos(self):
        for u in [
            "https://sebrae.com.br/empreendedores/guiaparaomei/ja-sou-mei/regularizar-mei",
            "https://www.sebrae.com.br/sites/PortalSebrae/x",
            "https://blog.rn.sebrae.com.br/guia-mei/",
            "https://www8.receita.fazenda.gov.br/SimplesNacional/Servicos/Grupo.aspx?grp=8",
            "https://www.gov.br/receitafederal/pt-br/assuntos/meu-imposto-de-renda",
            "https://gov.br/receitafederal",
        ]:
            self.assertTrue(v.url_permitida(u), u)

    def test_bloqueados(self):
        for u in [
            "http://sebrae.com.br/x",                                   # sem https
            "https://sebrae.com.br.malicioso.com/x",                    # domínio parecido
            "https://malicioso.com/?u=sebrae.com.br",                   # nome só na query
            "https://malicioso.com/sebrae.com.br",                      # nome só no caminho
            "https://sebrae.com.br@malicioso.com/x",                    # truque do "@"
            "https://usuario:senha@sebrae.com.br/x",
            "https://sebrae.com.br:8443/x",                             # porta estranha
            "https://notsebrae.com.br/x",                               # sufixo sem ponto
            "https://www.gov.br/fazenda/pt-br/assuntos",                # gov.br fora da Receita
            "https://www.gov.br/empresas-e-negocios/pt-br/empreendedor",
            "https://www.gov.br/receitafederalfalsa/x",                 # prefixo sem barra
            "https://www.gov.br/pt-br/servicos/declarar-meu-imposto-de-renda",
            "https://duckduckgo.com/?q=sebrae.com.br",
            "https://www.google.com/",
            "ftp://sebrae.com.br/x", "javascript:alert(1)", "", None,
        ]:
            self.assertFalse(v.url_permitida(u), u)


class Download(unittest.TestCase):
    def test_redirecionamento_para_fora_e_bloqueado(self):
        chamadas = []

        def falso_open(req, timeout=0):
            chamadas.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                         {"Location": "https://malicioso.com/x"}, None)

        with mock.patch.object(v._abridor, "open", side_effect=falso_open):
            self.assertIsNone(v.baixar_pagina("https://sebrae.com.br/a"))
        self.assertEqual(chamadas, ["https://sebrae.com.br/a"])   # nunca chegou ao site de fora

    def test_url_inicial_fora_da_lista_nem_e_acessada(self):
        with mock.patch.object(v._abridor, "open") as abrir:
            self.assertIsNone(v.baixar_pagina("https://malicioso.com/"))
            abrir.assert_not_called()


HTML = """<html><head><title>DAS do MEI - Sebrae</title><style>.x{}</style></head><body>
<nav>Menu enorme de navegação que deve ser ignorado completamente aqui</nav>
<p>O valor do DAS do MEI em 2026 é de R$ 82,05 para comércio e indústria e sobe conforme o salário mínimo.</p>
<p>Texto qualquer sem relação com o assunto principal desta página institucional do Sebrae.</p>
<script>var x = "ignorar isto";</script></body></html>"""


class Extracao(unittest.TestCase):
    def test_extrai_o_paragrafo_relevante(self):
        titulo, texto = v.extrair_trecho(HTML, "qual o valor do DAS do MEI")
        self.assertEqual(titulo, "DAS do MEI - Sebrae")
        self.assertIn("R$ 82,05", texto)
        self.assertNotIn("Menu enorme", texto)
        self.assertNotIn("ignorar isto", texto)


class Resposta:
    def __init__(self, texto):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=texto))]


class ClienteFalso:
    def __init__(self, texto):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **k: Resposta(texto)))


PDFS = [{"fonte": "1-Cartilha_MEI.pdf", "pagina": 3, "texto": "O DAS custa R$ 67,00."}]
WEB = [{"url": "https://sebrae.com.br/a", "titulo": "DAS", "site": "Sebrae",
        "texto": "O valor do DAS do MEI em 2026 é de R$ 82,05."}]


class Fluxo(unittest.TestCase):
    def rodar(self, json_llm, trechos=WEB):
        with mock.patch.object(v, "coletar_trechos", return_value=trechos):
            return v.verificar(ClienteFalso(json_llm), "m", {}, "quanto é o DAS?", "valor DAS", "O DAS custa R$ 67,00 [1].", PDFS)

    def test_confirmado(self):
        r = self.rodar(json.dumps({"veredito": "confirmado", "divergencias": [], "resposta": ""}))
        self.assertEqual(r["status"], "confirmado")
        self.assertEqual(r["fontes_web"][0]["url"], "https://sebrae.com.br/a")

    def test_desatualizado_gera_nova_resposta(self):
        nova = "Segundo o site do Sebrae, o DAS do MEI em 2026 é de **R$ 82,05** [W1]. Os PDFs citavam valor antigo [1]."
        saida = "```json\n" + json.dumps({"veredito": "desatualizado", "resposta": nova, "divergencias": [
            {"tema": "valor do DAS", "pdf": "R$ 67,00", "site": "R$ 82,05", "fonte": 1}]}) + "\n```"
        r = self.rodar(saida)
        self.assertEqual(r["status"], "atualizada")
        self.assertNotIn("[W1]", r["resposta"])
        self.assertIn("82,05", r["resposta"])
        self.assertEqual(r["divergencias"][0]["site"], "R$ 82,05")
        self.assertEqual(len(r["fontes_web"]), 1)

    def test_desatualizado_sem_citar_site_nao_e_aceito(self):
        r = self.rodar(json.dumps({"veredito": "desatualizado", "resposta": "O DAS agora é R$ 82,05, segundo eu acho de cabeça.",
                                   "divergencias": []}))
        self.assertEqual(r["status"], "inconclusivo")      # resposta sem [W#] = sem base nos sites

    def test_inconclusivo(self):
        r = self.rodar(json.dumps({"veredito": "inconclusivo", "divergencias": [], "resposta": ""}))
        self.assertEqual(r["status"], "inconclusivo")

    def test_sem_paginas_ou_json_invalido(self):
        self.assertEqual(self.rodar("{}", trechos=[])["status"], "indisponivel")
        r = self.rodar("não é json")                      # JSON inválido NÃO pode virar "inconclusivo"
        self.assertEqual(r["status"], "indisponivel")
        self.assertEqual(r["consultadas"], 1)

    def test_json_invalido_na_1a_tentativa_e_valido_na_2a(self):
        respostas = iter(["Claro! Aqui vai: ```", json.dumps({"veredito": "confirmado", "motivo": "o site traz R$ 82,05"})])
        c = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **k: Resposta(next(respostas)))))
        with mock.patch.object(v, "coletar_trechos", return_value=WEB):
            r = v.verificar(c, "m", {}, "p", "c", "r", PDFS)
        self.assertEqual(r["status"], "confirmado")
        self.assertEqual(r["motivo"], "o site traz R$ 82,05")

    def test_erro_do_llm_mantem_resposta_dos_pdfs(self):
        def erro(**k):
            raise RuntimeError("limite")
        c = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=erro)))
        with mock.patch.object(v, "coletar_trechos", return_value=WEB):
            self.assertEqual(v.verificar(c, "m", {}, "p", "c", "r", PDFS)["status"], "indisponivel")

    def test_descoberta_ignora_resultado_fora_da_lista(self):
        html = ('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fmalicioso.com%2Fx&rut=1">x</a>'
                '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsebrae.com.br%2Fok&rut=1">y</a>')

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, n): return html.encode()
        with mock.patch.object(v.urllib.request, "urlopen", return_value=R()):
            self.assertEqual(v._buscar_ddg("das", "sebrae.com.br"), ["https://sebrae.com.br/ok"])

    def test_sementes_so_com_sites_permitidos(self):
        for u in v.urls_das_sementes("como faço a declaração anual dasn e pago o das atrasado, imposto de renda"):
            self.assertTrue(v.url_permitida(u), u)
        self.assertTrue(v.urls_das_sementes("quem deve declarar imposto de renda"))





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


class Leitura_ao_vivo(unittest.TestCase):
    def test_verificador_le_pdf(self):
        import verificador as V
        V._cache_paginas.clear()
        with mock.patch.object(V, "baixar", return_value=(200, pdf_falso(2, "limite do MEI"), {"Content-Type": "application/pdf"})):
            t = V.obter_trecho("https://www8.receita.fazenda.gov.br/x/y.pdf", "limite do MEI")
        self.assertIsNotNone(t)
        self.assertIn("limite do MEI", t["texto"])
        self.assertEqual(t["site"], "Receita Federal")


class Respostas(unittest.TestCase):
    """Partes do rag.py que não dependem de fastembed nem da Groq."""

    @classmethod
    def setUpClass(cls):
        import rag
        cls.rag = rag

    def test_trechos_vizinhos_sao_fundidos_sem_repetir_texto(self):
        texto = " ".join(f"palavra{i}" for i in range(400))
        partes = self.rag._dividir(texto)
        trechos = [{"fonte": "1-A.pdf", "pagina": 1 + n // 2, "texto": t} for n, (_, t) in enumerate(partes)]
        trechos.append({"fonte": "2-B.pdf", "pagina": 9, "texto": "outro documento " * 10})
        ordem = [1, len(trechos) - 1, 2, 0]                  # 0,1,2 são vizinhos no mesmo PDF
        grupos = self.rag.juntar_vizinhos(trechos, ordem, k=3)
        self.assertEqual(len(grupos), 2)                          # 4 candidatos viram 2 grupos
        self.assertEqual(grupos[0]["fonte"], "1-A.pdf")          # o grupo do melhor colocado vem primeiro
        esperado = " ".join(f"palavra{i}" for i in range(400))
        self.assertTrue(esperado.startswith(grupos[0]["texto"]))   # colou sem duplicar a sobreposição
        self.assertEqual(grupos[0]["paginas"], sorted({t["pagina"] for t in trechos[:3]}))
        self.assertEqual(grupos[1]["fonte"], "2-B.pdf")

    def test_nao_junta_pdfs_diferentes(self):
        trechos = [{"fonte": "1-A.pdf", "pagina": 1, "texto": "a " * 50},
                   {"fonte": "2-B.pdf", "pagina": 1, "texto": "b " * 50}]
        self.assertEqual(len(self.rag.juntar_vizinhos(trechos, [0, 1], k=5)), 2)

    def test_conversa_simples(self):
        for p in ("oi", "Oi, tudo bem?", "Bom dia FiscOn!", "obrigado pela ajuda", "valeu!",
                  "O que você sabe fazer?", "quem é você?"):
            self.assertTrue(self.rag.eh_conversa_simples(p), p)
        for p in ("quem pode ser MEI?", "como funciona", "ok, e se eu atrasar o DAS?",
                  "oi, quanto custa o DAS?", "obrigado, e o imposto de renda?", ""):
            self.assertFalse(self.rag.eh_conversa_simples(p), p)

    def test_sugestoes_com_e_sem_marcador(self):
        r, s = self.rag._separar_sugestoes("Resposta.\n@@SUGESTOES: E se eu atrasar? | Como emitir o DAS?")
        self.assertEqual((r, s), ("Resposta.", ["E se eu atrasar?", "Como emitir o DAS?"]))
        r, s = self.rag._separar_sugestoes("Resposta.\n\n**Sugestões:** - Quem pode ser MEI?\n- Como declarar?")
        self.assertEqual(r, "Resposta.")
        self.assertEqual(s, ["Quem pode ser MEI?", "Como declarar?"])
        texto = "Sugestões: guarde os comprovantes por 5 anos e mantenha o CNPJ em dia. " + "x" * 500
        self.assertEqual(self.rag._separar_sugestoes(texto)[0], texto.strip())   # não corta texto legítimo

    def test_atalhos_oficiais(self):
        acoes = self.rag.detectar_acoes("Como emitir o boleto do DAS atrasado?", "Acesse o PGMEI.")
        self.assertEqual(acoes[0]["icone"], "boleto")
        self.assertTrue(all(a["url"].startswith("https://") for a in acoes))
        # IR só aparece se a PESSOA perguntou de IR (não basta a resposta mencionar)
        self.assertFalse(any(a["icone"] == "leao" for a in
                             self.rag.detectar_acoes("Quanto é o DAS?", "O MEI não paga imposto de renda no DAS.")))
        self.assertEqual(self.rag.detectar_acoes("Quem precisa declarar imposto de renda?", "")[0]["icone"], "leao")
        self.assertEqual(self.rag.detectar_acoes("Meu CPF está pendente de regularização", "")[0]["icone"], "cpf")
        self.assertEqual(self.rag.detectar_acoes("como tirar o cartão do CNPJ?", "")[0]["icone"], "cnpj")

    def test_sinonimos_de_ir_nao_puxam_o_mei(self):
        self.assertIn("dasn", self.rag.tokenizar("como declarar o faturamento do MEI", consulta=True))
        self.assertNotIn("dasn", self.rag.tokenizar("quem precisa declarar imposto de renda", consulta=True))

    def _cliente(self, texto, chamadas):
        def criar(**k):
            chamadas.append(k)
            if k.get("stream"):
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content=texto[i:i + 7]))]) for i in range(0, len(texto), 7)])
            return Resposta("consulta reescrita sobre o DAS do MEI")
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=criar)))

    def test_fluxo_completo_com_citacao_data_e_sugestoes(self):
        trechos = [{"fonte": "4-Perguntas&Respostas3_MEI.pdf", "pagina": 7, "paginas": [7, 8],
                    "texto": "O DAS vence no dia 20."}]
        indice = types.SimpleNamespace(buscar_varias=lambda consultas, k=8: trechos)
        chamadas = []
        cliente = self._cliente("O DAS vence no **dia 20** [1].\n@@SUGESTOES: E se eu atrasar? | Como emito o DAS?", chamadas)
        self.rag._cache.clear()
        with mock.patch.object(self.rag.verificador, "ATIVO", False):
            eventos = list(self.rag.responder_stream(indice, cliente, "Quando vence o DAS?", []))
        texto_mostrado = "".join(d for t, d in eventos if t == "texto")
        self.assertNotIn("@@", texto_mostrado)                       # a linha de sugestões não vaza
        fim = eventos[-1][1]
        self.assertEqual(fim["resposta"], "O DAS vence no **dia 20**.")
        self.assertEqual(fim["sugestoes"], ["E se eu atrasar?", "Como emito o DAS?"])
        self.assertEqual(fim["fontes"][0]["paginas"], [7, 8])
        self.assertIn("rotulo", fim["fontes"][0])
        self.assertEqual(len(chamadas), 2)                            # reescrita + resposta
        prompt = chamadas[-1]["messages"][-1]["content"]
        self.assertIn("Data de hoje:", prompt)
        self.assertIn("p. 7–8", prompt)

    def test_saudacao_nao_busca_nem_reescreve(self):
        indice = types.SimpleNamespace(buscar_varias=mock.Mock(side_effect=AssertionError("não devia buscar")))
        chamadas = []
        self.rag._cache.clear()
        with mock.patch.object(self.rag.verificador, "ATIVO", True):   # nem com a conferência ligada
            eventos = list(self.rag.responder_stream(indice, self._cliente("Olá! Como posso ajudar?", chamadas), "oi", []))
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(eventos[-1][1]["resposta"], "Olá! Como posso ajudar?")
        self.assertEqual(eventos[-1][1]["verificacao"]["status"], "nao_aplicavel")


if __name__ == "__main__":
    unittest.main()
