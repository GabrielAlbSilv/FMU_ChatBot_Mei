"""banco.py (PostgreSQL opcional) testado com SQLite — o mesmo SQL, sem precisar de servidor."""
from tests.apoio import *  # noqa: F401,F403
import sqlite3  # noqa: E402


class Banco(unittest.TestCase):
    def setUp(self):
        import banco
        import metricas
        self.banco, self.metricas = banco, metricas
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        caminho = os.path.join(self.tmp.name, "teste.db")
        banco.configurar(conectar=lambda: sqlite3.connect(caminho, check_same_thread=False, isolation_level=None),
                         dialeto="sqlite", retencao_dias=180)
        self.addCleanup(lambda: banco.configurar(url=None))
        metricas.limpar()

    def resposta(self, id_, voto=None, verificacao="confirmado", ajuste=None):
        self.metricas.registrar_resposta(id_, "Meu CPF 123.456.789-09: quanto custa o DAS?",
                                         {"resposta": "O DAS custa R$ 86,05.", "verificacao": {"status": verificacao},
                                          "ajuste_regras": ajuste, "fatos": [{"tema": "das_valor"}]}, 1500, versao="v14")
        if voto:
            self.metricas.registrar_feedback(id_, voto, "faltou o valor de 2027")
        self.assertTrue(self.banco.esperar())

    def test_resposta_e_voto_vao_para_o_banco_sem_dados_pessoais(self):
        self.resposta("a1b2c3d4e5f60001", voto="nao_util")
        linhas = self.banco._executar("SELECT * FROM respostas", buscar=True)
        self.assertEqual(len(linhas), 1)
        self.assertNotIn("123.456.789-09", linhas[0]["pergunta"])
        self.assertIn("[CPF]", linhas[0]["pergunta"])
        self.assertEqual((linhas[0]["voto"], linhas[0]["comentario"], linhas[0]["versao"]),
                         ("nao_util", "faltou o valor de 2027", "v14"))

    def test_voto_depois_de_reiniciar_o_servidor(self):
        self.resposta("a1b2c3d4e5f60002")
        self.metricas.limpar()                                   # servidor reiniciou: memória vazia
        self.assertTrue(self.metricas.registrar_feedback("a1b2c3d4e5f60002", "util"))
        self.assertTrue(self.banco.esperar())
        self.assertEqual(self.banco._executar("SELECT voto FROM respostas", buscar=True)[0]["voto"], "util")

    def test_painel(self):
        self.resposta("a1b2c3d4e5f60003", voto="util")
        self.resposta("a1b2c3d4e5f60004", voto="nao_util", verificacao="atualizada", ajuste="corrigida")
        self.resposta("a1b2c3d4e5f60005")
        self.banco.salvar_avaliacao({"versao": "v14", "casos": 21, "aprovados": 20, "percentual": 95.2,
                                     "reprovados": ["Teto 2028"]}, origem="semanal")
        p = self.banco.painel(30)
        t = p["totais"]
        self.assertEqual((t["respostas"], t["util"], t["nao_util"], t["aprovacao"]), (3, 1, 1, 50.0))
        self.assertEqual(t["atualizada_pelos_sites"], 33.3)
        self.assertEqual(t["corrigida_pelas_regras"], 33.3)
        self.assertEqual(t["tempo_mediano_ms"], 1500)
        self.assertEqual(len(p["por_dia"]), 30)                  # todos os dias do período, mesmo vazios
        self.assertEqual(p["por_dia"][-1]["respostas"], 3)
        self.assertEqual(sum(d["respostas"] for d in p["por_dia"]), 3)
        self.assertEqual(p["piores"][0]["comentario"], "faltou o valor de 2027")
        self.assertEqual(p["avaliacoes"][0]["reprovados"], ["Teto 2028"])
        self.assertEqual(p["avaliacoes"][0]["origem"], "semanal")

    def test_retencao_apaga_respostas_antigas(self):
        self.resposta("a1b2c3d4e5f60006")              # 1ª gravação do dia já agenda uma limpeza automática
        self.banco._executar("INSERT INTO respostas (id, criado_em, pergunta) VALUES (%s, {TS}, %s)",
                             ("antiga", "2020-01-01T00:00:00+00:00", "pergunta velha"))
        self.assertEqual(self.banco.limpar_antigos(), 1)
        ids = [l["id"] for l in self.banco._executar("SELECT id FROM respostas", buscar=True)]
        self.assertEqual(ids, ["a1b2c3d4e5f60006"])

    def test_banco_fora_do_ar_nao_derruba_nada(self):
        def falha():
            raise OSError("sem rede")
        self.banco.configurar(conectar=falha, dialeto="sqlite")
        self.metricas.registrar_resposta("a1b2c3d4e5f60007", "p", {"resposta": "r"}, 10)   # não levanta erro
        self.assertTrue(self.banco.esperar())
        self.assertGreaterEqual(self.banco.status()["descartados"], 1)
        self.assertIn("OSError", self.banco.status()["ultimo_erro"])

    def test_sql_do_postgres(self):
        self.banco.configurar(url="postgresql://x", dialeto="postgres")
        self.assertEqual(self.banco._sql("WHERE criado_em >= {TS} AND id = %s"),
                         "WHERE criado_em >= %s::timestamptz AND id = %s")
        self.assertIn("TIMESTAMPTZ", self.banco._sql(self.banco.ESQUEMA[0]))


class PainelHttp(unittest.TestCase):
    def setUp(self):
        import app as A
        self.A = A
        self.http = A.app.test_client()

    def test_painel_exige_senha(self):
        self.assertEqual(self.http.get("/painel").status_code, 200)            # a página abre...
        self.assertEqual(self.http.get("/api/painel").status_code, 401)       # ...os dados não
        with mock.patch.object(self.A.C, "STATUS_TOKEN", "segredo"):
            r = self.http.get("/api/painel", headers={"X-Token": "segredo"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("memoria", r.get_json())
