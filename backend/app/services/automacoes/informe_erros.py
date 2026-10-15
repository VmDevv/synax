"""Informativo: erros do sistema no período (25/09/2026).

Mesmas duas fontes da aba "Erros do Sistema" (services/erros_sistema.py), e
pelo mesmo motivo: os relatórios `.md` do diagnóstico são os bons — resumo em
uma frase e gravidade —, mas nem todo erro vira relatório (limite de um
diagnóstico a cada 15 minutos por erro, e nenhum quando falta a chave de IA).
O log cru cobre o resto. Mandar só os relatórios diria "nenhum erro"
justamente nos dias em que o diagnóstico estava desligado.

As datas dos dois vêm no horário do servidor (Brasília); a janela da rodada
é UTC, e é convertida aqui.

O log cru é AGRUPADO por origem e mensagem: "a mesma falha 40 vezes" é uma
linha com a contagem, não quarenta linhas que escondem a segunda falha.
"""
import logging
import unicodedata

from ...core.fuso import hora_local
from ...core.templating import build_env
from .. import erros_sistema
from . import janela as jan
from .tipos import GRAVIDADES

logger = logging.getLogger(__name__)

LIMITE_EMAIL = 30
_NIVEL = {"alta": 3, "critica": 3, "media": 2, "baixa": 1}
_MINIMO = {"todas": 0, "media": 2, "alta": 3}

_env = None


def _ambiente():
    global _env
    if _env is None:
        _env = build_env()
    return _env


def nivel(gravidade: str) -> int:
    """ "Alta" -> 3, "Média" -> 2, "Baixa" -> 1, sem gravidade -> 0."""
    chave = (gravidade or "").strip().split(" ")[0].lower().strip(".:,")
    chave = unicodedata.normalize("NFD", chave).encode("ascii", "ignore").decode()
    return _NIVEL.get(chave, 0)


def coletar(config: dict, inicio, fim) -> dict:
    ini, fim_local = hora_local(inicio), hora_local(fim)
    minimo = _MINIMO.get(config.get("gravidade", "todas"), 0)

    relatorios = []
    for r in erros_sistema.listar(limite=100000):
        if not (ini <= r["quando"] < fim_local):
            continue
        n = nivel(r.get("gravidade"))
        # Sem gravidade entra só em "todas": filtrar por "média e alta" e
        # receber um relatório que ninguém classificou seria ruído.
        if minimo and n < minimo:
            continue
        relatorios.append({"quando": f"{r['quando']:%d/%m/%Y %H:%M}", "origem": r["origem"],
                           "tipo": r["tipo"], "gravidade": r.get("gravidade") or "",
                           "nivel": n, "resumo": r.get("resumo") or "", "arquivo": r["arquivo"]})

    grupos, total_log = {}, 0
    if config.get("incluir_log", True):
        for e in erros_sistema.erros_do_log(limite=1000000):
            if not (ini <= e["quando"] < fim_local):
                continue
            total_log += 1
            chave = (e["origem"], e["mensagem"][:120])
            g = grupos.setdefault(chave, {"origem": e["origem"], "mensagem": e["mensagem"],
                                          "nivel": e["nivel"], "vezes": 0,
                                          "primeira": e["quando"], "ultima": e["quando"]})
            g["vezes"] += 1
            g["primeira"] = min(g["primeira"], e["quando"])
            g["ultima"] = max(g["ultima"], e["quando"])
    log = sorted(grupos.values(), key=lambda g: (-g["vezes"], g["origem"]))
    for g in log:
        g["primeira"] = f"{g['primeira']:%d/%m %H:%M}"
        g["ultima"] = f"{g['ultima']:%d/%m %H:%M}"

    return {"tipo": "erros_sistema", "periodo": jan.rotulo(inicio, fim),
            "periodo_curto": jan.rotulo_curto(inicio, fim), "config": config,
            "gravidade_rotulo": GRAVIDADES.get(config.get("gravidade", "todas"), "Todas"),
            "relatorios": relatorios, "log": log,
            "totais": {"relatorios": len(relatorios),
                       "altas": sum(1 for r in relatorios if r["nivel"] >= 3),
                       "ocorrencias_log": total_log, "tipos_log": len(log)}}


def desfecho(dados: dict) -> tuple[str, str, bool]:
    """(status, mensagem, tem_novidade)."""
    t = dados["totais"]
    if not t["relatorios"] and not t["ocorrencias_log"]:
        return "sem_novidade", "Nenhum erro no período.", False
    partes = []
    if t["relatorios"]:
        partes.append(f"{t['relatorios']} erro{'s' if t['relatorios'] != 1 else ''} "
                      f"diagnosticado{'s' if t['relatorios'] != 1 else ''}"
                      + (f" ({t['altas']} de gravidade alta)" if t["altas"] else ""))
    if t["ocorrencias_log"]:
        partes.append(f"{t['ocorrencias_log']} ocorrência{'s' if t['ocorrencias_log'] != 1 else ''} "
                      f"no log ({t['tipos_log']} tipo{'s' if t['tipos_log'] != 1 else ''})")
    return "ok", " · ".join(partes), True


def assunto(dados: dict) -> str:
    t = dados["totais"]
    if not t["relatorios"] and not t["ocorrencias_log"]:
        return f"[Synax] Erros do sistema {dados['periodo_curto']} — nenhum erro no período"
    partes = []
    if t["relatorios"]:
        partes.append(f"{t['relatorios']} diagnosticado{'s' if t['relatorios'] != 1 else ''}")
    if t["ocorrencias_log"]:
        partes.append(f"{t['ocorrencias_log']} no log")
    return f"⚠ [Synax] Erros do sistema {dados['periodo_curto']} — " + " · ".join(partes)


def montar_email(dados: dict, nome_automacao: str) -> tuple[str, str, str]:
    contexto = {**dados, "nome": nome_automacao, "limite": LIMITE_EMAIL,
                "relatorios_mostrados": dados["relatorios"][:LIMITE_EMAIL],
                "log_mostrado": dados["log"][:LIMITE_EMAIL]}
    html = _ambiente().get_template("emails/informativo_erros.html").render(contexto)
    return assunto(dados), html, _texto(contexto)


def _texto(d: dict) -> str:
    t = d["totais"]
    linhas = [f"Erros do sistema — {d['nome']}", f"Período: {d['periodo']}", ""]
    if not t["relatorios"] and not t["ocorrencias_log"]:
        linhas.append("Nenhum erro no período.")
    if d["relatorios_mostrados"]:
        linhas.append("DIAGNOSTICADOS")
        for r in d["relatorios_mostrados"]:
            linhas.append(f"   - {r['quando']} · {r['origem']} · {r['tipo']}"
                          + (f" · {r['gravidade']}" if r["gravidade"] else "")
                          + f": {r['resumo']}")
        linhas.append("")
    if d["log_mostrado"]:
        linhas.append("NO LOG (agrupado)")
        for g in d["log_mostrado"]:
            linhas.append(f"   - {g['vezes']}x {g['origem']}: {g['mensagem']} (última: {g['ultima']})")
    linhas += ["", "Detalhes em Administração → Erros do Sistema.",
               "Enviado automaticamente pelo Synax (Administração → Automações)."]
    return "\n".join(linhas)
