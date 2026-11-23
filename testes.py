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


class BaseWeb(unittest.TestCase):
    """Coleta periódica dos sites oficiais (base_web.py), sem internet."""

    def setUp(self):
        import base_web as B
        self.B = B
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sementes = Path(self.tmp.name) / "fontes_oficiais.json"
        sementes.write_text(json.dumps({"paginas": [{"palavras": ["futebol"], "urls": ["https://sebrae.com.br/futebol"]}],
                                        "rastrear": [{"inicio": "https://www.gov.br/receitafederal/pt-br/assuntos/noticias",
                                                      "seguir": r"/noticias/20\d\d/.*mei", "max": 5}]}), encoding="utf-8")
        paginas = {"https://www.gov.br/receitafederal/pt-br/assuntos/noticias": PAGINA_INICIO,
                   "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/maio/mei-tem-ate-31-de-maio-para-entregar-declaracao": PAGINA_MEI,
                   "https://sebrae.com.br/futebol": PAGINA_SEM_TEMA}
        self.baixados = []

        def falso_baixar(url, *a, **k):
            self.baixados.append(url)
            if url in paginas:
                return 200, paginas[url].encode(), {"Content-Type": "text/html; charset=utf-8"}
            return None, None, None
        for p in (mock.patch.object(v, "ARQ_SEMENTES", sementes),
                  mock.patch.object(v, "baixar", side_effect=falso_baixar),
                  mock.patch.object(B, "ARQ_BASE", Path(self.tmp.name) / "base_web.json"),
                  mock.patch.object(B, "PASTA", Path(self.tmp.name)),
                  mock.patch.object(B, "VERIFICAR_PDFS", False)):
            p.start()
            self.addCleanup(p.stop)
        B._estado["paginas"] = {}
        B._reindexar()
        self.addCleanup(lambda: (B._estado.update(paginas={}, atualizada_em=None), B._reindexar()))

    def test_coleta_segue_links_permitidos_e_filtra_o_tema(self):
        st = self.B.atualizar_agora()
        self.assertTrue(all(v.url_permitida(u) for u in self.baixados))           # nunca saiu da lista
        self.assertNotIn("https://malicioso.com/receitafederal/pt-br/assuntos/noticias/2026/mei-golpe", self.baixados)
        urls = set(self.B._estado["paginas"])
        self.assertIn("https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/maio/mei-tem-ate-31-de-maio-para-entregar-declaracao", urls)
        self.assertNotIn("https://sebrae.com.br/futebol", urls)                      # fora do tema MEI/Receita
        self.assertTrue(st["atualizada_em"])
        self.assertTrue(self.B.ARQ_BASE.is_file())                                 # salvo em disco

    def test_busca_local_e_data_da_pagina(self):
        self.B.atualizar_agora()
        r = self.B.buscar("multa atraso declaração anual MEI")
        self.assertEqual(len(r), 1)
        self.assertIn("2% ao mês", r[0]["texto"])
        self.assertEqual(r[0]["data"], "05/05/2026")
        self.assertEqual(r[0]["site"], "Receita Federal")
        self.assertEqual(self.B.buscar("receita de bolo de chocolate"), [])

    def test_base_salva_e_recarregada(self):
        self.B.atualizar_agora()
        self.B._estado["paginas"] = {}
        self.B._reindexar()
        self.assertEqual(self.B.buscar("multa declaração MEI"), [])
        self.assertTrue(self.B.carregar())
        self.assertTrue(self.B.buscar("multa declaração MEI"))

    def test_pagina_que_falha_mantem_a_versao_anterior(self):
        data_boa = self.B.atualizar_agora()["atualizada_em"]
        with mock.patch.object(v, "baixar", return_value=(None, None, None)):
            st = self.B.atualizar_agora()
        self.assertTrue(self.B.buscar("multa declaração MEI"))
        self.assertEqual(st["atualizada_em"], data_boa)          # sem internet: não finge que atualizou
        self.assertTrue(st["ultimo_erro"])

    def test_conferencia_usa_base_local_sem_internet(self):
        self.B.atualizar_agora()
        self.B.aprender("https://sebrae.com.br/mei-declaracao",
                        "Declaração do MEI", ["A declaração anual do MEI (DASN-SIMEI) tem multa mínima de R$ 50,00 por atraso."])
        with mock.patch.object(v, "coletar_trechos_ao_vivo", side_effect=AssertionError("não devia ir à internet")):
            trechos = v.coletar_trechos("multa atraso declaração anual MEI")
        self.assertEqual(len(trechos), 2)
        self.assertTrue(all(t["origem"] == "base_local" for t in trechos))
        with mock.patch.object(v, "comparar", return_value={"veredito": "confirmado", "motivo": "ok"}):
            r = v.verificar(None, "m", {}, "p", "c", "r", PDFS, trechos_web=trechos)
        self.assertEqual((r["status"], r["origem"]), ("confirmado", "base_local"))

    def test_sem_nada_local_busca_ao_vivo(self):
        with mock.patch.object(v, "coletar_trechos_ao_vivo", return_value=WEB) as ao_vivo:
            self.assertEqual(v.coletar_trechos("valor do DAS"), WEB)
        ao_vivo.assert_called_once()


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


class Velocidade(unittest.TestCase):
    """Resposta aparece antes da conferência; versão corrigida substitui a do PDF."""

    @classmethod
    def setUpClass(cls):
        import rag
        cls.rag = rag

    def _cliente(self, texto, chamadas, falhar_modelo=None):
        def criar(**k):
            chamadas.append(k)
            if falhar_modelo and k["model"] == falhar_modelo:
                erro = RuntimeError(f"The model `{falhar_modelo}` has been decommissioned")
                erro.status_code = 400
                raise erro
            if k.get("stream"):
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content=texto))])])
            return Resposta("consulta reescrita sobre o DAS do MEI")
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=criar)))

    def setUp(self):
        self.rag._cache.clear()
        self.trechos = [{"fonte": "4-Perguntas&Respostas3_MEI.pdf", "pagina": 7, "texto": "O DAS custa R$ 67,00."}]
        self.indice = types.SimpleNamespace(buscar_varias=lambda consultas, k=8: list(self.trechos))
        p = mock.patch.object(self.rag, "trechos_da_web", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def test_texto_sai_antes_da_conferencia_e_e_substituido(self):
        ordem = []
        nova = {"status": "atualizada", "resposta": "Segundo o site do Sebrae, o DAS custa **R$ 82,05**.",
                "divergencias": [{"tema": "DAS", "pdf": "R$ 67,00", "site": "R$ 82,05"}], "fontes_web": [], "data": "23/11/2026"}

        def verificar(*a, **k):
            ordem.append("verificar")
            self.assertIsNotNone(k.get("trechos_web"))          # coleta já feita em paralelo
            return dict(nova)
        with mock.patch.object(self.rag.verificador, "ATIVO", True), \
             mock.patch.object(self.rag, "CONFERENCIA_MODO", "depois"), \
             mock.patch.object(self.rag.verificador, "coletar_trechos", return_value=WEB), \
             mock.patch.object(self.rag.verificador, "verificar", side_effect=verificar):
            for tipo, dado in self.rag.responder_stream(self.indice, self._cliente("O DAS custa R$ 67,00 [1].", []),
                                                         "Quanto custa o DAS do MEI?", []):
                ordem.append(tipo)
                if tipo == "fim":
                    fim = dado
        self.assertLess(ordem.index("texto"), ordem.index("verificar"))   # a pessoa já lê antes de conferir
        self.assertIn("status", ordem)
        self.assertTrue(fim["substituida"])
        self.assertIn("82,05", fim["resposta"])
        self.assertNotIn("resposta", fim["verificacao"])

    def test_modo_antes_continua_funcionando(self):
        with mock.patch.object(self.rag.verificador, "ATIVO", True), \
             mock.patch.object(self.rag, "CONFERENCIA_MODO", "antes"), \
             mock.patch.object(self.rag.verificador, "coletar_trechos", return_value=WEB), \
             mock.patch.object(self.rag.verificador, "verificar",
                               return_value={"status": "confirmado", "fontes_web": [], "divergencias": []}):
            cli = self._cliente("", [])
            cli.chat.completions.create = lambda **k: (Resposta("O DAS custa R$ 67,00 [1].") if k.get("messages", [{}])[0].get("content", "").startswith("Você é")
                                                       else Resposta("consulta"))
            eventos = list(self.rag.responder_stream(self.indice, cli, "Quanto custa o DAS?", []))
        self.assertEqual(eventos[0], ("status", "Consultando os documentos…"))
        self.assertEqual(eventos[-1][1]["verificacao"]["status"], "confirmado")
        self.assertFalse(eventos[-1][1]["substituida"])

    def test_reescrita_usa_modelo_rapido_e_cai_para_o_principal(self):
        chamadas = []
        with mock.patch.object(self.rag.verificador, "ATIVO", False), \
             mock.patch.object(self.rag, "_modelo_reescrita", "modelo-rapido"):
            list(self.rag.responder_stream(self.indice, self._cliente("ok [1].", chamadas, falhar_modelo="modelo-rapido"),
                                           "Quanto custa o DAS?", []))
            self.assertEqual(self.rag._modelo_reescrita, self.rag.MODELO_LLM)      # lembra a troca
        modelos = [c["model"] for c in chamadas]
        self.assertEqual(modelos, ["modelo-rapido", self.rag.MODELO_LLM, self.rag.MODELO_LLM])

    def test_paginas_oficiais_entram_como_fonte(self):
        web = [{"fonte": "web", "pagina": 0, "url": "https://www.gov.br/receitafederal/pt-br/x", "titulo": "DAS 2026",
                "site": "Receita Federal", "data": "02/02/2026", "tipo": "pagina", "texto": "O DAS de 2026 é R$ 82,05."}]
        chamadas = []
        with mock.patch.object(self.rag.verificador, "ATIVO", False), \
             mock.patch.object(self.rag, "trechos_da_web", return_value=web):
            fim = list(self.rag.responder_stream(self.indice, self._cliente("Valor novo [2], antigo [1].", chamadas),
                                                 "Quanto custa o DAS?", []))[-1][1]
        self.assertEqual(fim["paginas_oficiais"][0]["url"], "https://www.gov.br/receitafederal/pt-br/x")
        self.assertEqual(fim["fontes"][0]["arquivo"], "4-Perguntas&Respostas3_MEI.pdf")
        self.assertIn("página oficial — site Receita Federal: DAS 2026, publicada/atualizada em 02/02/2026",
                      chamadas[-1]["messages"][-1]["content"])


class BuscaOficialPorAssunto(unittest.TestCase):
    """Conferência de perguntas básicas ("Quem pode ser MEI?"): a página certa precisa ser escolhida."""

    PAGINAS = {
        "https://blog.rn.sebrae.com.br/?s=mei": ["Resultados da busca por MEI: Guia MEI, Natal para o MEI, "
                                                 "Site para MEI, Impostos do MEI, MEI e CNPJ, MEI 2026"],
        "https://sebrae.com.br/empreendedores/guiaparaomei/dasn": ["Declaração anual de faturamento do MEI (DASN): "
                                                                  "o MEI informa o faturamento do ano anterior."],
        "https://blog.rn.sebrae.com.br/impostos-mei/": ["Auxílio-doença: o MEI pode solicitar o auxílio-doença após "
                                                       "12 contribuições. O MEI tem salário-maternidade."],
        "https://blog.rn.sebrae.com.br/como-abrir-mei-2026-passo-a-passo/": [
            "Requisitos para ser MEI em 2026: não ser sócio, administrador ou titular de outra empresa; exercer "
            "atividade permitida; respeitar o limite de faturamento de R$ 81 mil; ter no máximo um empregado."],
    }

    def setUp(self):
        import base_web as B
        self.B = B
        antigo = dict(B._estado["paginas"])
        B._estado["paginas"] = {u: {"titulo": u.rsplit("/", 2)[-2], "site": v.nome_do_site(u), "data": "",
                                    "tipo": "pagina", "passagens": t} for u, t in self.PAGINAS.items()}
        B._reindexar()
        self.addCleanup(lambda: (B._estado.update(paginas=antigo), B._reindexar()))

    def test_quem_pode_ser_mei_vira_busca_por_requisitos(self):
        rica, destaques = v.expandir_consulta("Quem pode ser MEI?")
        self.assertIn("requisitos", rica)
        self.assertTrue(destaques)
        self.assertEqual(self.B._tokens("Quem pode ser MEI?"), ["mei"])   # o problema: sobrava só "mei"

    def test_pagina_dos_requisitos_vem_primeiro_e_listagem_fica_de_fora(self):
        with mock.patch.object(v, "coletar_trechos_ao_vivo", side_effect=AssertionError("não devia ir à internet")):
            trechos = v.coletar_trechos("Quem pode ser MEI?", "Quem pode ser MEI?")
        urls = [t["url"] for t in trechos]
        self.assertEqual(urls[0], "https://blog.rn.sebrae.com.br/como-abrir-mei-2026-passo-a-passo/")
        self.assertTrue(trechos[0]["destaque"])
        self.assertNotIn("https://blog.rn.sebrae.com.br/?s=mei", urls)

    def test_sem_pagina_sobre_o_assunto_le_as_oficiais_ao_vivo(self):
        self.B._estado["paginas"] = {u: p for u, p in self.B._estado["paginas"].items() if "como-abrir" not in u}
        self.B._reindexar()
        ao_vivo = [{"url": "https://blog.rn.sebrae.com.br/guia-mei/", "titulo": "Guia MEI", "site": "Sebrae",
                    "texto": "Quem pode ser MEI: ...", "destaque": True}]
        with mock.patch.object(v, "coletar_trechos_ao_vivo", return_value=ao_vivo) as vivo:
            trechos = v.coletar_trechos("quem pode ser mei", "Quem pode ser MEI?")
        vivo.assert_called_once()
        self.assertEqual(vivo.call_args.kwargs["busca"], "Quem pode ser MEI?")   # buscador recebe a pergunta curta
        self.assertEqual(trechos[0]["url"], "https://blog.rn.sebrae.com.br/guia-mei/")

    def test_destaques_escolhem_o_paragrafo_certo_ao_vivo(self):
        linhas = ["O MEI tem muitas vantagens e o MEI cresce no Brasil, MEI MEI.",
                  "Quem pode ser MEI: não ser sócio de outra empresa e exercer atividade permitida."]
        rica, destaques = v.expandir_consulta("Quem pode ser MEI?")
        texto = v.selecionar_trechos(linhas, rica, limite=90, destaques=destaques)
        self.assertIn("não ser sócio", texto)

    def test_paginas_de_listagem(self):
        for u in ("https://blog.rn.sebrae.com.br/?s=mei", "https://blog.rn.sebrae.com.br/guia-mei/feed/",
                  "https://blog.rn.sebrae.com.br/tag/mei/", "https://blog.rn.sebrae.com.br/category/mei/page/2"):
            self.assertTrue(v.pagina_de_listagem(u), u)
        self.assertFalse(v.pagina_de_listagem("https://blog.rn.sebrae.com.br/como-abrir-mei-2026-passo-a-passo/"))

    def test_sementes_cobrem_perguntas_basicas(self):
        for p in ("quem pode ser mei", "atividades permitidas", "nota fiscal", "aposentadoria", "empregado"):
            self.assertTrue(v.urls_das_sementes(p), p)


class RegrasFiscais(unittest.TestCase):
    """Casos do relatório de testes (NAF / FiscOn): contas feitas pelo código, não pelo modelo."""

    @classmethod
    def setUpClass(cls):
        import regras_fiscais as R
        cls.R = R

    def fato(self, pergunta, tema, historico=None):
        fatos = [f for f in self.R.fatos_para(pergunta, historico) if f["tema"] == tema]
        self.assertTrue(fatos, f"nenhum fato '{tema}' para: {pergunta}")
        return fatos[0]

    # ------------------------------------------------------------------ IRPF
    def test_irpf_limites_abaixo_igual_acima(self):
        R = self.R
        self.assertFalse(R.obrigatoriedade_irpf(2025, R._d("35583.99"))["obrigado"])
        igual = R.obrigatoriedade_irpf(2025, R._d("35584.00"))
        self.assertFalse(igual["obrigado"])                    # a lei diz "ACIMA de": o valor exato não obriga
        self.assertTrue(igual["igual_ao_limite"])
        self.assertTrue(R.obrigatoriedade_irpf(2025, R._d("35584.01"))["obrigado"])
        self.assertEqual(igual["prazo"], "29/05/2026")
        self.assertEqual(igual["exercicio"], 2026)

    def test_irpf_34_mil_caso_do_relatorio(self):
        f = self.fato("Recebi R$ 34 mil em salários tributáveis em 2025, sem outros critérios. "
                      "Preciso entregar o IRPF 2026? Qual foi o prazo?", "irpf")
        self.assertFalse(f["conclusao"]["valor"])
        self.assertIn("dispensado", f["texto"])
        self.assertIn("R$ 35.584,00", f["texto"])
        self.assertIn("29/05/2026", f["texto"])

    def test_irpf_valores_no_limite_pelo_texto(self):
        for texto, obrigado in (("R$ 35.583,99", False), ("R$ 35.584,00", False), ("R$ 35.584,01", True),
                                ("36 mil", True)):
            f = self.fato(f"Recebi {texto} de salário em 2025. Preciso declarar o imposto de renda 2026?", "irpf")
            self.assertEqual(f["conclusao"]["valor"], obrigado, texto)

    # ------------------------------------------------------- MEI: limite e excesso
    def test_limite_proporcional_e_20_por_cento(self):
        R = self.R
        r = R.avaliar_excesso(R._d("50000"), 2026, 7, 2026)
        self.assertEqual(r["limite"]["valor"], R._d("40500"))
        self.assertEqual(r["excesso"], R._d("9500"))
        self.assertEqual(R.pct(r["percentual"]), "23,46%")
        self.assertTrue(r["acima_tolerancia"])
        self.assertEqual(r["efeito"], "07/2026")            # retroativo ao início da atividade
        exato = R.avaliar_excesso(R._d("48600"), 2026, 7, 2026)   # excesso de exatamente 20%
        self.assertFalse(exato["acima_tolerancia"])
        self.assertEqual(exato["efeito"], "01/01/2027")
        acima = R.avaliar_excesso(R._d("48600.01"), 2026, 7, 2026)
        self.assertTrue(acima["acima_tolerancia"])
        self.assertEqual(acima["efeito"], "07/2026")
        self.assertFalse(R.avaliar_excesso(R._d("40500"), 2026, 7, 2026)["houve_excesso"])
        # fora do ano de abertura: teto anual e retroatividade a 1º de janeiro
        comum = R.avaliar_excesso(R._d("100000"), 2026, 7, 2025)
        self.assertEqual(comum["limite"]["valor"], R._d("81000"))
        self.assertEqual(comum["efeito"], "01/01/2026")

    def test_caso_decisivo_uma_mensagem_e_duas_mensagens(self):
        unica = self.fato("Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?", "excesso")
        historico = [{"papel": "user", "texto": "Abri o MEI em julho de 2026."},
                     {"papel": "assistant", "texto": "Certo! O teto anual do MEI é de R$ 81.000,00."}]
        duas = self.fato("Faturei R$ 50 mil até dezembro. Houve excesso?", "excesso", historico)
        self.assertEqual(unica["texto"], duas["texto"])
        for f in (unica, duas):
            self.assertIn("R$ 40.500,00", f["texto"])
            self.assertIn("R$ 9.500,00", f["texto"])
            self.assertIn("23,46%", f["texto"])
            self.assertIn("julho de 2026", f["texto"])
            self.assertNotIn("2027", f["texto"])
        # continuação sem números ("e quando começa?") herda tema e dados da pergunta anterior
        historico = [{"papel": "user", "texto": "Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. "
                                                "Houve excesso?"},
                     {"papel": "assistant", "texto": "Sim, houve excesso."}]
        cont = self.fato("E quando começa o desenquadramento?", "excesso", historico)
        self.assertEqual(cont["texto"], unica["texto"])

    def test_sequencia_do_teste_do_naf(self):
        # 1) pergunta completa, 2) limite perguntado à parte, 3) continuação — o NAF voltou ao teto de 81 mil aqui
        p1 = "Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?"
        p2 = "Qual o limite proporcional para quem abriu em julho de 2026?"
        # com o faturamento já informado, o fato do limite vem junto do cálculo do excesso
        fatos = self.R.fatos_para(p2, [{"papel": "user", "texto": p1}])
        self.assertTrue(fatos)
        self.assertTrue(all("R$ 40.500,00" in f["texto"] and "81.000" not in f["resumo"] for f in fatos))
        sozinho = self.fato("Qual o limite do MEI para quem abriu em julho de 2026?", "limite_abertura")
        self.assertIn("R$ 40.500,00", sozinho["texto"])
        self.assertEqual(self.R.garantir("O limite do MEI é de R$ 81 mil por ano.", [sozinho])[1], "corrigida")
        historico = [{"papel": "user", "texto": p1}, {"papel": "assistant", "texto": "Não encontrei a informação."},
                     {"papel": "user", "texto": p2}, {"papel": "assistant", "texto": "O limite é R$ 40.500,00."}]
        cont = self.fato("Então houve excesso?", "excesso", historico)
        self.assertEqual(cont["texto"], self.fato(p1, "excesso")["texto"])
        self.assertEqual(self.R.garantir("Não houve excesso, pois R$ 50 mil está abaixo do teto anual de R$ 81 mil.",
                                         [cont])[1], "corrigida")
        self.assertEqual([f["tema"] for f in self.R.fatos_para("Qual o limite do MEI em 2026?")], ["teto"])

    def test_numero_do_teto_nao_vira_faturamento(self):
        historico = [{"papel": "user", "texto": "Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro."}]
        sit = self.R.extrair_situacao("Então não houve excesso, já que o teto do MEI é de 81 mil?", historico)
        self.assertEqual(sit["faturamento"], self.R._d("50000"))

    # ---------------------------------------------------------------- DAS
    def test_das_2026_por_atividade(self):
        v = self.R.valores_das(2026)
        self.assertEqual(v["inss"], self.R._d("81.05"))
        self.assertEqual((v["comercio"], v["servicos"], v["misto"]),
                         (self.R._d("82.05"), self.R._d("86.05"), self.R._d("87.05")))
        f = self.fato("Quanto paga de DAS em 2026 um MEI comum que presta exclusivamente serviços?", "das_valor")
        self.assertIn("R$ 86,05", f["texto"])
        self.assertIn("R$ 5,00 de ISS", f["texto"])
        self.assertNotIn("82,05", f["texto"])

    def test_sem_faturamento_das_e_dasn(self):
        f = self.fato("O MEI ficou ativo em 2025, sem faturamento. Precisa pagar DAS e entregar a declaração anual?",
                      "sem_faturamento")
        self.assertIn("DAS mensal: é devido", f["texto"])
        self.assertIn("31/05/2026", f["texto"])
        self.assertIn("zero", f["texto"])

    def test_teto_2026_e_2027_lc_238(self):
        f = self.fato("Qual era o teto do MEI em 2026 e qual estava previsto para 2027 pela LC nº 238/2026?", "teto")
        self.assertIn("R$ 81.000,00", f["texto"])
        self.assertIn("R$ 110.000,00", f["texto"])
        self.assertIn("orçamentária", f["texto"])

    def test_dirf(self):
        f = self.fato("Ainda é preciso entregar DIRF em 2026 sobre pagamentos e retenções de 2025?", "dirf")
        self.assertTrue(f["texto"].startswith("**Não.**"))
        self.assertIn("eSocial", f["texto"])
        self.assertIn("EFD-Reinf", f["texto"])

    def test_resposta_dos_fatos_e_coerente(self):
        for p in ("Recebi R$ 34 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?",
                  "Recebi R$ 40 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?",
                  "Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?",
                  "Um MEI abriu em julho de 2026 e faturou R$ 45 mil até dezembro. Houve excesso?",
                  "Um MEI abriu em julho de 2026 e faturou R$ 30 mil até dezembro. Houve excesso?",
                  "Quanto custa o DAS do MEI?", "Quanto paga de DAS em 2026 um MEI de serviços?",
                  "O MEI ficou ativo em 2025, sem faturamento. Precisa pagar DAS?",
                  "Qual o teto do MEI em 2026 e 2027?", "Ainda existe DIRF em 2026?"):
            fatos = self.R.fatos_para(p)
            self.assertTrue(fatos, p)
            final, ajuste, motivos = self.R.garantir(self.R.resposta_dos_fatos(fatos), fatos)
            self.assertIsNone(ajuste, f"{p}: {motivos}")

    # ------------------------------------- respostas erradas do relatório são barradas
    def test_respostas_erradas_do_relatorio_sao_corrigidas(self):
        R = self.R
        casos = [
            ("Recebi R$ 34 mil em salários tributáveis em 2025, sem outros critérios. Preciso entregar o IRPF 2026?",
             [], "Sim, você precisa declarar, pois R$ 34 mil supera o limite de obrigatoriedade. O prazo foi 29/05/2026."),
            ("Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Qual foi o excesso e quando começa o "
             "desenquadramento?", [],
             "Houve excesso de R$ 9.500 (cerca de 23,5%) sobre o limite proporcional de R$ 40.500. O desenquadramento "
             "produz efeitos a partir de 01/01/2027."),
            ("E então, houve excesso?", [{"papel": "user", "texto": "Um MEI abriu em julho de 2026 e faturou R$ 50 mil "
                                                                    "até dezembro."}],
             "Não houve excesso, pois R$ 50 mil está abaixo do teto anual de R$ 81 mil."),
            ("Quanto paga de DAS em 2026 um MEI comum que presta exclusivamente serviços?", [],
             "O DAS é 5% do salário mínimo mais R$ 6 de ISS."),
            ("Quanto paga de DAS em 2026 um MEI comum que presta exclusivamente serviços?", [],
             "O valor é 5% do salário mínimo de INSS. O ISS depende da alíquota municipal, geralmente entre 2% e 5%."),
            ("Qual era o teto do MEI em 2026 e qual estava previsto para 2027 pela LC nº 238/2026?", [],
             "Não encontrei essa informação nos documentos. Procure o NAF."),
        ]
        for pergunta, historico, errada in casos:
            fatos = R.fatos_para(pergunta, historico)
            final, ajuste, motivos = R.garantir(errada, fatos)
            self.assertEqual(ajuste, "corrigida", f"{pergunta} -> {motivos}")
            self.assertFalse(R.contradiz(final, fatos))

    def test_resposta_incompleta_e_complementada(self):
        R = self.R
        fatos = R.fatos_para("O MEI ficou ativo em 2025, sem faturamento. Precisa pagar DAS e entregar a declaração anual?")
        final, ajuste, _ = R.garantir("Sim, você deve entregar a DASN-SIMEI informando receita zero até 31/05/2026.", fatos)
        self.assertEqual(ajuste, "complementada")          # faltou dizer que o DAS continua devido
        self.assertIn("Conferência automática", final)
        self.assertIn("DAS mensal continua devido", final)

    def test_resposta_correta_fica_como_esta(self):
        R = self.R
        fatos = R.fatos_para("Recebi R$ 34 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?")
        certa = ("**Não, você está dispensado de entregar a declaração do IRPF 2026** por esse critério: só é obrigado "
                 "quem recebeu mais de R$ 35.584,00 em rendimentos tributáveis em 2025. O prazo foi 29 de maio de 2026.")
        self.assertEqual(R.garantir(certa, fatos), (certa, None, []))
        fatos = R.fatos_para("Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?")
        certa = ("Sim, houve excesso de R$ 9.500,00 (23,46%) sobre o limite proporcional de R$ 40.500,00. Como passou "
                 "de 20%, o desenquadramento é retroativo ao início da atividade, em julho de 2026. Se o excesso fosse "
                 "de até 20%, os efeitos seriam a partir de 01/01/2027.")       # hipótese não é contradição
        self.assertIsNone(R.garantir(certa, fatos)[1])

    def test_valores_monetarios_ignoram_anos_datas_e_percentuais(self):
        valores = [v for _, v in self.R._valores_monetarios("Em 2026, até 31/05/2026, 20% de R$ 1.621,00 e 50k e 34 mil")]
        self.assertEqual(valores, [self.R._d("1621.00"), self.R._d("50000"), self.R._d("34000")])


class RespostasComRegras(unittest.TestCase):
    """rag.py + verificador.py usando os fatos calculados (sem Groq e sem internet)."""

    @classmethod
    def setUpClass(cls):
        import rag
        cls.rag = rag

    def setUp(self):
        self.rag._cache.clear()
        self.indice = types.SimpleNamespace(buscar_varias=lambda consultas, k=8: [
            {"fonte": "5-Impostos_PF_MEI.pdf", "pagina": 3, "texto": "Está obrigada a declarar quem recebeu..."}])
        p = mock.patch.object(self.rag, "trechos_da_web", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _cliente(self, texto, chamadas):
        def criar(**k):
            chamadas.append(k)
            if k.get("stream"):
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content=texto))])])
            return Resposta("consulta reescrita")
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=criar)))

    def test_fatos_vao_no_prompt_e_resposta_errada_e_trocada(self):
        chamadas = []
        with mock.patch.object(self.rag.verificador, "ATIVO", False):
            fim = list(self.rag.responder_stream(
                self.indice, self._cliente("Sim, você precisa declarar: R$ 34 mil supera o limite [1].", chamadas),
                "Recebi R$ 34 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?", []))[-1][1]
        prompt = chamadas[-1]["messages"][-1]["content"]
        self.assertIn("FATOS CALCULADOS", prompt)
        self.assertLess(prompt.index("FATOS CALCULADOS"), prompt.index("Trechos recuperados"))
        self.assertEqual(fim["ajuste_regras"], "corrigida")
        self.assertTrue(fim["substituida"])
        self.assertIn("dispensado", fim["resposta"])
        self.assertEqual(fim["fatos"][0]["tema"], "irpf")
        self.assertTrue(fim["fatos"][0]["fonte"].startswith("https://www.gov.br/"))

    def test_contexto_de_duas_mensagens_chega_na_reescrita_e_no_prompt(self):
        chamadas = []
        historico = [{"papel": "user", "texto": "Abri o MEI em julho de 2026."},
                     {"papel": "assistant", "texto": "Ótimo! Como posso ajudar?"}]
        with mock.patch.object(self.rag.verificador, "ATIVO", False):
            fim = list(self.rag.responder_stream(
                self.indice, self._cliente("Sim, houve excesso de R$ 9.500,00 (23,46%) sobre R$ 40.500,00; os efeitos "
                                           "são retroativos a julho de 2026.", chamadas),
                "Faturei R$ 50 mil até dezembro. Houve excesso?", historico))[-1][1]
        reescrita = chamadas[0]["messages"][-1]["content"]
        self.assertIn("MEI aberto em 07/2026", reescrita)
        self.assertIn("R$ 40.500,00", chamadas[-1]["messages"][-1]["content"])
        self.assertIsNone(fim["ajuste_regras"])           # resposta já correta: não mexe
        self.assertFalse(fim["substituida"])

    def test_sem_regra_aplicavel_nada_muda(self):
        chamadas = []
        with mock.patch.object(self.rag.verificador, "ATIVO", False):
            fim = list(self.rag.responder_stream(self.indice, self._cliente("Sim, o MEI pode ter um empregado [1].",
                                                                            chamadas),
                                                 "O MEI pode contratar um funcionário?", []))[-1][1]
        self.assertEqual(fim["fatos"], [])
        self.assertIsNone(fim["ajuste_regras"])
        self.assertNotIn("FATOS CALCULADOS", chamadas[-1]["messages"][-1]["content"])

    def test_verificador_recusa_atualizacao_que_contradiz_os_fatos(self):
        import regras_fiscais as R
        fatos = R.fatos_para("Recebi R$ 34 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?")
        web = [{"url": "https://www.gov.br/receitafederal/pt-br/x", "titulo": "IRPF 2026", "site": "Receita Federal",
                "texto": "Deve declarar quem recebeu rendimentos tributáveis acima de R$ 35.584,00."}]
        errada = json.dumps({"veredito": "desatualizado", "motivo": "x", "divergencias": [],
                             "resposta": "Sim, você precisa declarar, pois R$ 34 mil supera o limite [W1]."})
        r = v.verificar(ClienteFalso(errada), "m", {}, "p", "c", "rascunho", PDFS, trechos_web=web, fatos=fatos)
        self.assertEqual(r["status"], "inconclusivo")
        self.assertNotIn("resposta", r)
        # sem fatos, o mesmo veredito continua sendo aceito (comportamento anterior)
        r = v.verificar(ClienteFalso(errada), "m", {}, "p", "c", "rascunho", PDFS, trechos_web=web)
        self.assertEqual(r["status"], "atualizada")


class Voz(unittest.TestCase):
    """Transcrição de voz (voz.py e /api/voz/transcrever), sem Groq de verdade."""

    AUDIO = b"\x1aE\xdf\xa3" + b"0" * 4000          # começo de um webm + conteúdo qualquer

    def _cliente(self, texto="Quanto custa o DAS do MEI?", falhar_modelo=None, chamadas=None):
        def criar(**k):
            if chamadas is not None:
                chamadas.append(k)
            if falhar_modelo and k["model"] == falhar_modelo:
                erro = RuntimeError(f"model {falhar_modelo} not found")
                erro.status_code = 404
                raise erro
            return types.SimpleNamespace(text=texto)
        return types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=criar)))

    def setUp(self):
        import voz
        self.voz = voz
        voz._modelo_atual = voz.MODELO_TRANSCRICAO

    def test_transcreve_em_portugues_com_vocabulario(self):
        chamadas = []
        texto = self.voz.transcrever(self._cliente(chamadas=chamadas), self.AUDIO, "x", "audio/webm;codecs=opus")
        self.assertEqual(texto, "Quanto custa o DAS do MEI?")
        self.assertEqual(chamadas[0]["language"], "pt")
        self.assertIn("DASN-SIMEI", chamadas[0]["prompt"])
        self.assertEqual(chamadas[0]["file"][0], "pergunta.webm")

    def test_modelo_reserva_quando_o_principal_some(self):
        chamadas = []
        self.voz.transcrever(self._cliente(falhar_modelo=self.voz.MODELO_TRANSCRICAO, chamadas=chamadas), self.AUDIO, "x", "audio/ogg")
        self.assertEqual([c["model"] for c in chamadas], [self.voz.MODELO_TRANSCRICAO, self.voz.MODELO_RESERVA])

    def test_audio_invalido(self):
        for conteudo, tipo in ((b"", "audio/webm"), (b"x" * 100, "audio/webm"), (self.AUDIO, "image/png"),
                               (b"x" * (self.voz.MAX_BYTES_AUDIO + 1), "audio/webm")):
            with self.assertRaises(self.voz.ErroVoz):
                self.voz.transcrever(self._cliente(), conteudo, "x", tipo)

    def test_frases_inventadas_no_silencio_sao_descartadas(self):
        for frase in ("Legendas pela comunidade Amara.org", "Obrigado por assistir!", "  "):
            self.assertEqual(self.voz.limpar_transcricao(frase), "", frase)
        self.assertEqual(self.voz.limpar_transcricao(" Obrigado pela ajuda "), "Obrigado pela ajuda")

    def test_rota_da_api(self):
        import io
        import os
        import app as A
        cliente_http = A.app.test_client()
        antes = dict(A.estado)
        self.addCleanup(lambda: A.estado.update(antes))
        A.estado.update(pid=os.getpid(), pronto=True, erro=None, llm=self._cliente("Como emitir o DAS?"))
        A._acessos.clear()
        r = cliente_http.post("/api/voz/transcrever", data={"audio": (io.BytesIO(self.AUDIO), "a.webm", "audio/webm")},
                              content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.get_json()), (200, {"texto": "Como emitir o DAS?"}))
        r = cliente_http.post("/api/voz/transcrever", data={}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        A.estado["llm"] = self._cliente("Legendas pela comunidade Amara.org")
        r = cliente_http.post("/api/voz/transcrever", data={"audio": (io.BytesIO(self.AUDIO), "a.webm", "audio/webm")},
                              content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        A.estado["pronto"] = False
        r = cliente_http.post("/api/voz/transcrever", data={"audio": (io.BytesIO(self.AUDIO), "a.webm", "audio/webm")},
                              content_type="multipart/form-data")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
