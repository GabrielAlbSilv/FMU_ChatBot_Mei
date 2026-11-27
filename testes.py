"""Roda todos os testes offline do FiscOn (sem internet de verdade e sem Groq).

Os testes ficam na pasta tests/, um arquivo por assunto (verificador, regras fiscais, rag, voz...).

    python testes.py                  -> todos
    python testes.py RegrasFiscais    -> só os testes cujo nome contém "RegrasFiscais"
    python -m unittest discover -s tests -t .   (mesma coisa, pelo unittest)
"""
import os
import sys
import unittest

RAIZ = os.path.dirname(os.path.abspath(__file__))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)
# A pesquisa geral (pesquisa.py) baixa sitemaps e páginas: nos testes ela fica desligada.
os.environ.setdefault("PESQUISA_ATIVA", "0")


def main(argv):
    carregador = unittest.TestLoader()
    if argv:
        carregador.testNamePatterns = [f"*{a}*" for a in argv]
    suite = carregador.discover(os.path.join(RAIZ, "tests"), top_level_dir=RAIZ)
    resultado = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if resultado.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
