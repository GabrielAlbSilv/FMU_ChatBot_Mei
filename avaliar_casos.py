"""Reteste com o chatbot de verdade (Groq + índice dos PDFs) dos casos do relatório de testes.

Roda as perguntas que reprovaram a divulgação (NAF e FiscOn), em uma mensagem e em duas, e confere
se cada resposta final traz os valores esperados e não traz os errados. Gera relatorio_reteste.md
para a equipe do NAF revisar ANTES da divulgação.

Uso (precisa do .env com GROQ_API_KEY e das bibliotecas do requirements.txt):
    python avaliar_casos.py            -> todos os casos
    python avaliar_casos.py --sem-sites -> sem a conferência com Sebrae/Receita (mais rápido)
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent

# (nome, [mensagens do usuário em ordem], deve conter (cada item: alternativas), não pode conter)
CASOS = [
    ("IRPF — R$ 34 mil (abaixo do limite)",
     ["Recebi R$ 34 mil em salários tributáveis em 2025, sem outros critérios. Preciso entregar o IRPF 2026? "
      "Qual foi o prazo?"],
     [["dispensad", "não é obrigad", "não precisa", "não está obrigad"], ["35.584"], ["29/05/2026", "29 de maio"]],
     ["supera o limite", "é obrigado a entregar"]),
    ("IRPF — exatamente R$ 35.584,00",
     ["Recebi exatamente R$ 35.584,00 em salários tributáveis em 2025, sem outros critérios. Preciso entregar o IRPF 2026?"],
     [["dispensad", "não é obrigad", "não precisa", "não está obrigad"], ["35.584"]], []),
    ("IRPF — R$ 35.584,01 (acima do limite)",
     ["Recebi R$ 35.584,01 em salários tributáveis em 2025. Preciso entregar o IRPF 2026?"],
     [["obrigad", "precisa entregar", "precisa declarar", "deve entregar"], ["35.584"]], ["dispensad"]),
    ("MEI — julho/2026, R$ 50 mil (uma mensagem)",
     ["Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso? Qual foi o excesso e quando "
      "começa o desenquadramento?"],
     [["40.500"], ["9.500"], ["23,46", "23,5"], ["julho de 2026", "07/2026", "julho/2026"]],
     ["01/01/2027", "janeiro de 2027", "não houve excesso"]),
    ("MEI — julho/2026, R$ 50 mil (duas mensagens)",
     ["Abri o MEI em julho de 2026.", "Faturei R$ 50 mil até dezembro. Houve excesso? Quando começa o desenquadramento?"],
     [["40.500"], ["9.500"], ["23,46", "23,5"], ["julho de 2026", "07/2026", "julho/2026"]],
     ["01/01/2027", "janeiro de 2027", "não houve excesso"]),
    ("MEI — continuação após a pergunta completa",
     ["Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?",
      "E considerando o teto de R$ 81 mil, ainda houve excesso?"],
     [["40.500"], ["9.500"]], ["não houve excesso"]),
    ("MEI — sequência do teste do NAF (completa → limite à parte → continuação)",
     ["Um MEI abriu em julho de 2026 e faturou R$ 50 mil até dezembro. Houve excesso?",
      "Qual o limite proporcional para quem abriu em julho de 2026?",
      "Então houve excesso?"],
     [["40.500"], ["9.500"], ["julho de 2026", "07/2026", "julho/2026"]],
     ["não houve excesso", "01/01/2027"]),
    ("MEI — limite de quem abriu em julho/2026 (sem faturamento informado)",
     ["Qual o limite de faturamento do MEI para quem abriu em julho de 2026?"],
     [["40.500"]], ["não encontrei"]),
    ("MEI — excesso de exatamente 20% (R$ 48.600)",
     ["Um MEI abriu em julho de 2026 e faturou R$ 48.600 até dezembro. Houve excesso? Quando começa o desenquadramento?"],
     [["8.100"], ["01/01/2027", "janeiro de 2027"]], ["retroativ"]),
    ("MEI — excesso de R$ 48.601 (acima de 20%)",
     ["Um MEI abriu em julho de 2026 e faturou R$ 48.601 até dezembro. Houve excesso? Quando começa o desenquadramento?"],
     [["8.101"], ["julho de 2026", "07/2026", "julho/2026"]], ["01/01/2027"]),
    ("MEI sem faturamento — DAS e DASN",
     ["O MEI ficou ativo em 2025, sem faturamento. Precisa pagar DAS e entregar a declaração anual?"],
     [["31/05/2026", "31 de maio de 2026"], ["devido", "precisa pagar", "deve pagar", "continua"]], []),
    ("DAS 2026 — só serviços",
     ["Quanto paga de DAS em 2026 um MEI comum que presta exclusivamente serviços?"],
     [["86,05"], ["81,05"], ["5,00", "R$ 5"]], ["entre 2% e 5%", "R$ 6"]),
    ("DIRF em 2026",
     ["Ainda é preciso entregar DIRF em 2026 sobre pagamentos e retenções de 2025?"],
     [["esocial"], ["reinf"]], []),
    ("Teto MEI 2026 e 2027 (LC 238/2026)",
     ["Qual era o teto do MEI em 2026 e qual estava previsto para 2027 pela LC nº 238/2026?"],
     [["81.000", "81 mil"], ["110.000", "110 mil"], ["orçament"]], ["não encontrei"]),
]


def _contem(texto, alternativa):
    import regras_fiscais as R
    return R._sem_acento(alternativa) in R._sem_acento(texto).replace("*", "")


def avaliar(resposta, deve, nao_pode):
    faltou = [" / ".join(g) for g in deve if not any(_contem(resposta, a) for a in g)]
    proibido = [p for p in nao_pode if _contem(resposta, p)]
    return faltou, proibido


def main():
    from dotenv import load_dotenv

    load_dotenv(AQUI / ".env")
    if not os.getenv("GROQ_API_KEY"):
        sys.exit("Defina GROQ_API_KEY no .env para rodar o reteste.")
    if "--sem-sites" in sys.argv:
        os.environ["VERIFICAR_SITES"] = "0"
    sys.path.insert(0, str(AQUI))
    import rag

    indice, cliente = rag.iniciar_rag()
    linhas = [f"# Reteste dos casos do relatório — {datetime.now():%d/%m/%Y %H:%M}", "",
              f"Versão: `{rag.VERSAO}` · conferência com sites: {'ligada' if rag.verificador.ATIVO else 'desligada'}",
              "", "| Caso | Resultado | Ajuste pelas regras | Observação |", "|---|---|---|---|"]
    detalhes, aprovados = [], 0
    for nome, mensagens, deve, nao_pode in CASOS:
        rag._cache.clear()
        historico, final = [], None
        for msg in mensagens:
            final = rag.responder(indice, cliente, msg, historico)
            historico += [{"papel": "user", "texto": msg}, {"papel": "assistant", "texto": final["resposta"]}]
            time.sleep(1)        # respeita o limite por minuto da Groq
        faltou, proibido = avaliar(final["resposta"], deve, nao_pode)
        ok = not faltou and not proibido
        aprovados += ok
        obs = "; ".join([f"faltou: {f}" for f in faltou] + [f"não podia ter: {p}" for p in proibido]) or "—"
        linhas.append(f"| {nome} | {'✅ ok' if ok else '❌ falhou'} | {final.get('ajuste_regras') or '—'} | {obs} |")
        detalhes += [f"## {nome}", ""] + [f"> **Usuário:** {m}" for m in mensagens] + \
                    ["", final["resposta"], "", f"_Verificação: {final['verificacao'].get('status')}_", ""]
        print(f"{'OK ' if ok else 'ERRO'} {nome} {obs if not ok else ''}", flush=True)
    linhas += ["", f"**{aprovados}/{len(CASOS)} casos corretos.** Revisão do NAF: ☐ aprovado ☐ ajustar", "",
               "---", ""] + detalhes
    (AQUI / "relatorio_reteste.md").write_text("\n".join(linhas), encoding="utf-8")
    print(f"\n{aprovados}/{len(CASOS)} corretos. Relatório: relatorio_reteste.md")
    sys.exit(0 if aprovados == len(CASOS) else 1)


if __name__ == "__main__":
    main()
