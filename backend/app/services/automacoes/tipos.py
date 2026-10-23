"""Os tipos de automação e o que cada um aceita de configuração.

Fonte única para a tela (menu "Nova automação", campos do formulário, texto do
cartão) e para o agendador (qual configuração vale numa rodada). A
configuração de cada automação fica em `config` (JSON); o que faltar lá vem do
padrão daqui — automação gravada antes de uma opção existir continua rodando.
"""
import json

COLETA = "coleta_nfse"
RESUMO = "resumo_auditorias"
ERROS = "erros_sistema"
EMAILS = "resumo_emails"
VALIDAR = "validar_sped"

TIPOS = {
    COLETA: {
        "rotulo": "Coleta de NFS-e",
        "icone": "fa-file-invoice",
        "descricao": "Baixa as NFS-e do ADN e manda o informativo das notas que chegaram.",
        # Mensal não: o ADN guarda as notas por pouco tempo, e uma coleta por
        # mês deixaria a empresa semanas sem saber o que chegou.
        "frequencias": ("diaria", "semanal"),
        "email_obrigatorio": False,
    },
    RESUMO: {
        "rotulo": "Resumo das auditorias",
        "icone": "fa-chart-column",
        "descricao": "Auditorias do período, inconsistências e valor em jogo, por empresa.",
        "frequencias": ("diaria", "semanal", "mensal"),
        # Informativo sem destinatário não faz nada: só a coleta trabalha
        # sozinha (baixa as notas) mesmo sem e-mail.
        "email_obrigatorio": True,
    },
    ERROS: {
        "rotulo": "Erros do sistema",
        "icone": "fa-bug",
        "descricao": "O que a aba Erros do Sistema registrou no período.",
        "frequencias": ("diaria", "semanal", "mensal"),
        "email_obrigatorio": True,
    },
    EMAILS: {
        "rotulo": "Resumo dos e-mails",
        "icone": "fa-envelope-open-text",
        "descricao": "O que chegou na caixa do escritório, resumido por IA, e os XMLs recebidos.",
        # Mensal não: é o resumo "do que chegou de ontem para hoje" — um mês de
        # e-mails não cabe num resumo que alguém leia.
        "frequencias": ("diaria", "semanal"),
        "email_obrigatorio": True,
    },
    # 23/10/2026: a pasta onde o Domínio grava os SPED, validada e assinada no PVA (services/automacoes/
    # validar_sped.py). Trabalha sozinha mesmo sem e-mail: o resultado fica no histórico e na aba Validar SPED.
    VALIDAR: {
        "rotulo": "Validar e assinar SPED",
        "icone": "fa-file-signature",
        "descricao": "Valida no PVA os SPED de uma pasta e assina com o e-CNPJ os que passarem.",
        "frequencias": ("diaria", "semanal", "mensal"),
        "email_obrigatorio": False,
    },
}

PERIODOS_RESUMO = {
    "desde_ultima": "Desde a última rodada",
    "7d": "Últimos 7 dias",
    "15d": "Últimos 15 dias",
    "30d": "Últimos 30 dias",
    "mes_anterior": "Mês anterior (fechado)",
    "mes_atual": "Mês atual até agora",
}

PERIODOS_EMAILS = {
    "desde_ultima": "Desde a última rodada",
    "24h": "Últimas 24 horas",
}

PERIODOS_ERROS = {
    "desde_ultima": "Desde a última rodada",
    "24h": "Últimas 24 horas",
    "7d": "Últimos 7 dias",
}

GRAVIDADES = {
    "todas": "Todas",
    "media": "Média e alta",
    "alta": "Só alta",
}

_PADRAO = {
    COLETA: {},
    # O resumo de gestão vai mesmo vazio: "nenhuma auditoria na semana" também
    # é informação para quem acompanha o fechamento.
    RESUMO: {"periodo": "7d", "ultima_por_competencia": True, "so_com_novidade": False},
    # O de erros, não: e-mail diário dizendo "nada" vira ruído, e a pessoa para
    # de ler justamente o dia em que houver erro.
    ERROS: {"periodo": "desde_ultima", "gravidade": "todas", "incluir_log": True,
            "so_com_novidade": True},
    # Envia mesmo sem e-mail: "nada chegou" numa manhã útil é, em si, estranho.
    EMAILS: {"periodo": "desde_ultima", "usar_ia": True, "so_com_novidade": False},
    # Transmitir não é opção: o envio à Receita não tem volta e não está ligado no Synax.
    VALIDAR: {"pasta": "", "subpastas": False, "destino": "", "assinar": True},
}


def valido(tipo: str) -> bool:
    return tipo in TIPOS


def frequencias(tipo: str) -> tuple:
    return TIPOS.get(tipo, TIPOS[COLETA])["frequencias"]


def padrao(tipo: str) -> dict:
    return dict(_PADRAO.get(tipo, {}))


def ler_config(automacao) -> dict:
    """A configuração gravada, completada pelo padrão do tipo."""
    tipo = getattr(automacao, "tipo", None) or COLETA
    config = padrao(tipo)
    try:
        gravada = json.loads(getattr(automacao, "config", None) or "{}")
    except ValueError:
        gravada = {}
    if isinstance(gravada, dict):
        config.update({k: v for k, v in gravada.items() if k in config})
    return config


def config_do_formulario(tipo: str, form: dict) -> tuple[dict, str]:
    """(config, erro). `form` traz os campos crus do formulário."""
    config = padrao(tipo)
    if tipo == RESUMO:
        periodo = str(form.get("periodo") or "")
        if periodo not in PERIODOS_RESUMO:
            return config, "Escolha o período que o resumo cobre."
        config["periodo"] = periodo
        config["ultima_por_competencia"] = bool(form.get("ultima_por_competencia"))
        config["so_com_novidade"] = bool(form.get("so_com_novidade"))
    elif tipo == ERROS:
        periodo = str(form.get("periodo") or "")
        if periodo not in PERIODOS_ERROS:
            return config, "Escolha o período que o informe de erros cobre."
        gravidade = str(form.get("gravidade") or "")
        if gravidade not in GRAVIDADES:
            return config, "Escolha a gravidade mínima dos erros."
        config["periodo"] = periodo
        config["gravidade"] = gravidade
        config["incluir_log"] = bool(form.get("incluir_log"))
        config["so_com_novidade"] = bool(form.get("so_com_novidade"))
    elif tipo == EMAILS:
        periodo = str(form.get("periodo") or "")
        if periodo not in PERIODOS_EMAILS:
            return config, "Escolha o período que o resumo dos e-mails cobre."
        config["periodo"] = periodo
        config["usar_ia"] = bool(form.get("usar_ia"))
        config["so_com_novidade"] = bool(form.get("so_com_novidade"))
    elif tipo == VALIDAR:
        import os

        pasta = str(form.get("pasta") or "").strip().strip('"')
        destino = str(form.get("destino") or "").strip().strip('"')
        config.update(pasta=pasta, destino=destino, subpastas=bool(form.get("subpastas")),
                      assinar=bool(form.get("assinar")))
        if not pasta:
            return config, "Informe a pasta onde os SPED chegam (a que o Domínio grava)."
        if not os.path.isdir(pasta):
            return config, f"O servidor não encontrou a pasta {pasta}. Confira o caminho (pasta da rede: \\\\servidor\\pasta)."
        if destino and os.path.normcase(os.path.abspath(destino)) == os.path.normcase(os.path.abspath(pasta)):
            return config, "A pasta dos assinados tem de ser outra: na mesma pasta, o assinado ficaria por cima do original."
    return config, ""


def descrever(tipo: str, config: dict) -> str:
    """Uma linha para o cartão da automação."""
    if tipo == RESUMO:
        partes = [PERIODOS_RESUMO.get(config.get("periodo"), "")]
        if config.get("ultima_por_competencia"):
            partes.append("última auditoria de cada empresa e competência")
        if config.get("so_com_novidade"):
            partes.append("só quando houver auditoria")
        return " · ".join(p for p in partes if p)
    if tipo == ERROS:
        partes = [PERIODOS_ERROS.get(config.get("periodo"), ""),
                  "gravidade: " + GRAVIDADES.get(config.get("gravidade"), "todas").lower()]
        if not config.get("incluir_log"):
            partes.append("sem o log cru")
        partes.append("só quando houver erro" if config.get("so_com_novidade")
                      else "envia mesmo sem erro")
        return " · ".join(p for p in partes if p)
    if tipo == VALIDAR:
        partes = [config.get("pasta") or "(sem pasta)",
                  "valida e assina" if config.get("assinar") else "só valida",
                  "com subpastas" if config.get("subpastas") else ""]
        return " · ".join(p for p in partes if p)
    if tipo == EMAILS:
        partes = [PERIODOS_EMAILS.get(config.get("periodo"), ""),
                  "resumo por IA" if config.get("usar_ia") else "sem IA",
                  "só quando chegar e-mail" if config.get("so_com_novidade")
                  else "envia mesmo sem e-mail"]
        return " · ".join(p for p in partes if p)
    return ""
