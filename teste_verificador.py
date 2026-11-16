"""Testes offline do verificador (não usam internet nem a Groq).  Rode: python teste_verificador.py"""
import json
import types
import unittest
import urllib.error
from unittest import mock

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
        self.assertEqual(self.rodar("não é json")["status"], "inconclusivo")

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
