"""regras_fiscais.py: contas feitas pelo código (casos do relatório do NAF)."""
from tests.apoio import *  # noqa: F401,F403


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


class LeiComplementar238(unittest.TestCase):
    """LC 238/2026: teto de 2027/2028 (condicionado ao orçamento), empregados e perdão de dívidas."""

    @classmethod
    def setUpClass(cls):
        import regras_fiscais as R
        cls.R = R

    def setUp(self):
        from datetime import date
        p = mock.patch.object(self.R, "hoje", return_value=date(2026, 11, 27))
        p.start()
        self.addCleanup(p.stop)

    def fato(self, pergunta, tema, historico=None, regras=None):
        fatos = [f for f in self.R.fatos_para(pergunta, historico, regras) if f["tema"] == tema]
        self.assertTrue(fatos, f"nenhum fato '{tema}' para: {pergunta}")
        return fatos[0]

    def regras_com(self, **mudancas_2027):
        import copy
        regras = copy.deepcopy(self.R.carregar_regras())
        regras["mei"]["2027"].update(mudancas_2027)
        return regras

    def test_teto_2028_previsto(self):
        f = self.fato("Qual será o teto do MEI em 2028?", "teto")
        self.assertIn("R$ 140.000,00", f["texto"])
        self.assertIn("previsto", f["texto"])
        self.assertNotIn("81.000", f["texto"])

    def test_teto_sem_ano_mostra_o_atual_e_os_previstos(self):
        f = self.fato("Qual o teto do MEI?", "teto")
        for valor in ("R$ 81.000,00", "R$ 110.000,00", "R$ 140.000,00"):
            self.assertIn(valor, f["texto"])

    def test_limite_proporcional_2027_avisa_o_valor_previsto(self):
        f = self.fato("Abri o MEI em julho de 2027, qual o meu limite de faturamento?", "limite_abertura")
        self.assertIn("R$ 40.500,00", f["texto"])           # regra em vigor hoje
        self.assertIn("R$ 55.000,02", f["texto"])           # 9.166,67 x 6, se o teto novo for confirmado
        self.assertIn("lei orçamentária", f["texto"])

    def test_excesso_que_depende_do_teto_novo_nao_fixa_conclusao(self):
        f = self.fato("Um MEI abriu em julho de 2027 e faturou R$ 50 mil até dezembro. Houve excesso?", "excesso")
        self.assertIsNone(f["conclusao"])                    # depende do orçamento: não "corrige" nenhuma das versões
        self.assertIn("Pela regra em vigor hoje", f["texto"])
        self.assertIn("sem excesso", f["texto"])
        self.assertIn("R$ 9.500,00", f["texto"])
        ok = "Depende: pela regra atual o limite é R$ 40.500,00; com o teto novo da LC 238, R$ 55.000,02 e não houve excesso."
        self.assertNotEqual(self.R.garantir(ok, [f])[1], "corrigida")
        # com R$ 70 mil há excesso nos dois cenários: aí a conclusão é fixa
        f = self.fato("Um MEI abriu em julho de 2027 e faturou R$ 70 mil até dezembro. Houve excesso?", "excesso")
        self.assertTrue(f["conclusao"]["valor"])

    def test_teto_confirmado_vale_sem_esperar_o_salario_minimo(self):
        regras = self.regras_com(vigente=True)
        lim = self.R.limite_mei(2027, 7, regras)
        self.assertEqual(lim["valor"], self.R._d("55000.02"))
        self.assertIsNone(lim["previsto"])
        self.assertEqual(self.R.limite_mei(2027, None, regras)["valor"], self.R._d("110000"))
        das = self.R.valores_das(2027, regras)                # DAS de 2027 ainda sem salário mínimo cadastrado
        self.assertEqual(das["ano"], 2026)
        self.assertIn("2027", das["aviso"])

    def test_empregados_por_ano(self):
        f2026 = self.fato("Em 2026 o MEI pode contratar quantos funcionários?", "empregados")
        self.assertIn("1 empregado", f2026["texto"])
        self.assertNotIn("até 2", f2026["texto"])
        f2027 = self.fato("Quantos empregados o MEI vai poder ter em 2027?", "empregados")
        self.assertIn("até 2 empregados", f2027["texto"])
        self.assertIn("Lei Complementar nº 238/2026", f2027["texto"])
        ambos = self.fato("O MEI pode contratar um funcionário?", "empregados")
        self.assertIn("1 empregado", ambos["texto"])
        self.assertIn("até 2 empregados", ambos["texto"])
        final, ajuste, _ = self.R.garantir("Sim, o MEI pode contratar.", [f2027])
        self.assertEqual(ajuste, "complementada")
        self.assertIn("até 2 empregados", final)

    def test_contratar_contador_nao_e_empregado(self):
        temas = [f["tema"] for f in self.R.fatos_para("Preciso contratar um contador para o MEI?")]
        self.assertNotIn("empregados", temas)

    def test_perdao_das_dividas(self):
        f = self.fato("As dívidas do MEI foram perdoadas? Preciso pedir?", "remissao")
        for trecho in ("13/10/2026", "automático", "ICMS", "1º de janeiro de 2027", "não são devolvidos"):
            self.assertIn(trecho, f["texto"])
        self.assertTrue(f["fonte"].startswith("https://www.planalto.gov.br/"))
        final, ajuste, _ = self.R.garantir("Sim, a LC 238 perdoou as dívidas.", [f])
        self.assertEqual(ajuste, "complementada")
        self.assertEqual(self.fato("Como funciona a anistia da LC 238 para dívidas do MEI?", "remissao")["tema"],
                         "remissao")

    def test_pendentes_de_validacao(self):
        pendentes = self.R.pendentes_validacao()
        self.assertIn("mei/2027", pendentes)
        self.assertIn("remissao_lc238", pendentes)
        regras = {"mei": {"2026": {"validado_naf": True}}, "dirf": {"validado_naf": False}}
        self.assertEqual(self.R.pendentes_validacao(regras), ["dirf"])


class DadosDoModelo(unittest.TestCase):
    """A reescrita do modelo pode sugerir dados; o código só aceita o que confirma no texto do usuário."""

    @classmethod
    def setUpClass(cls):
        import regras_fiscais as R
        cls.R = R

    def test_so_aceita_o_que_esta_no_texto(self):
        R = self.R
        pergunta = "Sou manicure, abri em julho de 2026 e faturei 50 mil. Houve excesso?"
        aceitos = R.validar_dados_llm({"faturamento": 50000, "mes_abertura": 7, "ano_abertura": 2026,
                                       "atividade": "servicos", "evidencia_atividade": "Sou manicure",
                                       "temas": ["excesso", "inventado"]}, pergunta)
        self.assertEqual(aceitos["faturamento"], R._d("50000"))
        self.assertEqual(aceitos["abertura"], {"mes": 7, "ano": 2026})
        self.assertEqual(aceitos["atividade"], "servicos")
        self.assertEqual(aceitos["temas"], {"excesso"})
        inventados = R.validar_dados_llm({"faturamento": 90000, "rendimento": "35.000,00", "mes_abertura": 3,
                                          "ano_abertura": 2030, "atividade": "comercio",
                                          "evidencia_atividade": "vendo roupas"}, pergunta)
        self.assertEqual(inventados, {})
        self.assertEqual(R.validar_dados_llm("não é um dicionário", pergunta), {})
        # ano que não aparece no texto não é aceito (o mês sim)
        self.assertEqual(R.validar_dados_llm({"mes_abertura": 7, "ano_abertura": 2025}, pergunta)["abertura"],
                         {"mes": 7, "ano": None})

    def test_dado_confirmado_muda_o_calculo(self):
        R = self.R
        pergunta = "Sou manicure. Quanto pago de DAS em 2026?"
        sem = [f for f in R.fatos_para(pergunta) if f["tema"] == "das_valor"][0]
        self.assertIn("R$ 82,05", sem["texto"])                 # sem saber a atividade: mostra os três valores
        extras = R.validar_dados_llm({"atividade": "servicos", "evidencia_atividade": "sou manicure"}, pergunta)
        com = [f for f in R.fatos_para(pergunta, extras=extras) if f["tema"] == "das_valor"][0]
        self.assertIn("R$ 86,05", com["texto"])
        self.assertNotIn("R$ 82,05", com["texto"])

    def test_regex_tem_prioridade(self):
        R = self.R
        historico = [{"papel": "user", "texto": "Recebi R$ 34 mil de salário em 2025."}]
        extras = R.validar_dados_llm({"faturamento": 34000}, "Preciso declarar o IRPF 2026?", historico)
        sit = R.completar_situacao(R.extrair_situacao("Preciso declarar o IRPF 2026?", historico), extras)
        self.assertEqual(sit["rendimento"], R._d("34000"))
        self.assertIsNone(sit["faturamento"])                   # o mesmo valor não vira faturamento também
