"""Compara modelos de embeddings na busca dos PDFs (acertos e memória) ANTES de trocar no Render.

Por que existe: o modelo padrão (all-MiniLM-L6-v2) foi treinado em inglês. Um modelo multilíngue
entende melhor o português, mas usa mais memória — e o plano gratuito do Render tem só 512 MB.
Este script mede as duas coisas na sua máquina, com os PDFs de verdade, para decidir com números.

Uso (precisa do fastembed e dos PDFs na pasta; NÃO usa a Groq nem a internet, exceto para baixar o
modelo na 1ª vez):
    python avaliar_busca.py                       -> modelo atual (MODELO_EMBEDDINGS ou o padrão)
    python avaliar_busca.py sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
    python avaliar_busca.py modeloA modeloB       -> compara os dois, lado a lado

Para cada modelo (em um processo separado, para medir a memória de cada um):
  - monta um índice TEMPORÁRIO (não mexe na pasta indice/);
  - roda as perguntas de CASOS, escritas como as pessoas escrevem (gíria, sem termo técnico);
  - conta em quantas o trecho certo aparece entre os 8 enviados ao modelo (acerto@8), na busca
    completa (palavras-chave + significado) e só pelo significado (mostra o efeito do modelo);
  - mede o pico de memória do processo.
Regra prática: troque o modelo só se o acerto subir de forma clara E o pico ficar abaixo de ~300 MB
(o servidor ainda precisa de memória para o Flask, a base web e as respostas).
"""
import json
import os
import subprocess
import sys
import time

# (pergunta como a pessoa escreve, termos que o trecho certo precisa ter — sem acento, minúsculas)
CASOS = [
    ("posso ter funcionário sendo mei?", ["empregado"]),
    ("quanto pago todo mês no boleto do mei", ["inss", "icms"]),
    ("esqueci de mandar a declaração anual do mei, e agora?", ["dasn", "multa"]),
    ("como faço pra fechar meu cnpj de mei", ["baixa"]),
    ("vendi mais do que podia esse ano, o que acontece com o mei?", ["desenquadramento"]),
    ("mei tem direito a se aposentar?", ["aposentadoria"]),
    ("o que acontece se eu atrasar o boleto do mei", ["juros", "multa"]),
    ("mei pode ter sócio?", ["socio"]),
    ("preciso dar nota fiscal quando vendo pra pessoa física?", ["nota fiscal", "pessoa fisica"]),
    ("quem é obrigado a declarar imposto de renda", ["obrigad", "declar"]),
    ("mandei a declaração do ir errada, como arrumo?", ["retificadora"]),
    ("posso colocar minha mãe como dependente no ir?", ["dependente"]),
    ("caí na malha fina, o que faço?", ["malha"]),
    ("quem não pode entrar no simples nacional", ["vedad"]),
    ("fui excluído do simples por dívida, dá pra voltar?", ["exclus"]),
]
K = 8


def _avaliar_um(modelo):
    """Roda no processo filho: devolve um dicionário com acertos e memória."""
    import resource

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ["MODELO_EMBEDDINGS"] = modelo
    import numpy as np

    import busca

    busca.NOME_MODELO_EMB = modelo
    inicio = time.time()
    emb = busca.criar_embeddings()
    trechos = busca.extrair_trechos()
    vetores = []
    for i in range(0, len(trechos), 64):
        vetores.extend(emb.passage_embed([t["texto"] for t in trechos[i:i + 64]]))
    vetores = busca._normalizar(np.array(vetores, dtype=np.float32))
    indice = busca.Indice(emb, vetores, trechos)
    montagem = time.time() - inicio

    def acertou(lista, termos):
        return any(all(t in busca._sem_acento(x["texto"]) for t in termos) for x in lista)

    hibrida = semantica = 0
    erros = []
    for pergunta, termos in CASOS:
        ok_h = acertou(indice.buscar_varias([pergunta], k=K), termos)
        q = busca._normalizar(np.array(list(emb.query_embed([pergunta])), dtype=np.float32))[0]
        ordem = np.argsort(-(vetores @ q))[:K]
        ok_s = acertou([trechos[int(i)] for i in ordem], termos)
        hibrida += ok_h
        semantica += ok_s
        if not ok_h:
            erros.append(pergunta)
    pico_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024   # Linux: KB -> MB
    if sys.platform == "darwin":
        pico_mb /= 1024                                                     # macOS: bytes
    return {"modelo": modelo, "casos": len(CASOS), "acerto_hibrida": hibrida, "acerto_so_significado": semantica,
            "pico_memoria_mb": round(pico_mb), "segundos_montagem": round(montagem), "trechos": len(trechos),
            "erros_hibrida": erros}


def main(argv):
    if len(argv) >= 2 and argv[0] == "--um":
        print(json.dumps(_avaliar_um(argv[1]), ensure_ascii=False))
        return 0
    if sys.platform.startswith("win"):
        print("Aviso: a medição de memória usa o módulo 'resource' (Linux/macOS). No Windows, rode no WSL.")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config as C

    modelos = argv or [C.MODELO_EMBEDDINGS]
    resultados = []
    for modelo in modelos:
        print(f"Avaliando {modelo} (a 1ª vez baixa o modelo e pode demorar)...", flush=True)
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--um", modelo],
                           capture_output=True, text=True)
        linha = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            resultados.append(json.loads(linha))
        except ValueError:
            print(f"Falhou ({modelo}):\n{r.stderr[-1500:]}")
    if not resultados:
        return 1
    print()
    print(f"{'modelo':62} {'busca completa':>15} {'só significado':>15} {'memória':>9}")
    for r in resultados:
        print(f"{r['modelo'][:62]:62} {r['acerto_hibrida']:>7}/{r['casos']:<7} "
              f"{r['acerto_so_significado']:>7}/{r['casos']:<7} {r['pico_memoria_mb']:>6} MB")
    for r in resultados:
        if r["erros_hibrida"]:
            print(f"\nNão achou o trecho certo com {r['modelo']}:")
            for p in r["erros_hibrida"]:
                print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
