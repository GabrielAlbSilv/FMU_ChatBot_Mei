"""app.py e privacidade.py: IP real, limites, /api/status enxuto, feedback e dados pessoais mascarados."""
from tests.apoio import *  # noqa: F401,F403


class Privacidade(unittest.TestCase):
    def setUp(self):
        import privacidade
        self.m = privacidade.mascarar

    def test_mascara_dados_pessoais(self):
        self.assertEqual(self.m("CPF 123.456.789-09 e CNPJ 12.345.678/0001-90"), "CPF [CPF] e CNPJ [CNPJ]")
        self.assertEqual(self.m("cpf 12345678909, cnpj 12345678000190"), "cpf [CPF ou telefone], cnpj [CNPJ]")
        self.assertEqual(self.m("escreva para maria.souza@gmail.com"), "escreva para [e-mail]")
        self.assertEqual(self.m("ligue (11) 91234-5678"), "ligue [telefone]")
        self.assertEqual(self.m("CNPJ novo 12.ABC.345/01DE-35"), "CNPJ novo [CNPJ]")

    def test_valores_datas_e_anos_ficam(self):
        for texto in ("Faturei R$ 35.584,00 em 2025, até 31/05/2026, 20% de R$ 1.621,00",
                      "DAS de R$ 86,05; 50 mil; 81000; teto de R$ 110.000,00 em 2027", "", "MEI aberto em 07/2026"):
            self.assertEqual(self.m(texto), texto)

    def test_historico(self):
        import privacidade
        h = privacidade.mascarar_historico([{"papel": "user", "texto": "CPF 123.456.789-09"}, "lixo"])
        self.assertEqual(h[0]["texto"], "CPF [CPF]")
        self.assertEqual(privacidade.mascarar_historico(None), [])


class Servidor(unittest.TestCase):
    def setUp(self):
        import app as A
        import metricas
        self.A, self.metricas = A, metricas
        self.http = A.app.test_client()
        antes = dict(A.estado)
        self.addCleanup(lambda: A.estado.update(antes))

        def responder(indice, cliente, pergunta, historico):
            yield "texto", "Resposta de teste."
            yield "fim", {"resposta": "Resposta de teste.", "verificacao": {"status": "confirmado"}, "fatos": [],
                          "ajuste_regras": None, "substituida": False, "cache": False}
        A.estado.update(pid=os.getpid(), pronto=True, erro=None, rag=types.SimpleNamespace(responder_stream=responder))
        A._acessos.clear()
        metricas.limpar()

    def perguntar(self, **cabecalhos):
        return self.http.post("/api/chat", json={"pergunta": "Quanto custa o DAS?"}, headers=cabecalhos)

    def test_ip_real_ignora_x_forwarded_for(self):
        with self.A.app.test_request_context(headers={"X-Forwarded-For": "1.2.3.4", "True-Client-IP": "9.9.9.9"}):
            self.assertEqual(self.A.ip_cliente(), "9.9.9.9")
        with self.A.app.test_request_context(headers={"X-Forwarded-For": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "10.0.0.7"}):
            self.assertEqual(self.A.ip_cliente(), "10.0.0.7")
        with self.A.app.test_request_context(headers={"True-Client-IP": "<script>"}, environ_base={"REMOTE_ADDR": "10.0.0.7"}):
            self.assertEqual(self.A.ip_cliente(), "10.0.0.7")     # valor estranho no cabeçalho é ignorado

    def test_trocar_x_forwarded_for_nao_burla_o_limite(self):
        with mock.patch.object(self.A, "LIMITE_REQUISICOES", 2):
            codigos = [self.perguntar(**{"X-Forwarded-For": f"5.5.5.{i}"}).status_code for i in range(3)]
        self.assertEqual(codigos, [200, 200, 429])

    def test_limite_global_protege_a_cota_da_groq(self):
        with mock.patch.object(self.A, "LIMITE_REQUISICOES", 100), mock.patch.object(self.A, "LIMITE_GLOBAL", 3):
            respostas = [self.perguntar(**{"True-Client-IP": f"7.7.7.{i}"}) for i in range(4)]
        self.assertEqual([r.status_code for r in respostas], [200, 200, 200, 429])
        self.assertIn("Muitas pessoas", respostas[-1].get_json()["erro"])
        self.assertEqual(self.metricas.resumo()["contadores"].get("erro:limite_global"), 1)

    def test_status_publico_e_detalhado(self):
        publico = self.http.get("/api/status").get_json()
        for chave in ("pronto", "erro", "etapa", "documentos", "base_web", "base_atualizada_em"):
            self.assertIn(chave, publico)
        for chave in ("pid", "rag_md5", "pilha_thread_init", "config", "metricas", "commit"):
            self.assertNotIn(chave, publico)
        self.A.estado["erro"] = "RuntimeError: GROQ_API_KEY não configurada"
        self.assertEqual(self.http.get("/api/status").get_json()["erro"], "indisponível")   # sem detalhes
        with mock.patch.object(self.A.C, "STATUS_TOKEN", "segredo"):
            self.assertNotIn("pid", self.http.get("/api/status", headers={"X-Token": "errado"}).get_json())
            completo = self.http.get("/api/status", headers={"X-Token": "segredo"}).get_json()
        for chave in ("pid", "metricas", "config", "regras_nao_validadas", "pdfs_faltando"):
            self.assertIn(chave, completo)
        self.assertIn("GROQ_API_KEY", completo["erro"])
        self.assertNotIn("GROQ_API_KEY", completo["config"])     # segredos nunca aparecem

    def test_feedback_liga_o_voto_a_resposta(self):
        linhas = [json.loads(l) for l in self.perguntar().get_data(as_text=True).splitlines() if l.strip()]
        fim = linhas[-1]
        self.assertEqual(fim["tipo"], "fim")
        self.assertRegex(fim["id"], r"^[0-9a-f]{16}$")
        r = self.http.post("/api/feedback", json={"id": fim["id"], "voto": "util"})
        self.assertEqual(r.get_json(), {"ok": True})
        self.http.post("/api/feedback", json={"id": fim["id"], "voto": "nao_util"})      # mudou de ideia
        resumo = self.metricas.resumo()
        self.assertEqual((resumo["feedback"]["util"], resumo["feedback"]["nao_util"]), (0, 1))
        self.assertEqual(resumo["respostas"], 1)
        self.assertEqual(resumo["percentual"]["conferida_confirmada"], 100.0)
        self.assertEqual(self.http.post("/api/feedback", json={"id": fim["id"], "voto": "talvez"}).status_code, 400)
        self.assertEqual(self.http.post("/api/feedback", json={"id": "<x>", "voto": "util"}).status_code, 400)
        self.assertFalse(self.http.post("/api/feedback", json={"id": "abcdef0123456789", "voto": "util"}).get_json()["ok"])

    def test_cabecalhos_de_seguranca(self):
        r = self.http.get("/api/status")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")


class Configuracao(unittest.TestCase):
    def test_flags_e_numeros(self):
        import config as C
        with mock.patch.dict(os.environ, {"X_TESTE": "0", "X_NUM": "7", "X_RUIM": "abc"}):
            self.assertFalse(C.flag("X_TESTE", True))
            self.assertTrue(C.flag("X_AUSENTE", True))
            self.assertEqual(C.inteiro("X_NUM", 3), 7)
            self.assertEqual(C.inteiro("X_RUIM", 3), 3)
            self.assertEqual(C.decimal("X_RUIM", 0.5), 0.5)
