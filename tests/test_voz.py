"""voz.py e a rota /api/voz/transcrever."""
from tests.apoio import *  # noqa: F401,F403


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
