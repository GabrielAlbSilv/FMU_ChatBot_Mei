"""pesquisa.py e a escolha das páginas oficiais por assunto."""
from tests.apoio import *  # noqa: F401,F403


class Pesquisa(unittest.TestCase):
    """Pesquisa geral (pesquisa.py): qualquer pergunta acha a página oficial do assunto, sem lista de temas."""

    SITEMAP = """<?xml version="1.0"?><urlset>
      <url><loc>https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/como-funciona-o-mei-caminhoneiro</loc>
           <lastmod>2026-05-01</lastmod></url>
      <url><loc>https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/atividades-permitidas-no-mei</loc></url>
      <url><loc>https://blog.rn.sebrae.com.br/qual-a-diferenca-entre-agricultor-e-produtor-rural/</loc></url>
      <url><loc>https://blog.rn.sebrae.com.br/diferenca-mei-e-me/</loc></url>
      <url><loc>https://blog.rn.sebrae.com.br/tag/mei/</loc></url>
      <url><loc>https://malicioso.com/mei-caminhoneiro</loc></url>
    </urlset>"""
    INDICE = """<?xml version="1.0"?><sitemapindex>
      <sitemap><loc>https://blog.rn.sebrae.com.br/post-sitemap1.xml</loc></sitemap>
      <sitemap><loc>https://blog.rn.sebrae.com.br/category-sitemap.xml</loc></sitemap></sitemapindex>"""

    def setUp(self):
        import base_web as B
        import pesquisa as P
        self.P, self.B = P, B
        self.respostas = {}
        self.baixados = []

        def falso_baixar(url, aceitar=None, max_bytes=None, extra=None, timeout=None):
            self.baixados.append(url)
            corpo = self.respostas.get(url)
            if corpo is None:
                return None, None, None
            return 200, corpo.encode() if isinstance(corpo, str) else corpo, {"Content-Type": "text/html"}

        antigo_cat = dict(P._catalogo)
        antigo_pag = dict(B._estado["paginas"])
        for p in (mock.patch.object(P, "ATIVA", True), mock.patch.object(v, "baixar", side_effect=falso_baixar)):
            p.start()
            self.addCleanup(p.stop)
        B._estado["paginas"] = {}
        B._reindexar()
        P._catalogo.update(itens={}, idf={}, tokens={})
        self.addCleanup(lambda: (P._catalogo.update(antigo_cat), B._estado.update(paginas=antigo_pag), B._reindexar()))

    def catalogo(self, urls):
        self.P.adicionar_ao_catalogo(urls)

    def test_sitemap_so_endereco_oficial_e_sem_listagem(self):
        self.respostas["https://sebrae.com.br/sitemap.xml"] = self.SITEMAP
        urls = [u for u, _ in self.P.ler_sitemap("https://sebrae.com.br/sitemap.xml")]
        self.assertIn("https://blog.rn.sebrae.com.br/diferenca-mei-e-me/", urls)
        self.assertNotIn("https://malicioso.com/mei-caminhoneiro", urls)          # fora da lista de sites
        self.assertNotIn("https://blog.rn.sebrae.com.br/tag/mei/", urls)           # página de listagem
        self.assertEqual(dict(self.P.ler_sitemap("https://sebrae.com.br/sitemap.xml"))[
            "https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/como-funciona-o-mei-caminhoneiro"], "2026-05-01")

    def test_indice_de_sitemaps_segue_so_o_padrao(self):
        self.respostas["https://blog.rn.sebrae.com.br/wp-sitemap.xml"] = self.INDICE
        self.respostas["https://blog.rn.sebrae.com.br/post-sitemap1.xml"] = self.SITEMAP
        urls = self.P.ler_sitemap("https://blog.rn.sebrae.com.br/wp-sitemap.xml", seguir=r"post-sitemap")
        self.assertTrue(urls)
        self.assertNotIn("https://blog.rn.sebrae.com.br/category-sitemap.xml", self.baixados)

    def test_links_de_menu_entram_com_o_texto_do_link(self):
        self.respostas["https://www.gov.br/receitafederal/pt-br/assuntos"] = (
            '<a href="/receitafederal/pt-br/assuntos/orientacao-tributaria/cpf"><span>CPF</span></a>'
            '<a href="https://malicioso.com/x">x</a><a href="/receitafederal/pt-br/search?SearchableText=a">busca</a>')
        links = self.P.links_com_titulo("https://www.gov.br/receitafederal/pt-br/assuntos")
        self.assertEqual(links, [("https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cpf", "CPF")])

    def test_busca_interna_do_blog(self):
        self.respostas["https://blog.rn.sebrae.com.br/wp-json/wp/v2/search?per_page=6&search=MEI+pode+ter+dois+CNPJ"] = \
            json.dumps([{"url": "https://blog.rn.sebrae.com.br/mei-para-ltda/"}, {"url": "https://malicioso.com/y"}])
        with mock.patch.object(self.P, "_config", return_value=([], self.P.BUSCAS_PADRAO)):
            self.assertEqual(self.P.buscar_nos_sites("MEI pode ter dois CNPJ?"), ["https://blog.rn.sebrae.com.br/mei-para-ltda/"])

    def test_termos_da_pergunta_sem_palavras_vazias(self):
        termos = self.P.termos_da_pergunta("Qual a diferença entre MEI e ME?")
        self.assertIn("mei", termos)
        self.assertIn("diferen", termos)
        self.assertNotIn("entre", termos)
        self.assertNotIn("doi", self.P.termos_da_pergunta("MEI pode ter dois CNPJ?"))

    def test_titulo_forte_exige_o_sujeito_da_pergunta(self):
        self.catalogo(["https://blog.rn.sebrae.com.br/qual-a-diferenca-entre-agricultor-e-produtor-rural/",
                       "https://blog.rn.sebrae.com.br/diferenca-mei-e-me/",
                       "https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/como-funciona-o-mei-caminhoneiro",
                       ("https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cpf", "CPF")])
        P = self.P
        self.assertEqual(P.titulos_fortes(P.termos_da_pergunta("Qual a diferença entre MEI e ME?")),
                         ["https://blog.rn.sebrae.com.br/diferenca-mei-e-me/"])
        self.assertEqual(P.titulos_fortes(P.termos_da_pergunta("Como funciona o MEI caminhoneiro?")),
                         ["https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/como-funciona-o-mei-caminhoneiro"])
        self.assertIn("https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cpf",
                      P.titulos_fortes(P.termos_da_pergunta("Como regularizar CPF suspenso?")))
        self.assertEqual(P.titulos_fortes(P.termos_da_pergunta("Quem pode ser MEI?")), [])   # só o sujeito

    def test_reforco_le_a_pagina_do_assunto_e_tira_as_genericas(self):
        pagina = "https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/como-funciona-o-mei-caminhoneiro"
        self.catalogo([pagina])
        genericas = [{"url": f"https://blog.rn.sebrae.com.br/generica-{i}/", "titulo": "Guia", "site": "Sebrae",
                      "texto": "O MEI paga o DAS todo mês e pode ter um empregado. O MEI emite nota fiscal."}
                     for i in range(3)]
        lida = {"url": pagina, "titulo": "Como funciona o MEI Caminhoneiro", "site": "Sebrae",
                "texto": "O MEI caminhoneiro (transportador autônomo de cargas) pode faturar até R$ 251.600 por ano e "
                         "contribui com 12% do salário mínimo para o INSS."}
        with mock.patch.object(v, "obter_trecho", side_effect=lambda u, *a, **k: dict(lida) if u == pagina else None):
            final = self.P.reforcar(genericas, "Como funciona o MEI caminhoneiro?", "",
                                    "O MEI caminhoneiro pode faturar até R$ 251.600 e paga 12% de INSS.")
        self.assertEqual(final[0]["url"], pagina)
        self.assertTrue(final[0]["destaque"])
        self.assertEqual(len(final), 1)                  # as genéricas não vão para o verificador

    def test_sem_pagina_sobre_o_assunto_mantem_o_que_havia(self):
        genericas = [{"url": "https://blog.rn.sebrae.com.br/a/", "titulo": "A", "site": "Sebrae", "texto": "Texto qualquer."}]
        with mock.patch.object(v, "obter_trecho", return_value=None):
            final = self.P.reforcar(genericas, "Como funciona o MEI caminhoneiro?", "", "Resposta.")
        self.assertEqual([t["url"] for t in final], ["https://blog.rn.sebrae.com.br/a/"])
        self.assertFalse(final[0]["destaque"])

    def test_portal_do_sebrae_le_o_artigo_no_json(self):
        url = "https://sebrae.com.br/empreendedores/guiaparaomei/querosermei/atividades-permitidas-no-mei"
        corpo = ("Um dos requisitos básicos para se tornar um Microempreendedor Individual é que a sua ocupação "
                 "profissional esteja na lista de atividades permitidas.")
        self.respostas[url + ".model.json"] = json.dumps({":items": {"root": {":items": {
            "contentfragmentv2": {"text": f"<p>{corpo}</p>", "textParagraphs": [corpo]},
            "botao": {"iconSvg": "<svg>" + "x" * 200 + "</svg>"}}}}})
        titulo, linhas = v.completar_portal_sebrae(url, ("Atividades permitidas no MEI", ["Resumo curto."]))
        self.assertIn(corpo, linhas)
        self.assertEqual(sum("<svg" in l for l in linhas), 0)
        # página com texto suficiente não precisa do JSON
        longo = ("Atividades", ["x" * 900])
        self.assertEqual(v.completar_portal_sebrae(url, longo), longo)
        # fora do portal do Sebrae, não tenta
        self.assertEqual(v.completar_portal_sebrae("https://blog.rn.sebrae.com.br/a/", ("t", ["curto"])), ("t", ["curto"]))

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
