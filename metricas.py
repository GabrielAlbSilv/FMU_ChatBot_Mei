"""Métricas de qualidade do FiscOn (em memória) e registro do feedback 👍/👎 das respostas.

Para que serve: saber, com números, se o bot está melhorando — quantas respostas foram conferidas
pelos sites, quantas o verificador trocou ("atualizada"), quantas as regras fiscais corrigiram ou
completaram, o tempo de resposta e o que as pessoas acharam (feedback).

Onde ver:
  - GET /api/status com o cabeçalho X-Token (STATUS_TOKEN ou BASE_WEB_TOKEN): campo "metricas";
  - logs do servidor (Render > Logs): uma linha "[METRICA] {...}" por resposta e "[FEEDBACK] {...}" por
    voto, em JSON — dá para filtrar e copiar para uma planilha.
  - com DATABASE_URL (banco.py, ex.: Neon gratuito): cada resposta e cada voto também vão para o banco,
    em segundo plano, e o painel /painel mostra o histórico mesmo depois de o Render reiniciar.
Sem banco, os contadores daqui recomeçam quando o servidor reinicia (o disco do plano gratuito do
Render não é permanente) e o histórico fica só nos logs.

Privacidade: a pergunta guardada para o feedback já vem mascarada (privacidade.py) e só fica em
memória (últimas 500 respostas).
"""
import json
import threading
import time
from collections import Counter, OrderedDict, deque

import banco
import privacidade

MAX_RESPOSTAS = 500        # respostas lembradas para ligar o voto à resposta
_trava = threading.Lock()
_contadores = Counter()
_tempos = deque(maxlen=300)   # milissegundos das últimas respostas
_respostas = OrderedDict()    # id -> resumo da resposta
_inicio = time.time()


def _log(rotulo, dados):
    print(f"[{rotulo}] {json.dumps(dados, ensure_ascii=False, default=str)}", flush=True)


def registrar_resposta(id_resposta, pergunta, resultado, ms, versao=None):
    """Conta uma resposta entregue e guarda o resumo dela para um possível voto (e no banco, se houver)."""
    verificacao = (resultado.get("verificacao") or {}).get("status") or "nao_aplicavel"
    ajuste = resultado.get("ajuste_regras") or "nenhum"
    temas = [f.get("tema") for f in resultado.get("fatos") or []]
    resumo = {"id": id_resposta, "ms": int(ms), "verificacao": verificacao, "ajuste_regras": ajuste,
              "substituida": bool(resultado.get("substituida")), "cache": bool(resultado.get("cache")),
              "modelo_reserva": bool(resultado.get("modelo_reserva")),
              "temas_regras": temas, "fontes": len(resultado.get("fontes") or []),
              "paginas_oficiais": len(resultado.get("paginas_oficiais") or [])}
    completo = dict(resumo, pergunta=privacidade.mascarar(pergunta)[:300],
                    resposta=str(resultado.get("resposta") or "")[:400], versao=versao)
    with _trava:
        _contadores["respostas"] += 1
        _contadores[f"verificacao:{verificacao}"] += 1
        _contadores[f"ajuste:{ajuste}"] += 1
        if resumo["cache"]:
            _contadores["cache"] += 1
        if resumo["modelo_reserva"]:
            _contadores["modelo_reserva"] += 1
        for t in temas:
            _contadores[f"regra:{t}"] += 1
        _tempos.append(int(ms))
        _respostas[id_resposta] = dict(completo, em=time.time())
        while len(_respostas) > MAX_RESPOSTAS:
            _respostas.popitem(last=False)
    _log("METRICA", resumo)
    banco.salvar_resposta(completo)


def registrar_erro(tipo):
    with _trava:
        _contadores["erros"] += 1
        _contadores[f"erro:{tipo}"] += 1


VOTOS = ("util", "nao_util")


def registrar_feedback(id_resposta, voto, comentario=""):
    """Guarda o voto de uma resposta. Devolve False se o voto ou a resposta não forem válidos.

    Com banco: o voto é gravado lá também — inclusive de respostas que o servidor já esqueceu
    (ex.: a pessoa votou depois de um reinício do Render).
    """
    if voto not in VOTOS:
        return False
    comentario = privacidade.mascarar(str(comentario or ""))[:500]
    with _trava:
        resposta = _respostas.get(id_resposta)
        if not resposta:
            if banco.ativo():
                _contadores[f"feedback:{voto}"] += 1
            else:
                return False
    if not resposta:
        banco.salvar_voto(id_resposta, voto, comentario)
        _log("FEEDBACK", {"id": id_resposta, "voto": voto, "comentario": comentario, "so_no_banco": True})
        return True
    with _trava:
        if resposta.get("voto"):            # um voto por resposta (troca de ideia vale o último)
            _contadores[f"feedback:{resposta['voto']}"] -= 1
        resposta["voto"] = voto
        _contadores[f"feedback:{voto}"] += 1
        dados = {k: resposta.get(k) for k in ("id", "verificacao", "ajuste_regras", "substituida", "temas_regras",
                                             "pergunta", "resposta")}
    dados.update(voto=voto, comentario=comentario)
    _log("FEEDBACK", dados)
    banco.salvar_voto(id_resposta, voto, comentario)
    return True


def resumo():
    """Números para o painel técnico (/api/status com token)."""
    with _trava:
        c = dict(_contadores)
        tempos = sorted(_tempos)
    total = c.get("respostas", 0)

    def taxa(chave):
        return round(100 * c.get(chave, 0) / total, 1) if total else 0.0

    votos = c.get("feedback:util", 0) + c.get("feedback:nao_util", 0)
    return {
        "desde": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_inicio)),
        "respostas": total,
        "erros": c.get("erros", 0),
        "tempo_ms": {"mediana": tempos[len(tempos) // 2] if tempos else None,
                     "p90": tempos[int(len(tempos) * 0.9)] if tempos else None},
        "percentual": {"conferida_confirmada": taxa("verificacao:confirmado"),
                       "atualizada_pelos_sites": taxa("verificacao:atualizada"),
                       "corrigida_pelas_regras": taxa("ajuste:corrigida"),
                       "complementada_pelas_regras": taxa("ajuste:complementada"),
                       "modelo_reserva": taxa("modelo_reserva"),
                       "do_cache": taxa("cache")},
        "feedback": {"util": c.get("feedback:util", 0), "nao_util": c.get("feedback:nao_util", 0),
                     "aprovacao": round(100 * c.get("feedback:util", 0) / votos, 1) if votos else None},
        "contadores": c,
    }


def limpar():
    """Zera tudo (usado nos testes)."""
    with _trava:
        _contadores.clear()
        _tempos.clear()
        _respostas.clear()
