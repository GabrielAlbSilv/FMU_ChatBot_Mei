"""Mostra, passo a passo, o que a conferência com Sebrae/Receita Federal faz para uma pergunta.

    python diagnosticar_verificacao.py "Qual o limite de faturamento do MEI em 2026?"
    python diagnosticar_verificacao.py "..." --llm      # também roda a comparação com a Groq (precisa de GROQ_API_KEY)

Serve para descobrir ONDE a conferência falha na SUA máquina/servidor:
  1) quais páginas foram escolhidas (lista fontes_oficiais.json e/ou buscador);
  2) se cada página abriu (ou foi bloqueada / deu erro) e quanto texto útil saiu dela;
  3) qual trecho seria enviado ao modelo para comparar;
  4) (com --llm) o veredito do modelo e o motivo.
"""
import argparse
import os
import sys

import verificador as V


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pergunta")
    ap.add_argument("--llm", action="store_true", help="roda também a comparação com o modelo (Groq)")
    ap.add_argument("--rascunho", default="", help="resposta do bot (a conferência procura pelos termos dela)")
    args = ap.parse_args()

    print(f"== Conferência ligada (VERIFICAR_SITES): {V.ATIVO} | buscador de endereços: {V.BUSCA_DESCOBERTA}\n")

    print("== 0) O que a conferência do chat usa (base local primeiro; ao vivo se não houver página sobre o assunto)")
    rica, destaques = V.expandir_consulta(args.pergunta)
    print(f"   consulta: {rica[:160]}")
    print(f"   expressões do assunto: {destaques or '(nenhuma — assunto não reconhecido em _INTENCOES)'}")
    try:
        import base_web
        base_web.carregar()
    except Exception as erro:  # noqa: BLE001
        print(f"   base local indisponível: {erro}")
    import pesquisa
    trechos = V.coletar_trechos(args.pergunta, args.pergunta)
    print(f"   catálogo de endereços: {pesquisa.status_catalogo()}")
    trechos = pesquisa.reforcar(trechos, args.pergunta, args.pergunta, args.rascunho or args.pergunta)
    for i, t in enumerate(trechos, 1):
        print(f"   [W{i}] {t.get('relevancia', 0):.2f} {'SOBRE O ASSUNTO' if t.get('destaque') else 'genérica      '} {t['url']}")
        print(f"        {t['texto'][:200].replace(chr(10), ' ')}")
    print()

    print("== 1) Páginas candidatas")
    sementes = V.urls_das_sementes(args.pergunta)
    print(f"   de fontes_oficiais.json: {len(sementes)}")
    for u in sementes:
        print("    -", u)
    urls = V.descobrir_urls(args.pergunta)
    print(f"   escolhidas no total ({len(urls)}):")
    for u in urls:
        print("    -", u)
    if not urls:
        print("   !! Nenhuma página candidata. Sem palavra-chave em fontes_oficiais.json e o buscador não devolveu nada.")

    print("\n== 2) Leitura das páginas (linhas [VERIFICADOR] = download)")
    trechos = []
    for u in urls:
        t = V.obter_trecho(u, args.pergunta)
        doc = V._cache_paginas.get(u, (0, None))[1]
        if t:
            trechos.append(t)
            print(f"   OK  {len(t['texto']):>5} caracteres úteis | página com {len(doc[1])} blocos de texto | {u}")
        elif doc:
            print(f"   --  abriu ({len(doc[1])} blocos), mas nada relevante para a pergunta | {u}")
        else:
            print(f"   XX  NÃO abriu / sem texto (bloqueio, erro HTTP, página montada por JavaScript ou tipo inesperado) | {u}")

    print(f"\n== 3) Trechos que iriam para a comparação: {len(trechos)}")
    for i, t in enumerate(trechos, 1):
        print(f"\n[W{i}] {t['site']} — {t['titulo']}\n{t['texto'][:600]}{'…' if len(t['texto']) > 600 else ''}")

    if args.llm:
        print("\n== 4) Comparação com o modelo")
        if not os.getenv("GROQ_API_KEY"):
            sys.exit("   defina GROQ_API_KEY no ambiente (.env) para usar --llm")
        from groq import Groq
        import rag
        rascunho = input("   Cole a resposta que o bot deu (ou Enter para usar um resumo vazio): ").strip() or "(sem rascunho)"
        r = V.verificar(Groq(api_key=os.environ["GROQ_API_KEY"]), rag.MODELO_VERIFICACAO,
                        rag._opcoes_modelo(rag.MODELO_VERIFICACAO, "low"), args.pergunta, args.pergunta, rascunho, [])
        print("   status:", r["status"], "| motivo:", r.get("motivo", ""))
    elif not trechos:
        print("\n>> Sem trechos, a conferência aparece no chat como 'não consegui ler os sites'.")
    else:
        print("\n>> Se o chat mostra 'não encontrei... confirme ou corrija', o problema está depois daqui: veja os "
              "logs do servidor ([VERIFICADOR] veredito/NÃO é JSON) ou rode com --llm.")


if __name__ == "__main__":
    main()
