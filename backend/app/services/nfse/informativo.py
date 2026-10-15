"""O e-mail informativo da coleta automática de NFS-e (fase 3, 25/09/2026).

Sempre as três seções, nesta ordem (decisão do Sandro em 24/09/2026): o que
chegou, o que não foi consultado e os certificados que estão para vencer. A
terceira é a que evita a automação parar em silêncio — certificado vencido é o
jeito mais comum de uma empresa sair da coleta sem ninguém notar.

O assunto já diz o essencial, para quem só lê a caixa de entrada: quantas notas
chegaram e se houve problema. Uma rodada com falha NUNCA tem assunto que pareça
tudo certo.
"""
import re

from ...core.fuso import hora_local
from ...core.templating import build_env, moeda_br
from . import relatorio_coleta

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
MAX_DESTINATARIOS = 20

_env = None


def _ambiente():
    global _env
    if _env is None:
        _env = build_env()
    return _env


def ler_destinatarios(texto: str) -> tuple[list[str], list[str]]:
    """Separa por vírgula, ponto e vírgula, espaço ou linha. Devolve
    (válidos sem repetição, inválidos)."""
    validos, invalidos = [], []
    for bruto in re.split(r"[,;\s]+", texto or ""):
        email = bruto.strip()
        if not email:
            continue
        if _EMAIL.match(email):
            if email.lower() not in (v.lower() for v in validos):
                validos.append(email)
        else:
            invalidos.append(email)
    return validos, invalidos


def assunto(execucao) -> str:
    local = hora_local(execucao.inicio)
    partes = [f"{execucao.notas_novas} nota{'s' if execucao.notas_novas != 1 else ''} "
              f"nova{'s' if execucao.notas_novas != 1 else ''}"]
    if execucao.status == "vazia":
        partes = ["nenhuma empresa consultada"]
    if execucao.empresas_falha:
        partes.append(f"{execucao.empresas_falha} falha{'s' if execucao.empresas_falha > 1 else ''}")
    if execucao.empresas_adiadas:
        partes.append(f"{execucao.empresas_adiadas} adiada{'s' if execucao.empresas_adiadas > 1 else ''}")
    if execucao.certificados_vencendo:
        partes.append(f"{execucao.certificados_vencendo} certificado"
                      f"{'s' if execucao.certificados_vencendo > 1 else ''} vencendo")
    alerta = "⚠ " if (execucao.empresas_falha or execucao.status in ("falhou", "vazia")) else ""
    return f"{alerta}[Synax] NFS-e {local:%d/%m %H:%M} — " + " · ".join(partes)


def _dados(db, execucao, nome_automacao: str, rotulo_status: str) -> dict:
    detalhe = relatorio_coleta._detalhe(execucao)
    empresas = detalhe.get("empresas", [])
    valores = relatorio_coleta.valores_por_empresa(db, execucao)
    com_notas = []
    for e in empresas:
        if e.get("novos") or e.get("atualizados"):
            v = valores.get(e.get("tenant_id"), {})
            com_notas.append({**e, "valor": v.get("valor", 0.0), "retencoes": v.get("retencoes", 0.0)})
    return {
        "e": execucao,
        "nome": nome_automacao,
        "rotulo_status": rotulo_status,
        "inicio_local": hora_local(execucao.inicio),
        "com_notas": com_notas,
        "falhas": [e for e in empresas if e.get("erro")],
        "sem_novidade": sum(1 for e in empresas if not e.get("erro")
                            and not e.get("novos") and not e.get("atualizados")),
        "certificados": detalhe.get("certificados_vencendo", []),
        "total_valor": sum(v["valor"] for v in valores.values()),
        "total_retencoes": sum(v["retencoes"] for v in valores.values()),
        "tipos": relatorio_coleta.TIPOS_ERRO,
        "relatorio": execucao.relatorio,
    }


def montar(db, execucao, nome_automacao: str, rotulo_status: str,
           com_anexo: bool = True) -> tuple[str, str, str]:
    """(assunto, html, texto) do informativo. `com_anexo=False` quando a
    planilha não pôde ser montada — o texto não promete o que não vai junto."""
    d = _dados(db, execucao, nome_automacao, rotulo_status)
    d["com_anexo"] = com_anexo
    html = _ambiente().get_template("emails/informativo_nfse.html").render(d)
    return assunto(execucao), html, _texto(d)


def _texto(d: dict) -> str:
    """Versão sem HTML, para cliente de e-mail que não mostra o HTML."""
    e = d["e"]
    linhas = [
        f"Coleta de NFS-e — {d['nome']}",
        f"{d['inicio_local']:%d/%m/%Y %H:%M} · {d['rotulo_status']}",
        e.mensagem or "",
        "",
        "1. NOTAS QUE CHEGARAM",
    ]
    if d["com_notas"]:
        for x in d["com_notas"]:
            linhas.append(f"   - {x['empresa']} ({x['cnpj']}): {x['novos']} nova(s), "
                          f"{x['atualizados']} atualizada(s) · R$ {moeda_br(x['valor'])} "
                          f"· retenções R$ {moeda_br(x['retencoes'])}")
        linhas.append(f"   Total: R$ {moeda_br(d['total_valor'])} · retenções "
                      f"R$ {moeda_br(d['total_retencoes'])}")
    else:
        linhas.append("   Nenhuma nota nova nesta coleta.")
    linhas += ["", "2. O QUE NÃO FOI CONSULTADO"]
    if d["falhas"]:
        for x in d["falhas"]:
            linhas.append(f"   - {x['empresa']} ({x['cnpj']}) — "
                          f"{d['tipos'].get(x.get('tipo_erro'), 'Falha')}: {x['erro']}")
    else:
        linhas.append("   Todas as empresas foram consultadas.")
    linhas += ["", "3. CERTIFICADOS VENCENDO EM ATÉ 30 DIAS"]
    if d["certificados"]:
        for c in d["certificados"]:
            v = c.get("validade") or ""
            prazo = (f"vencido há {-c['dias']} dia(s)" if c.get("vencido")
                     else f"vence em {c['dias']} dia(s)")
            linhas.append(f"   - {c['empresa']} ({c['cnpj']}): {v[8:10]}/{v[5:7]}/{v[:4]} — {prazo}")
    else:
        linhas.append("   Nenhum certificado vence nos próximos 30 dias.")
    if d["sem_novidade"]:
        linhas += ["", f"{d['sem_novidade']} empresa(s) consultada(s) sem nota nova."]
    anexo = ("A planilha completa segue em anexo." if d.get("com_anexo", True)
             else "A planilha não pôde ser gerada nesta coleta (veja o log do servidor).")
    linhas += ["", anexo
               + (f" Cópia na pasta das NFS-e: {d['relatorio']}" if d["relatorio"] else ""),
               "Enviado automaticamente pelo Synax (Administração → Automações)."]
    return "\n".join(linhas)
