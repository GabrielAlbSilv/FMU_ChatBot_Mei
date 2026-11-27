"""rag.py e busca.py: fluxo da resposta, streaming, reescrita, citações e regras no prompt."""
from tests.apoio import *  # noqa: F401,F403


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
            eventos = list(self.rag.responder_stream(
                self.indice, self._cliente("Sim, você precisa declarar: R$ 34 mil supera o limite [1].", chamadas),
                "Recebi R$ 34 mil em salários tributáveis em 2025. Preciso entregar o IRPF 2026?", []))
        fim = eventos[-1][1]
        prompt = chamadas[-1]["messages"][-1]["content"]
        self.assertIn("FATOS CALCULADOS", prompt)
        self.assertLess(prompt.index("FATOS CALCULADOS"), prompt.index("Trechos recuperados"))
        self.assertEqual(fim["ajuste_regras"], "corrigida")
        self.assertIn("dispensado", fim["resposta"])
        # Com fatos, o texto só aparece DEPOIS de conferido: a versão errada nunca chega à tela.
        mostrado = "".join(d for t, d in eventos if t == "texto")
        self.assertNotIn("supera", mostrado)
        self.assertIn("dispensado", mostrado)
        self.assertFalse(fim["substituida"])          # nada a "trocar" na tela
        self.assertEqual(eventos[0], ("status", "Calculando pelas regras oficiais…"))
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
            fim = list(self.rag.responder_stream(self.indice, self._cliente("Sim, o MEI pode emitir nota fiscal [1].",
                                                                            chamadas),
                                                 "O MEI pode emitir nota fiscal?", []))[-1][1]
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


class Melhorias(unittest.TestCase):
    """Reescrita com JSON, privacidade antes da Groq e limite de trechos por PDF."""

    @classmethod
    def setUpClass(cls):
        import busca
        import rag
        cls.rag, cls.busca = rag, busca

    def setUp(self):
        self.rag._cache.clear()
        self.indice = types.SimpleNamespace(buscar_varias=lambda consultas, k=8: [
            {"fonte": "4-Perguntas&Respostas3_MEI.pdf", "pagina": 7, "texto": "O DAS é um valor fixo mensal."}])
        for p in (mock.patch.object(self.rag, "trechos_da_web", return_value=[]),
                  mock.patch.object(self.rag.verificador, "ATIVO", False)):
            p.start()
            self.addCleanup(p.stop)

    def _cliente(self, reescrita, texto, chamadas):
        def criar(**k):
            chamadas.append(k)
            if k.get("stream"):
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content=texto))])])
            return Resposta(reescrita)
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=criar)))

    def test_ler_reescrita_json_texto_e_quebrado(self):
        ler = self.rag._ler_reescrita
        self.assertEqual(ler('{"consulta": "valor do DAS", "atividade": null}'),
                         ("valor do DAS", {"consulta": "valor do DAS", "atividade": None}))
        self.assertEqual(ler("valor do DAS do MEI\nexplicação"), ("valor do DAS do MEI", {}))
        self.assertEqual(ler('{"consulta": "valor do DAS", "temas": ['), ("valor do DAS", {}))   # JSON cortado
        self.assertEqual(ler(""), (None, {}))

    def test_dado_confirmado_da_reescrita_entra_no_calculo(self):
        chamadas = []
        json_reescrita = json.dumps({"consulta": "valor do DAS do MEI prestador de serviços", "atividade": "servicos",
                                     "evidencia_atividade": "sou manicure", "temas": ["das_valor"]})
        fim = list(self.rag.responder_stream(self.indice, self._cliente(json_reescrita, "O DAS custa R$ 86,05.", chamadas),
                                             "Sou manicure. Quanto pago de DAS em 2026?", []))[-1][1]
        prompt = chamadas[-1]["messages"][-1]["content"]
        self.assertIn("R$ 86,05 por mês", prompt)
        self.assertNotIn("R$ 82,05", prompt)
        self.assertIn("presta serviços", prompt)                 # "Dados já informados" também usa o dado
        self.assertEqual([f["tema"] for f in fim["fatos"]], ["das_valor"])

    def test_dado_inventado_pela_reescrita_e_ignorado(self):
        chamadas = []
        json_reescrita = json.dumps({"consulta": "valor do DAS", "faturamento": 90000, "atividade": "comercio",
                                     "evidencia_atividade": "tenho uma loja"})
        list(self.rag.responder_stream(self.indice, self._cliente(json_reescrita, "ok", chamadas),
                                       "Quanto pago de DAS em 2026?", []))
        prompt = chamadas[-1]["messages"][-1]["content"]
        self.assertNotIn("90.000", prompt)
        self.assertIn("R$ 86,05", prompt)                        # sem atividade confirmada: os três valores
        self.assertIn("R$ 82,05", prompt)

    def test_cpf_e_email_nao_chegam_na_groq(self):
        chamadas = []
        historico = [{"papel": "user", "texto": "meu e-mail é joao@exemplo.com"},
                     {"papel": "assistant", "texto": "Certo."}]
        list(self.rag.responder_stream(self.indice, self._cliente("consulta", "Consulte no site da Receita.", chamadas),
                                       "Meu CPF 123.456.789-09 está pendente, e agora?", historico))
        enviado = json.dumps([c["messages"] for c in chamadas], ensure_ascii=False)
        self.assertNotIn("123.456.789-09", enviado)
        self.assertNotIn("joao@exemplo.com", enviado)
        self.assertIn("[CPF]", enviado)
        self.assertIn("[e-mail]", enviado)

    def test_um_pdf_nao_ocupa_todas_as_vagas(self):
        trechos = []
        for i in range(20):        # trechos do IRPF intercalados com outro PDF (assim não viram vizinhos)
            trechos += [{"fonte": "5-IRPF.pdf", "pagina": i, "texto": f"irpf {i}"},
                        {"fonte": "9-Outro.pdf", "pagina": i, "texto": f"outro {i}"}]
        trechos += [{"fonte": "4-MEI.pdf", "pagina": 1, "texto": "mei a"},
                    {"fonte": "3-MEI.pdf", "pagina": 1, "texto": "mei b"}]
        irpf = list(range(0, 40, 2))
        ordem = irpf + [40, 41]                                  # os do IRPF vêm primeiro no ranking
        sem = self.busca.juntar_vizinhos(trechos, ordem, k=8)
        self.assertEqual({g["fonte"] for g in sem}, {"5-IRPF.pdf"})
        com = self.busca.juntar_vizinhos(trechos, ordem, k=8, max_por_fonte=5)
        fontes = [g["fonte"] for g in com]
        self.assertEqual(len(com), 8)
        self.assertIn("4-MEI.pdf", fontes)
        self.assertIn("3-MEI.pdf", fontes)
        self.assertEqual(fontes.count("5-IRPF.pdf"), 6)          # 5 + 1 vaga que sobrou (sem outro candidato)
        so_irpf = self.busca.juntar_vizinhos(trechos, irpf, k=8, max_por_fonte=5)
        self.assertEqual(len(so_irpf), 8)                        # sem outro PDF, completa com o que sobrou

    def test_pergunta_de_ir_nao_tem_limite_por_pdf(self):
        self.assertTrue(self.busca.pergunta_sobre_ir("Quem precisa declarar imposto de renda?"))
        self.assertTrue(self.busca.pergunta_sobre_ir("como corrigir o IRPF"))
        self.assertFalse(self.busca.pergunta_sobre_ir("O MEI paga imposto de renda?"))
        self.assertFalse(self.busca.pergunta_sobre_ir("quanto custa o DAS"))
