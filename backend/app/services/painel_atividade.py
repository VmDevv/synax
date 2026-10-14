"""Os números do Radar de Atividade: quem trabalhou, onde, e o que ficou parado.

A aba do Radar mostrava só a lista crua das últimas auditorias. Dá para ver o
que aconteceu ontem; não dá para responder "quem está produzindo", "qual empresa
está dando mais trabalho" nem "quanta curadoria está represada". Este módulo
calcula essas respostas, e a tela só desenha.

Três decisões que valem mais que o código:

1. **result_data NUNCA é lido aqui.** Ele guarda o relatório inteiro de uma
   auditoria — todas as divergências, todos os itens — e passa fácil de alguns
   MB. Somar as inconsistências de 200 auditorias abrindo esse JSON seria baixar
   centenas de MB a cada abertura do painel. Os dois números que o painel usa
   ficam em colunas próprias de `audit_tasks` (`notas_processadas`,
   `inconsistencias`), gravadas pelo motor ao concluir. As consultas daqui
   selecionam coluna por coluna, e result_data não está em nenhuma delas.

2. **Inconsistência por empresa sai da auditoria MAIS RECENTE, não da soma.**
   Rodar a mesma competência três vezes é rotina — o operador corrige regra e
   roda de novo. Somando, a empresa que exigiu mais retrabalho apareceria como a
   mais problemática justamente por já ter sido corrigida. O painel mostra a
   última foto de cada empresa, que é o estado de hoje, e ao lado a taxa por 100
   notas: 40 inconsistências em 2.000 notas é outra história que 40 em 60.

3. **Auditoria e curadoria são contadas por pessoa em tabelas SEPARADAS,
   porque o sistema identifica as duas coisas de formas diferentes.** A
   auditoria grava `user_id` (o UUID da conta, que resolve para o nome); a regra
   grava `created_by`, que na maior parte das tabelas é o e-mail de login e não
   está em lugar nenhum do banco — o e-mail mora no Supabase Auth. Juntar as
   duas listas exigiria adivinhar que fulano@x.com é o mesmo UUID; adivinhar
   identidade para deixar a tela mais bonita é o tipo de presunção silenciosa
   que este sistema já pagou caro. Duas tabelas, cada uma dizendo por que
   critério identifica.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, func, literal, select, union_all

from ..core import competencia as comp
from ..core.fuso import hora_local
from ..domain.models.audit import AuditTask
from ..domain.models.auth_models import Tenant, UserProfile
from ..domain.models.rules import (
    CfopClassification,
    CfopCreditRule,
    CteCfopClassification,
    GeradorCfopRegra,
    GeradorCteCfopRegra,
    IcmsAnticipationRule,
    IpiCreditRule,
    ItemMatchRule,
    PisCofinsCreditRule,
)

DIAS_PADRAO = 30
PERIODOS = (7, 30, 90)
TOPO = 5

# Régua do heatmap de competências. 12 meses cobrem o ano fiscal inteiro e
# ainda cabem na largura da tela sem rolagem horizontal.
JANELA_COMPETENCIAS = 12
# Linhas desenhadas. Com 170 empresas, a tabela inteira vira papel de parede —
# o que se quer ver são as que estão em aberto, e elas vêm ordenadas primeiro.
LINHAS_HEATMAP = 20

# As tabelas de regra que passam por maker-checker, com o nome que o usuário lê.
# `CestMvaRule` fica de fora de propósito: é a Portaria 16, dado legal carregado
# por script, não trabalho de curadoria de ninguém.
TABELAS_DE_REGRA = (
    (CfopClassification, "CFOP de entrada"),
    (CteCfopClassification, "CFOP de CT-e"),
    (CfopCreditRule, "Crédito de ICMS"),
    (IpiCreditRule, "Crédito de IPI"),
    (PisCofinsCreditRule, "PIS/COFINS"),
    (IcmsAnticipationRule, "Antecipação"),
    (ItemMatchRule, "Itens SPED×XML"),
    # As do gerador de SPED (09/10/2026) moram em tabelas próprias, mas passam
    # pela mesma fila — e pendente ali segura o arquivo de um cliente.
    (GeradorCfopRegra, "Gerador · CFOP NF-e"),
    (GeradorCteCfopRegra, "Gerador · CFOP CT-e"),
)

STATUS_CONCLUIDO = "concluido"
STATUS_ERRO = "erro"


def _agora_local() -> datetime:
    return hora_local(datetime.utcnow())


def _pct(parte: float, total: float) -> float:
    return round(parte * 100.0 / total, 1) if total else 0.0


# ---------------------------------------------------------------- consultas

def _tarefas_do_periodo(db, corte: datetime) -> list:
    """As auditorias do período — SEM result_data.

    O `select()` nomeia coluna por coluna justamente para que o relatório
    completo não venha junto. Um `db.query(AuditTask)` inocente aqui traria
    todos os result_data do período pela rede.
    """
    return db.execute(
        select(
            AuditTask.tenant_id,
            AuditTask.user_id,
            AuditTask.status,
            AuditTask.created_at,
            AuditTask.notas_processadas,
            AuditTask.inconsistencias,
        ).where(AuditTask.created_at >= corte)
    ).all()


def _uniao_de_regras(colunas_extra, filtro_status=None, desde=None):
    """UNION ALL das sete tabelas de regra: uma ida ao banco em vez de sete.

    `colunas_extra` recebe uma função que, dado o model, devolve a coluna de
    agrupamento (tenant_id ou created_by).
    """
    partes = []
    for modelo, rotulo in TABELAS_DE_REGRA:
        stmt = select(
            colunas_extra(modelo).label("chave"),
            literal(rotulo).label("tipo"),
            func.count().label("qtd"),
        )
        if filtro_status:
            stmt = stmt.where(modelo.status == filtro_status)
        if desde is not None:
            stmt = stmt.where(modelo.created_at >= desde)
        partes.append(stmt.group_by(colunas_extra(modelo)))
    return union_all(*partes)


def _regras_pendentes_por_empresa(db) -> dict:
    """{tenant_id: {"total": n, "por_tipo": {tipo: n}}} — o represado da Curadoria.

    Sem recorte de data: pendência de três meses atrás continua pendente, e é
    justamente a que ninguém está vendo.
    """
    linhas = db.execute(
        _uniao_de_regras(lambda m: m.tenant_id, filtro_status="pendente_revisao")
    ).all()

    saida = defaultdict(lambda: {"total": 0, "por_tipo": defaultdict(int)})
    for chave, tipo, qtd in linhas:
        alvo = saida[str(chave or "")]
        alvo["total"] += int(qtd or 0)
        alvo["por_tipo"][tipo] += int(qtd or 0)
    return saida


def _regras_criadas_por_autor(db, corte: datetime) -> dict:
    """{created_by: {"total": n, "por_tipo": {...}}} no período."""
    linhas = db.execute(
        _uniao_de_regras(lambda m: m.created_by, desde=corte)
    ).all()

    saida = defaultdict(lambda: {"total": 0, "por_tipo": defaultdict(int)})
    for chave, tipo, qtd in linhas:
        alvo = saida[str(chave or "")]
        alvo["total"] += int(qtd or 0)
        alvo["por_tipo"][tipo] += int(qtd or 0)
    return saida


# ------------------------------------------------------------------ blocos

def _serie_por_dia(tarefas, dias: int) -> list:
    """Uma barra por dia do período, inclusive nos dias sem movimento.

    Dia vazio precisa aparecer: um gráfico que só desenha os dias que tiveram
    auditoria mostra uma linha cheia e esconde exatamente o que interessa
    enxergar — a semana em que ninguém rodou nada.
    """
    hoje = _agora_local().date()
    balde = {hoje - timedelta(days=i): {"concluido": 0, "erro": 0, "outros": 0}
             for i in range(dias)}

    for _tenant, _user, status, criado, _notas, _incons in tarefas:
        dia = hora_local(criado).date() if criado else None
        if dia not in balde:
            continue
        if status == STATUS_CONCLUIDO:
            balde[dia]["concluido"] += 1
        elif status == STATUS_ERRO:
            balde[dia]["erro"] += 1
        else:
            balde[dia]["outros"] += 1

    serie = []
    for dia in sorted(balde):
        c = balde[dia]
        serie.append({
            "dia": dia,
            "rotulo": dia.strftime("%d/%m"),
            "dia_semana": dia.weekday(),          # 5 e 6 = fim de semana
            "concluido": c["concluido"],
            "erro": c["erro"],
            "outros": c["outros"],
            "total": c["concluido"] + c["erro"] + c["outros"],
        })
    return serie


def _uso_por_pessoa(tarefas, nomes) -> list:
    """Quem rodou auditoria no período. Identificada pelo user_id da conta."""
    por_pessoa = defaultdict(lambda: {
        "auditorias": 0, "concluidas": 0, "erros": 0,
        "notas": 0, "inconsistencias": 0, "empresas": set(), "ultima": None,
    })

    for tenant_id, user_id, status, criado, notas, incons in tarefas:
        p = por_pessoa[str(user_id or "")]
        p["auditorias"] += 1
        if status == STATUS_CONCLUIDO:
            p["concluidas"] += 1
        elif status == STATUS_ERRO:
            p["erros"] += 1
        p["notas"] += int(notas or 0)
        p["inconsistencias"] += int(incons or 0)
        if tenant_id:
            p["empresas"].add(str(tenant_id))
        if criado and (p["ultima"] is None or criado > p["ultima"]):
            p["ultima"] = criado

    linhas = []
    for uid, p in por_pessoa.items():
        linhas.append({
            "id": uid,
            "nome": nomes.get(uid) or ("Sistema / API" if not uid else "Conta removida"),
            "identificada": bool(nomes.get(uid)),
            "auditorias": p["auditorias"],
            "concluidas": p["concluidas"],
            "erros": p["erros"],
            "taxa_sucesso": _pct(p["concluidas"], p["concluidas"] + p["erros"]),
            "notas": p["notas"],
            "inconsistencias": p["inconsistencias"],
            "empresas": len(p["empresas"]),
            "ultima": hora_local(p["ultima"]),
        })
    linhas.sort(key=lambda r: (-r["auditorias"], r["nome"]))
    return linhas


def _curadoria_por_pessoa(autores: dict, nomes: dict) -> list:
    """Quem cadastrou regra no período. Identificada pelo created_by.

    `created_by` guarda o e-mail de login na maioria das tabelas e o UUID da
    conta em `item_match_rules`. O UUID a gente resolve para o nome; o e-mail
    aparece como está, porque ele não existe no banco — mora no Supabase Auth, e
    ir buscar lá seria uma chamada de rede por pessoa.
    """
    linhas = []
    for chave, dados in autores.items():
        if not chave:
            continue
        linhas.append({
            "chave": chave,
            "nome": nomes.get(chave) or chave,
            "por_conta": bool(nomes.get(chave)),
            "total": dados["total"],
            "por_tipo": dict(sorted(dados["por_tipo"].items(),
                                    key=lambda kv: -kv[1])),
        })
    linhas.sort(key=lambda r: (-r["total"], r["nome"]))
    return linhas


def _heatmap_competencias(linhas, tarefas, nomes: dict, ativas: set) -> dict:
    """Empresas × competências: uma linha por empresa, uma coluna por mês.

    É o único lugar do sistema que responde "quem está com mês em aberto" de
    relance. As regras de honestidade são as mesmas da ficha da empresa:

    - **mês corrente não é cobrado** (o SPED dele ainda nem venceu);
    - **empresa só é cobrada a partir da primeira competência que ela tem** —
      cliente que entrou em março não deve janeiro;
    - **empresa inativa não entra**: saiu da carteira, não deve mês nenhum, e
      apareceria no topo da lista de atrasadas;
    - **empresa sem competência gravada não vira empresa em dia**. Ela fica
      fora da tabela e é contada à parte, com o aviso do backfill: fingir que
      não há buraco é o mesmo erro de contar contador nulo como zero.
    """
    por_empresa = defaultdict(dict)
    for tenant_id, competencia_, notas, incons in linhas:
        chave = str(tenant_id or "")
        mes = comp.valida(competencia_)
        if not mes or chave not in ativas:
            continue
        por_empresa[chave][mes] = {
            "notas": notas,
            "inconsistencias": incons,
            "taxa": (round((incons or 0) * 100.0 / notas, 1)
                     if notas and incons is not None else None),
        }

    vistos = [m for meses in por_empresa.values() for m in meses]
    fim = max([comp.fechada()] + vistos)
    meses_regua = comp.sequencia(JANELA_COMPETENCIAS, ate=fim)
    cobravel_ate = comp.fechada()

    tabela = []
    for tid, meses in por_empresa.items():
        primeira = min(meses)
        celulas, em_aberto = [], 0
        for mes in meses_regua:
            dados = meses.get(mes)
            esperada = primeira <= mes <= cobravel_ate
            falta = esperada and not dados
            em_aberto += 1 if falta else 0
            celulas.append({
                "comp": mes,
                "rotulo": comp.rotulo(mes),
                "auditada": bool(dados),
                "esperada": esperada,
                "em_aberto": falta,
                "notas": dados["notas"] if dados else None,
                "inconsistencias": dados["inconsistencias"] if dados else None,
                "taxa": dados["taxa"] if dados else None,
            })
        tabela.append({
            "id": tid,
            "nome": nomes.get(tid, "Empresa excluída"),
            "celulas": celulas,
            "em_aberto": em_aberto,
            "ultima": max(meses),
            "ultima_rotulo": comp.rotulo(max(meses)),
        })

    tabela.sort(key=lambda r: (-r["em_aberto"], r["nome"].lower()))

    # Empresas que rodaram auditoria no período mas não têm competência em
    # lugar nenhum: é o histórico anterior a 03/09/2026 pedindo o backfill.
    com_auditoria = {str(t[0]) for t in tarefas
                     if t[2] == STATUS_CONCLUIDO and t[0] and str(t[0]) in ativas}
    sem_competencia = len(com_auditoria - set(por_empresa))

    return {
        "meses": [{"comp": m, "rotulo": comp.rotulo(m), "curto": comp.mes_curto(m),
                   "ano": m[:4], "virada": m.endswith("-01")} for m in meses_regua],
        "linhas": tabela[:LINHAS_HEATMAP],
        "empresas": len(tabela),
        "ocultas": max(len(tabela) - LINHAS_HEATMAP, 0),
        "empresas_em_aberto": sum(1 for r in tabela if r["em_aberto"]),
        "meses_em_aberto": sum(r["em_aberto"] for r in tabela),
        "sem_competencia": sem_competencia,
    }


def _empresas(tarefas, pendentes: dict, nomes: dict) -> tuple:
    """Devolve (top por inconsistência, top por pendência de curadoria).

    A inconsistência de cada empresa vem da auditoria mais recente que tem
    contador preenchido — ver o item 2 do cabeçalho do módulo.
    """
    ultima_por_empresa = {}
    for tenant_id, _user, status, criado, notas, incons in tarefas:
        if status != STATUS_CONCLUIDO or incons is None or not tenant_id:
            continue
        chave = str(tenant_id)
        atual = ultima_por_empresa.get(chave)
        if atual is None or (criado and criado > atual["quando"]):
            ultima_por_empresa[chave] = {
                "quando": criado, "notas": int(notas or 0),
                "inconsistencias": int(incons or 0),
            }

    por_inconsistencia = []
    for tid, d in ultima_por_empresa.items():
        por_inconsistencia.append({
            "id": tid,
            "nome": nomes.get(tid, "Empresa excluída"),
            "inconsistencias": d["inconsistencias"],
            "notas": d["notas"],
            # Taxa por 100 notas: é o que permite comparar uma empresa de 2.000
            # notas com uma de 60 sem que o tamanho decida o ranking sozinho.
            "taxa": round(d["inconsistencias"] * 100.0 / d["notas"], 1) if d["notas"] else 0.0,
            "quando": hora_local(d["quando"]),
        })
    por_inconsistencia.sort(key=lambda r: (-r["inconsistencias"], r["nome"]))

    por_pendencia = []
    for tid, d in pendentes.items():
        if not tid:
            continue
        por_pendencia.append({
            "id": tid,
            "nome": nomes.get(tid, "Empresa excluída"),
            "total": d["total"],
            "por_tipo": dict(sorted(d["por_tipo"].items(), key=lambda kv: -kv[1])),
        })
    por_pendencia.sort(key=lambda r: (-r["total"], r["nome"]))

    return por_inconsistencia, por_pendencia


# ------------------------------------------------------------------- nomes

def _competencias_por_empresa(db) -> list:
    """A última execução concluída de cada (empresa, competência).

    Sem recorte de janela: é preciso saber a PRIMEIRA competência de cada
    empresa para não cobrar dela um mês anterior à entrada na carteira, e uma
    empresa parada há um ano não tem competência nenhuma dentro da janela —
    justamente a que mais interessa aparecer. São algumas milhares de linhas
    agregadas no pior caso, sem nenhum `result_data` no caminho.
    """
    ultimas = (
        select(AuditTask.tenant_id.label("tid"),
               AuditTask.competencia.label("comp"),
               func.max(AuditTask.created_at).label("quando"))
        .where(AuditTask.status == STATUS_CONCLUIDO,
               AuditTask.competencia.isnot(None))
        .group_by(AuditTask.tenant_id, AuditTask.competencia)
        .subquery()
    )
    return db.execute(
        select(AuditTask.tenant_id, AuditTask.competencia,
               AuditTask.notas_processadas, AuditTask.inconsistencias)
        .join(ultimas, and_(AuditTask.tenant_id == ultimas.c.tid,
                            AuditTask.competencia == ultimas.c.comp,
                            AuditTask.created_at == ultimas.c.quando))
    ).all()


def _nomes_de_usuario(db) -> dict:
    """{uuid_em_texto: nome} de todas as contas.

    Traz a tabela inteira e casa em Python de propósito. `UserProfile.id` é UUID
    no Postgres e as chaves que temos em mão são texto (vêm de colunas String);
    um `IN` misturando os dois exige cast e quebra diferente em cada dialeto — e
    a tabela de perfis tem dezenas de linhas, não milhões.
    """
    linhas = db.execute(select(UserProfile.id, UserProfile.name)).all()
    return {str(uid): ((nome or "").strip() or str(uid)) for uid, nome in linhas}


def _empresas_cadastradas(db) -> tuple[dict, set]:
    """({uuid: nome}, {uuid das ativas}) — uma consulta, dois usos.

    Mesmo motivo do de cima para casar em Python. O conjunto das ativas existe
    porque o heatmap de competências não cobra mês de empresa que saiu da
    carteira: ela não deve nada, e apareceria como a mais atrasada de todas.
    """
    linhas = db.execute(select(Tenant.id, Tenant.name, Tenant.ativo)).all()
    nomes = {str(tid): nome for tid, nome, _ativo in linhas}
    ativas = {str(tid) for tid, _nome, ativo in linhas if ativo}
    return nomes, ativas


# ------------------------------------------------------------------- painel

def montar_painel(db, dias: int = DIAS_PADRAO) -> dict:
    """Tudo que a tela do painel precisa, em 6 idas ao banco.

    São elas: as auditorias do período, o UNION das regras pendentes, o UNION
    das regras criadas no período, a última execução de cada (empresa,
    competência), os nomes das contas e o cadastro das empresas. Nenhuma delas
    cresce com o tamanho do relatório de auditoria.
    """
    dias = dias if dias in PERIODOS else DIAS_PADRAO
    # O corte é em UTC porque created_at é UTC.
    corte = datetime.utcnow() - timedelta(days=dias)

    tarefas = _tarefas_do_periodo(db, corte)
    pendentes = _regras_pendentes_por_empresa(db)
    autores = _regras_criadas_por_autor(db, corte)
    competencias = _competencias_por_empresa(db)
    nomes_conta = _nomes_de_usuario(db)
    nomes_empresa, empresas_ativas = _empresas_cadastradas(db)

    concluidas = sum(1 for t in tarefas if t[2] == STATUS_CONCLUIDO)
    com_erro = sum(1 for t in tarefas if t[2] == STATUS_ERRO)
    em_andamento = len(tarefas) - concluidas - com_erro

    # Auditoria concluída antes de 02/09/2026 não tem contador. Contar como zero
    # faria o painel afirmar "nenhuma inconsistência" sobre o que ele não mediu.
    sem_contador = sum(1 for t in tarefas
                       if t[2] == STATUS_CONCLUIDO and t[5] is None)

    por_inconsistencia, por_pendencia = _empresas(tarefas, pendentes, nomes_empresa)

    return {
        "dias": dias,
        "periodos": PERIODOS,
        "gerado_em": _agora_local(),
        "kpis": {
            "auditorias": len(tarefas),
            "concluidas": concluidas,
            "com_erro": com_erro,
            "em_andamento": em_andamento,
            "taxa_sucesso": _pct(concluidas, concluidas + com_erro),
            "notas": sum(int(t[4] or 0) for t in tarefas),
            "inconsistencias": sum(int(t[5] or 0) for t in tarefas),
            "empresas_ativas": len({str(t[0]) for t in tarefas if t[0]}),
            "pessoas_ativas": len({str(t[1]) for t in tarefas if t[1]}),
            "regras_pendentes": sum(d["total"] for d in pendentes.values()),
        },
        "competencias": _heatmap_competencias(competencias, tarefas,
                                              nomes_empresa, empresas_ativas),
        "por_dia": _serie_por_dia(tarefas, dias),
        "pessoas": _uso_por_pessoa(tarefas, nomes_conta),
        "curadores": _curadoria_por_pessoa(autores, nomes_conta),
        "empresas_inconsistencias": por_inconsistencia[:TOPO],
        "empresas_pendencias": por_pendencia[:TOPO],
        "total_empresas_com_pendencia": len(por_pendencia),
        "sem_contador": sem_contador,
    }
