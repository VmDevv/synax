"""Informativo "Resumo dos e-mails": o que chegou na caixa desde a última rodada.

Automação da aba Automações (tipo `resumo_emails`), como o resumo das
auditorias. A rodada:

1. **Lê a caixa antes** (espera a leitura em andamento terminar): o resumo das
   08h tem de incluir o e-mail das 07h59, e não o da última leitura de hora
   em hora.
2. Junta os e-mails lidos no período — pela hora em que o SISTEMA os leu
   (`created_at`), não pelo cabeçalho Date, que o remetente escreve como quer.
   E-mail do próprio Synax (os informativos) fica de fora.
3. A lista factual é montada aqui; o parágrafo é da IA (`ia.py`). O `detalhe`
   gravado na execução NÃO leva o corpo dos e-mails — ele fica para sempre no
   histórico, e o corpo só existe por `leitura.DIAS_TRECHO` dias.
"""
import json
import logging

from ...core.fuso import hora_local
from ...core.templating import build_env
from ...domain.models.auth_models import Tenant
from ...domain.models.email_recebido import EmailMensagem
from ..automacoes import janela as jan
from . import ia, remetentes

logger = logging.getLogger(__name__)

LIMITE_EMAIL = 60
ROTULO_SITUACAO = {
    "com_sped": "SPED recebido",
    "com_xml": "XML aproveitado",
    "xml_recusado": "XML não aproveitado",
    "outros_anexos": "com anexo",
    "sem_anexo": "",
    "grande": "grande demais, não baixado",
    "erro": "não pôde ser aberto",
}

_env = None


def _ambiente():
    global _env
    if _env is None:
        _env = build_env()
    return _env


def _email(m, quando, de_rotulo: str, sistema: str, contexto: dict) -> dict:
    return {"hora": f"{quando:%d/%m %H:%M}", "de": m.remetente or "",
            "de_rotulo": de_rotulo, "assunto": m.assunto or "(sem assunto)",
            "situacao": m.situacao, "situacao_rotulo": ROTULO_SITUACAO.get(m.situacao, ""),
            "xmls": m.xmls_aceitos, "recusados": m.xmls_recusados,
            "trecho": m.trecho or "", "sistema": sistema, "contexto": contexto,
            "empresa": contexto.get("empresa", "")}


def coletar(db, config: dict, inicio, fim) -> dict:
    mensagens = (db.query(EmailMensagem)
                 .filter(EmailMensagem.created_at >= inicio, EmailMensagem.created_at < fim,
                         EmailMensagem.situacao != "proprio")
                 .order_by(EmailMensagem.created_at).all())
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    regras = remetentes.carregar(db)

    emails, recusas, por_empresa, silenciados = [], [], {}, {}
    for m in mensagens:
        try:
            detalhe = json.loads(m.detalhe or "{}")
        except ValueError:
            detalhe = {}
        quando = hora_local(m.recebido_em or m.created_at)
        de_rotulo = (f"{m.remetente_nome} <{m.remetente}>" if m.remetente_nome
                     else (m.remetente or "(sem remetente)"))
        sistema = ""
        aceitos = detalhe.get("aceitos") or []
        for sp in detalhe.get("speds") or []:
            sistema += (f"SPED de {sp.get('empresa', '')} ({sp.get('competencia') or 'sem data'})"
                        + (" — já recebido antes" if sp.get("repetido")
                           else " guardado; os XMLs das entradas estão sendo baixados do SIEG")
                        + ". ")
        if aceitos or m.xmls_repetidos or m.xmls_recusados:
            empresas_do_email = sorted({a.get("empresa", "") for a in aceitos})
            sistema += (f"{m.xmls_aceitos} XML(s) guardado(s)"
                       + (f" de {', '.join(empresas_do_email)}" if empresas_do_email else "")
                       + (f", {m.xmls_repetidos} já recebido(s) antes" if m.xmls_repetidos else "")
                       + (f", {m.xmls_recusados} não aproveitado(s)" if m.xmls_recusados else ""))
        regra = remetentes.regra_para(m.remetente, regras)
        if regra is not None and regra.silenciar:
            # Não vai para a IA nem para a lista — vira contagem por remetente.
            # O XML que vier nele segue abaixo, aproveitado como qualquer outro.
            rotulo = regra.rotulo or regra.padrao
            silenciados[rotulo] = silenciados.get(rotulo, 0) + 1
        else:
            contexto = {}
            if regra is not None and regra.nota:
                contexto["quem_e"] = regra.nota
            if regra is not None and regra.tenant_id and regra.tenant_id in nomes:
                contexto["empresa"] = nomes[regra.tenant_id]
            if regra is not None and regra.rotulo:
                contexto["rotulo"] = regra.rotulo
            emails.append(_email(m, quando, de_rotulo, sistema, contexto))
        for a in aceitos:
            chave = (a.get("tenant_id") or a.get("empresa"), a.get("competencia") or "")
            g = por_empresa.setdefault(chave, {
                "empresa": nomes.get(a.get("tenant_id") or "", a.get("empresa", "")),
                "tenant_id": a.get("tenant_id", ""), "competencia": a.get("competencia") or "",
                "nfe": 0, "cte": 0})
            g["nfe" if a.get("modelo") == "NF-e" else "cte"] += 1
        for r in detalhe.get("recusas") or []:
            recusas.append({"hora": f"{quando:%d/%m %H:%M}", "de": de_rotulo,
                            "assunto": m.assunto or "(sem assunto)",
                            "arquivo": r.get("arquivo", ""), "documento": r.get("documento", ""),
                            "motivo": r.get("motivo", "")})
        if m.situacao in ("grande", "erro"):
            recusas.append({"hora": f"{quando:%d/%m %H:%M}", "de": de_rotulo,
                            "assunto": m.assunto or "(sem assunto)", "arquivo": "",
                            "documento": "", "motivo": ROTULO_SITUACAO[m.situacao]})

    xmls = sorted(por_empresa.values(), key=lambda g: (g["empresa"], g["competencia"]))
    n_silenciados = sum(silenciados.values())
    totais = {"emails": len(emails) + n_silenciados, "silenciados": n_silenciados,
              "com_xml": sum(1 for e in emails if e["xmls"]),
              "xmls": sum(g["nfe"] + g["cte"] for g in xmls),
              "empresas": len({g["tenant_id"] for g in xmls}), "recusados": len(recusas)}

    if config.get("usar_ia", True):
        texto, da_ia, motivo = ia.resumir(emails, totais)
    else:
        texto, da_ia, motivo = ia.resumo_local(totais), False, "desligado nesta automação"

    for e in emails:
        e.pop("trecho", None)        # o corpo não vai para o histórico
        e.pop("contexto", None)
    return {"tipo": "resumo_emails", "periodo": jan.rotulo(inicio, fim),
            "periodo_curto": jan.rotulo_curto(inicio, fim), "config": config,
            "texto": texto, "texto_ia": da_ia, "motivo_sem_ia": motivo,
            "emails": emails, "xmls": xmls, "recusas": recusas, "totais": totais,
            "silenciados": [{"rotulo": r, "vezes": n}
                            for r, n in sorted(silenciados.items(), key=lambda x: (-x[1], x[0]))]}


def _so_automaticos(t: dict) -> bool:
    """Tudo o que chegou foi silenciado e não trouxe XML nem recusa."""
    return (bool(t["emails"]) and t["emails"] == t.get("silenciados", 0)
            and not t["xmls"] and not t["recusados"])


def desfecho(dados: dict) -> tuple[str, str, bool]:
    """(status, mensagem, tem_novidade). Só e-mail silenciado não é novidade:
    a automação "só com novidade" não manda e-mail para dizer que o Nibo
    mandou o relatório do dia."""
    t = dados["totais"]
    if not t["emails"]:
        return "sem_novidade", "Nenhum e-mail no período.", False
    if _so_automaticos(t):
        return "sem_novidade", f"Só e-mails automáticos ({t['emails']}), silenciados.", False
    partes = [f"{t['emails']} e-mail(s)"]
    if t.get("silenciados"):
        partes.append(f"{t['silenciados']} automático(s) silenciado(s)")
    if t["xmls"]:
        partes.append(f"{t['xmls']} XML(s) de {t['empresas']} empresa(s)")
    if t["recusados"]:
        partes.append(f"{t['recusados']} anexo(s) não aproveitado(s)")
    return "ok", " · ".join(partes), True


def assunto(dados: dict) -> str:
    t = dados["totais"]
    base = f"[Synax] Resumo dos e-mails {dados['periodo_curto']}"
    if not t["emails"]:
        return f"{base} — nenhum e-mail no período"
    if _so_automaticos(t):
        return f"{base} — só e-mails automáticos"
    uteis = t["emails"] - t.get("silenciados", 0)
    partes = [f"{uteis} e-mail{'s' if uteis != 1 else ''}"]
    if t["xmls"]:
        partes.append(f"{t['xmls']} XML{'s' if t['xmls'] != 1 else ''}")
    if t["recusados"]:
        partes.append(f"{t['recusados']} não aproveitado{'s' if t['recusados'] != 1 else ''}")
    return f"{base} — " + " · ".join(partes)


def montar_email(dados: dict, nome_automacao: str) -> tuple[str, str, str]:
    contexto = {**dados, "nome": nome_automacao, "limite": LIMITE_EMAIL,
                "linhas": ia.linhas_para_tela(dados["texto"]),
                "emails_mostrados": dados["emails"][:LIMITE_EMAIL],
                "recusas_mostradas": dados["recusas"][:LIMITE_EMAIL]}
    html = _ambiente().get_template("emails/informativo_emails.html").render(contexto)
    return assunto(dados), html, _texto(contexto)


def _texto(d: dict) -> str:
    linhas = [f"Resumo dos e-mails — {d['nome']}", f"Período: {d['periodo']}", ""]
    linhas.append("RESUMO" + (" (escrito por IA)" if d["texto_ia"] else " (gerado pelo sistema)"))
    linhas += [f"   {l}" for l in d["texto"].splitlines()]
    linhas.append("")
    if d["xmls"]:
        linhas.append("XMLS RECEBIDOS")
        for g in d["xmls"]:
            linhas.append(f"   - {g['empresa']} · {g['competencia'] or 'sem data'}: "
                          f"{g['nfe']} NF-e, {g['cte']} CT-e")
        linhas.append("")
    if d["recusas_mostradas"]:
        linhas.append("NÃO APROVEITADOS")
        for r in d["recusas_mostradas"]:
            linhas.append(f"   - {r['hora']} · {r['de']} · {r['arquivo'] or r['assunto']}: {r['motivo']}")
        linhas.append("")
    if d.get("silenciados"):
        linhas.append("AUTOMÁTICOS (SILENCIADOS)")
        linhas.append("   " + " · ".join(f"{s['rotulo']} ×{s['vezes']}" for s in d["silenciados"]))
        linhas.append("")
    if d["emails_mostrados"]:
        linhas.append("E-MAILS DO PERÍODO")
        for e in d["emails_mostrados"]:
            linhas.append(f"   - {e['hora']} · {e['de_rotulo']} · {e['assunto']}"
                          + (f" [{e['situacao_rotulo']}]" if e["situacao_rotulo"] else ""))
    linhas += ["", "Detalhes na tela E-mails do Synax.",
               "Enviado automaticamente pelo Synax (Administração → Automações)."]
    return "\n".join(linhas)
