"""base_web.py: coleta periódica dos sites oficiais (sem internet)."""
from tests.apoio import *  # noqa: F401,F403


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


class BaseWebConcorrencia(unittest.TestCase):
    """Uma coleta por vez e reindexação adiada das páginas aprendidas."""

    def setUp(self):
        import base_web as B
        self.B = B
        antigo = dict(B._estado["paginas"])
        B._estado["paginas"] = {}
        B._reindexar()
        self.addCleanup(lambda: (B._estado.update(paginas=antigo), B._reindexar()))

    def test_paginas_aprendidas_viram_uma_reindexacao(self):
        B = self.B
        with mock.patch.object(B, "ESPERA_REINDEXAR_S", 30), mock.patch.object(B, "_reindexar",
                                                                               wraps=B._reindexar) as reindexar:
            for i in range(3):
                B.aprender(f"https://sebrae.com.br/mei-pagina-{i}", f"MEI {i}",
                           [f"O MEI precisa entregar a declaração anual DASN-SIMEI todo ano, página número {i}."])
            self.assertEqual(reindexar.call_count, 0)            # nada refeito ainda (está agendado)
            self.assertTrue(B._reindexacao["pendente"])
            achados = B.buscar("declaração anual DASN MEI")      # quem busca antes do prazo já encontra
            self.assertEqual(reindexar.call_count, 1)
        self.assertEqual(len(achados), 3)
        self.assertFalse(B._reindexacao["pendente"])
        B._reindexacao["timer"].cancel()

    def test_so_uma_coleta_por_vez(self):
        B = self.B
        self.assertTrue(B._trava_coleta.acquire(blocking=False))     # outra coleta "em andamento"
        try:
            with mock.patch.object(B, "_carregar_fontes", side_effect=AssertionError("não devia coletar")):
                self.assertIn("paginas", B.atualizar_agora())
        finally:
            B._trava_coleta.release()
