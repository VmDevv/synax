"""A ficha da empresa: como o cliente ESTÁ, não quem ele é.

A aba "Empresas e Usuários" responde quem é o cliente — nome, CNPJ, UF, quem
tem acesso. Nenhuma tela respondia como ele está: quando foi auditado pela
última vez, quanta curadoria dele está represada, se o certificado vence semana
que vem. Isso vivia espalhado por três abas, e a curadoria represada só existia
somada no Radar — bom para saber que há 340 regras paradas, inútil para saber
de quem.

Este módulo junta essas respostas para UMA empresa. A tela só desenha.

As decisões que valem mais que o código:

1. **`result_data` não é lido aqui — nem por engano.** Vale a mesma regra do
   Radar: o relatório inteiro de uma auditoria passa fácil de alguns MB, e a
   ficha abre com um clique numa lista de 170 empresas. As consultas nomeiam
   coluna por coluna, `montar_ficha` custa 8 idas ao banco, e nenhuma delas
   cresce com o tamanho do relatório.

2. **A manchete é a ÚLTIMA auditoria; o histórico é que mostra o resto.**
   Rodar a mesma competência três vezes é rotina — corrige regra, roda de
   novo. O número do topo é a última foto, que é o estado de hoje; as
   execuções anteriores ficam na lista embaixo, onde o retrabalho é informação
   e não distorção do ranking.

3. **Contador nulo não vira zero.** Auditoria anterior a 02/09/2026 não tem
   `notas_processadas`/`inconsistencias`. Contá-la como zero faria a ficha
   afirmar "não achou nada" sobre o que ela não mediu: essas entram em
   `sem_contador` e aparecem com travessão.

4. **Competência em aberto substitui "auditada há N dias".** Desde 03/09/2026
   `audit_tasks.competencia` guarda o mês do SPED (registro 0000), e é ele que
   responde a pergunta do escritório: rodar abril em setembro é rotina, então a
   data do upload nunca disse se abril foi conferido. A régua só cobra meses
   FECHADOS e só a partir da primeira competência que a empresa tem — e,
   quando o histórico ainda não tem competência gravada, ela não afirma nada e
   pede o backfill, em vez de inventar atraso.

5. **Alerta é o que dá para AGIR hoje.** Certificado vencendo, cadastro
   incompleto, execução que falhou, pendência velha, silêncio longo demais.
   Inconsistência alta NÃO é alerta: é o trabalho normal do escritório, e
   aviso que aparece sempre deixa de ser lido.

6. **Pendência se mede por IDADE, não só por volume.** "17 regras represadas"
   não move ninguém; "a mais antiga está parada há 34 dias" move. O
   `created_at` já estava lá.

7. **As sete tabelas de curadoria vêm de `painel_atividade`.** Importar a
   constante em vez de repetir a lista é o que garante que a ficha e o Radar
   contem a mesma coisa — inclusive a `CestMvaRule` ficar de fora, que é a
   Portaria 16 e não é trabalho de curadoria de ninguém.
"""
from __future__ import annotations

import re
import uuid as _uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import and_, case, func, literal, select, union_all

from ..core import competencia as comp
from ..core.fuso import hora_local
from ..domain.models.audit import AuditTask
from ..domain.models.auth_models import Tenant, UserProfile, UserTenant
from ..domain.models.certificates import TenantCertificate
from .nfse import certificados as cert_svc
from .painel_atividade import STATUS_CONCLUIDO, STATUS_ERRO, TABELAS_DE_REGRA

HISTORICO = 8              # execuções listadas na ficha
SPARK = 8                  # barras do minigráfico de inconsistência
JANELA_COMPETENCIAS = 12   # meses desenhados na régua de competências
DIAS_SEM_AUDITORIA = 45    # daqui para cima a ficha chama a atenção
DIAS_PENDENCIA_VELHA = 30  # pendência mais velha que isso vira alerta

# Escala de LEITURA da régua de competências, não regra fiscal: serve só para
# dar cor à célula. O número exato aparece do lado, e é ele que vale.
ESCALA_TAXA = ((0.0, "limpo"), (2.0, "baixo"), (5.0, "medio"))

STATUS_PENDENTE = "pendente_revisao"
STATUS_APROVADO = "aprovado"

# As colunas leves de audit_tasks. `result_data` e `error_message` NÃO estão
# aqui, e é por isso que a ficha abre rápido — ver o item 1 do cabeçalho.
COLUNAS_LEVES = (
    AuditTask.id,
    AuditTask.user_id,
    AuditTask.status,
    AuditTask.created_at,
    AuditTask.file_name,
    AuditTask.notas_processadas,
    AuditTask.inconsistencias,
)


# ------------------------------------------------------------------ formato

def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def formatar_cnpj(valor) -> str:
    """14 dígitos -> 00.000.000/0000-00. O que não for CNPJ volta como veio."""
    d = _digitos(valor)
    if len(d) != 14:
        return str(valor or "").strip() or "—"
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _pct(parte: float, total: float) -> float:
    return round(parte * 100.0 / total, 1) if total else 0.0


def _taxa(inconsistencias, notas):
    """Inconsistências a cada 100 notas. `None` quando não há base.

    Sem nota conferida não existe taxa — devolver 0.0 aqui faria a ficha
    apresentar uma empresa não medida como uma empresa limpa.
    """
    if not notas:
        return None
    return round((inconsistencias or 0) * 100.0 / notas, 1)


def _dias_desde(quando) -> int | None:
    if not quando:
        return None
    return max((datetime.utcnow() - quando).days, 0)


# ---------------------------------------------------------------- consultas

def _empresa(db, tenant_id):
    try:
        uid = _uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.query(Tenant).filter(Tenant.id == uid).first()


def _execucoes(db, tenant_id, limite: int = HISTORICO):
    """As últimas execuções, de qualquer status — sem o relatório junto."""
    return db.execute(
        select(*COLUNAS_LEVES)
        .where(AuditTask.tenant_id == str(tenant_id))
        .order_by(AuditTask.created_at.desc())
        .limit(limite)
    ).all()


def _medidas(db, tenant_id, limite: int = SPARK):
    """As últimas execuções que REALMENTE mediram algo — as do minigráfico.

    Consulta própria, e não um filtro sobre `_execucoes`: uma empresa com oito
    erros seguidos deixaria o gráfico vazio sem que ninguém entendesse por quê.
    """
    return db.execute(
        select(AuditTask.created_at, AuditTask.notas_processadas,
               AuditTask.inconsistencias)
        .where(AuditTask.tenant_id == str(tenant_id),
               AuditTask.status == STATUS_CONCLUIDO,
               AuditTask.inconsistencias.isnot(None),
               AuditTask.notas_processadas.isnot(None))
        .order_by(AuditTask.created_at.desc())
        .limit(limite)
    ).all()


def _competencias(db, tenant_id):
    """A ÚLTIMA execução concluída de cada competência desta empresa.

    A última, e não a soma nem a primeira, pelo mesmo motivo da manchete:
    reprocessar o mesmo mês depois de cadastrar regra é rotina, e o que vale é
    a foto mais recente daquele mês.
    """
    alvo = str(tenant_id)
    ultimas = (
        select(AuditTask.competencia.label("comp"),
               func.max(AuditTask.created_at).label("quando"))
        .where(AuditTask.tenant_id == alvo,
               AuditTask.status == STATUS_CONCLUIDO,
               AuditTask.competencia.isnot(None))
        .group_by(AuditTask.competencia)
        .subquery()
    )
    return db.execute(
        select(AuditTask.competencia, AuditTask.created_at,
               AuditTask.notas_processadas, AuditTask.inconsistencias)
        # O tenant entra também no SELECT de fora: sem ele, duas empresas com
        # execuções no mesmo instante e na mesma competência se cruzariam.
        .where(AuditTask.tenant_id == alvo)
        .join(ultimas, and_(AuditTask.competencia == ultimas.c.comp,
                            AuditTask.created_at == ultimas.c.quando))
    ).all()


def _totais(db, tenant_id):
    """Quantas execuções, quantas sem contador e desde quando — por status.

    Não soma notas nem inconsistências de propósito. Rodar a mesma competência
    três vezes é rotina, e a soma contaria o mesmo mês três vezes: o total
    diria que a empresa tem 142 inconsistências quando ela tem 12 e foi
    reprocessada duas vezes. O número que vale é o da última execução.
    """
    return db.execute(
        select(
            AuditTask.status,
            func.count().label("qtd"),
            func.sum(case((AuditTask.inconsistencias.is_(None), 1), else_=0)
                     ).label("sem_contador"),
            func.min(AuditTask.created_at).label("primeira"),
        )
        .where(AuditTask.tenant_id == str(tenant_id))
        .group_by(AuditTask.status)
    ).all()


def _regras(db, tenant_id):
    """UNION ALL das sete tabelas de regra: pendentes e aprovadas numa ida só.

    Agrupa por status para trazer as duas metades juntas — a represada (o que
    falta julgar) e a aprovada (o quanto a empresa já está mapeada).
    """
    partes = []
    for modelo, rotulo in TABELAS_DE_REGRA:
        partes.append(
            select(
                literal(rotulo).label("tipo"),
                modelo.status.label("situacao"),
                func.count().label("qtd"),
                func.min(modelo.created_at).label("mais_antiga"),
            )
            .where(modelo.tenant_id == str(tenant_id))
            .group_by(modelo.status)
        )
    return db.execute(union_all(*partes)).all()


def _pessoas(db):
    """(perfis, vínculos) das tabelas inteiras, casados em Python.

    Mesmo motivo do `_nomes_de_usuario` do Radar: as chaves que temos em mão
    são texto e as colunas são UUID no Postgres; um `IN` misturando os dois
    exige cast e quebra diferente em cada dialeto. As duas tabelas têm dezenas
    de linhas, não milhões.
    """
    perfis = db.execute(
        select(UserProfile.id, UserProfile.name, UserProfile.role,
               UserProfile.tenant_id)
    ).all()
    vinculos = db.execute(
        select(UserTenant.user_id, UserTenant.tenant_id)
    ).all()
    return perfis, vinculos


def _certificado(db, tenant_id, cnpj: str) -> dict:
    """Estado do A1 da empresa. Lê a validade JÁ GRAVADA — não abre o .pfx.

    A listagem da pasta é a única E/S fora do banco nesta ficha, e falha em
    silêncio de propósito (devolve lista vazia): pasta de rede fora do ar não
    pode impedir de ver o resto da ficha.
    """
    registro = db.query(TenantCertificate).filter(
        TenantCertificate.tenant_id == str(tenant_id)).first()

    if len(cnpj) != 14:
        diag = {"ok": False, "motivo": "Empresa sem CNPJ válido"}
    else:
        diag = cert_svc.diagnosticar(registro, cnpj, cert_svc.listar_arquivos())

    validade = cert_svc.classificar_validade(registro, diag)
    return {
        "cadastrado": bool(registro),
        "ativo": bool(registro and registro.ativo),
        "arquivo": diag.get("arquivo") or (registro.arquivo if registro else None),
        "apelido": registro.apelido if registro else None,
        "ultimo_erro": registro.ultimo_erro if registro else None,
        "diag": diag,
        "validade": validade,
        "dias_alerta": cert_svc.DIAS_ALERTA,
    }


# ------------------------------------------------------------------- blocos

def _bloco_auditoria(execucoes, medidas, totais, nomes) -> dict:
    por_status = {str(linha[0] or ""): linha for linha in totais}
    concluidas = int(por_status.get(STATUS_CONCLUIDO, (None, 0))[1] or 0)
    erros = int(por_status.get(STATUS_ERRO, (None, 0))[1] or 0)
    total = sum(int(l[1] or 0) for l in totais)

    # Só a concluída sem contador é lacuna: a que está em curso ainda não mediu.
    linha_ok = por_status.get(STATUS_CONCLUIDO)
    sem_contador = int(linha_ok[2] or 0) if linha_ok else 0
    primeiras = [l[3] for l in totais if l[3]]

    historico = []
    for tid, user_id, status, criado, arquivo, n, i in execucoes:
        historico.append({
            "id": tid,
            "quando": hora_local(criado),
            "status": status,
            "quem": nomes.get(str(user_id or "")) or ("Sistema / API" if not user_id
                                                      else "Conta removida"),
            "arquivo": arquivo,
            "notas": n,
            "inconsistencias": i,
            "taxa": _taxa(i, n) if i is not None else None,
            "medida": i is not None and n is not None,
        })

    ultima = historico[0] if historico else None
    ultima_medida = next((h for h in historico if h["medida"]), None)

    # Minigráfico: as medições em ordem cronológica, altura relativa ao pico.
    # O piso de 1 evita divisão por zero quando toda a série é limpa.
    serie = list(reversed(medidas))
    pico = max([1.0] + [(_taxa(i, n) or 0.0) for _q, n, i in serie])
    spark = []
    for quando, n, i in serie:
        taxa = _taxa(i, n) or 0.0
        local = hora_local(quando)
        spark.append({
            "quando": local,
            "rotulo": local.strftime("%d/%m") if local else "—",
            "notas": int(n or 0),
            "inconsistencias": int(i or 0),
            "taxa": taxa,
            # Piso de 6%: barra de valor zero precisa continuar visível, senão
            # a auditoria limpa some do gráfico e parece dia sem movimento.
            "altura": max(round(taxa * 100.0 / pico), 6),
        })

    tendencia = None
    if len(spark) >= 2:
        atual, anterior = spark[-1]["taxa"], spark[-2]["taxa"]
        delta = round(atual - anterior, 1)
        tendencia = {
            "delta": abs(delta),
            "direcao": "queda" if delta < 0 else ("alta" if delta > 0 else "estavel"),
            "de": anterior,
            "para": atual,
        }

    return {
        "total": total,
        "concluidas": concluidas,
        "erros": erros,
        "em_andamento": total - concluidas - erros,
        "taxa_sucesso": _pct(concluidas, concluidas + erros),
        "sem_contador": sem_contador,
        "primeira_em": hora_local(min(primeiras)) if primeiras else None,
        "ultima": ultima,
        "ultima_medida": ultima_medida,
        "dias_desde_ultima": _dias_desde(execucoes[0][3]) if execucoes else None,
        "historico": historico,
        "spark": spark,
        "tendencia": tendencia,
    }


def _nivel(taxa):
    """Faixa de cor da célula da régua. `None` (não medida) fica sem faixa."""
    if taxa is None:
        return None
    for teto, nome in ESCALA_TAXA:
        if taxa <= teto:
            return nome
    return "alto"


def _bloco_competencias(linhas, tem_execucao: bool) -> dict:
    """A régua dos últimos meses: o que foi conferido e o que está em aberto.

    Duas honestidades que a régua precisa manter:

    - **O mês corrente não é cobrado.** O SPED de setembro se entrega em
      outubro; a última competência cobrável é a fechada (`comp.fechada()`).

    - **Empresa só é cobrada a partir do primeiro mês que ela tem.** Cliente
      que entrou em março não deve aparecer devendo janeiro, e mês anterior ao
      primeiro auditado sai da régua como "fora do período", não como buraco.

    Quando NENHUMA execução tem competência gravada — histórico anterior a
    03/09/2026 —, a régua não afirma nada: devolve `sem_registro` e a tela pede
    o backfill. Alegar "abril em aberto" olhando um banco onde a competência
    nunca foi gravada seria inventar atraso.
    """
    medidas = {}
    for competencia_, quando, notas, incons in linhas:
        chave = comp.valida(competencia_)
        if not chave:
            continue
        medidas[chave] = {
            "quando": hora_local(quando),
            "notas": notas,
            "inconsistencias": incons,
            "taxa": _taxa(incons, notas) if incons is not None else None,
        }

    if not medidas:
        return {"regua": [], "auditadas": 0, "em_aberto": [],
                "sem_registro": tem_execucao, "primeira": None, "ultima": None}

    # A régua termina na última competência fechada — ou depois, se alguém já
    # auditou o mês em curso; escondê-la faria a pessoa procurar o que fez.
    fim = max([comp.fechada()] + list(medidas))
    regua_meses = comp.sequencia(JANELA_COMPETENCIAS, ate=fim)
    primeira = min(medidas)
    cobravel_ate = comp.fechada()

    regua = []
    for mes in regua_meses:
        dados = medidas.get(mes)
        esperada = mes >= primeira and mes <= cobravel_ate
        regua.append({
            "comp": mes,
            "rotulo": comp.rotulo(mes),
            "curto": comp.mes_curto(mes),
            "ano": mes[:4],
            "auditada": bool(dados),
            "esperada": esperada,
            "em_aberto": esperada and not dados,
            "notas": dados["notas"] if dados else None,
            "inconsistencias": dados["inconsistencias"] if dados else None,
            "taxa": dados["taxa"] if dados else None,
            "quando": dados["quando"] if dados else None,
            "nivel": _nivel(dados["taxa"]) if dados else None,
        })

    return {
        "regua": regua,
        "auditadas": len(medidas),
        "em_aberto": [r["rotulo"] for r in regua if r["em_aberto"]],
        "sem_registro": False,
        "primeira": comp.rotulo(primeira),
        "ultima": comp.rotulo(max(medidas)),
    }


def _bloco_curadoria(linhas) -> dict:
    pendentes = defaultdict(int)
    aprovadas = defaultdict(int)
    mais_antiga = None

    for tipo, situacao, qtd, antiga in linhas:
        qtd = int(qtd or 0)
        if situacao == STATUS_PENDENTE:
            pendentes[tipo] += qtd
            if antiga and (mais_antiga is None or antiga < mais_antiga):
                mais_antiga = antiga
        elif situacao == STATUS_APROVADO:
            aprovadas[tipo] += qtd

    total_pendente = sum(pendentes.values())
    total_aprovado = sum(aprovadas.values())

    def _lista(dados, total):
        return [{"tipo": t, "qtd": q, "pct": _pct(q, total)}
                for t, q in sorted(dados.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "pendentes": total_pendente,
        "por_tipo": _lista(pendentes, total_pendente),
        "mais_antiga_em": hora_local(mais_antiga),
        "dias_parada": _dias_desde(mais_antiga),
        "aprovadas": total_aprovado,
        "aprovadas_por_tipo": _lista(aprovadas, total_aprovado),
        "tipos_mapeados": len(aprovadas),
    }


def _bloco_equipe(perfis, vinculos, tenant_id) -> tuple[list, dict]:
    alvo = str(tenant_id)
    nomes = {}
    dados = {}

    for uid, nome, papel, tid in perfis:
        chave = str(uid)
        limpo = (nome or "").strip() or chave
        nomes[chave] = limpo
        if str(tid or "") == alvo:
            dados[chave] = {"id": chave, "nome": limpo, "papel": papel or "—",
                            "vinculo": "principal"}

    for user_id, tid in vinculos:
        if str(tid or "") != alvo:
            continue
        chave = str(user_id)
        if chave in dados:
            dados[chave]["vinculo"] = "principal e operação"
        else:
            dados[chave] = {"id": chave, "nome": nomes.get(chave, chave),
                            "papel": "—", "vinculo": "operação"}

    equipe = sorted(dados.values(), key=lambda p: p["nome"].lower())
    return equipe, nomes


def _bloco_cadastro(empresa, cnpj, certificado, equipe, auditoria) -> dict:
    """Checklist do que trava trabalho lá na frente, longe desta tela."""
    itens = [
        {"rotulo": "CNPJ válido", "ok": len(cnpj) == 14,
         "falta": "A empresa não tem CNPJ de 14 dígitos — sem ele não há "
                  "certificado nem busca no SIEG."},
        {"rotulo": "UF preenchida", "ok": bool((empresa.uf or "").strip()),
         "falta": "Sem UF, a auditoria de PDF presume CFOP 1933 e o arquivo do "
                  "Domínio não sai."},
        {"rotulo": "Certificado A1 ativo", "ok": bool(certificado["diag"].get("ok")),
         "falta": certificado["diag"].get("motivo") or "Certificado indisponível."},
        {"rotulo": "Alguém com acesso", "ok": bool(equipe),
         "falta": "Nenhum usuário vinculado: só admin enxerga esta empresa."},
        {"rotulo": "Já auditada", "ok": bool(auditoria["total"]),
         "falta": "Nenhuma auditoria rodada até hoje."},
    ]
    completos = sum(1 for i in itens if i["ok"])
    return {"itens": itens, "completos": completos, "de": len(itens),
            "pct": _pct(completos, len(itens))}


def _alertas(empresa, certificado, cadastro, auditoria, curadoria,
             competencias) -> list:
    """Só o que dá para agir hoje — ver o item 5 do cabeçalho do módulo."""
    fila = []

    val = certificado["validade"]
    if val["grupo"] == "problema":
        fila.append({"nivel": "problema", "icone": "fa-file-shield",
                     "texto": f"Certificado: {val['rotulo'].lower()}",
                     "detalhe": "Sem certificado válido não há busca de XML nem "
                                "envio de NFS-e."})
    elif val["grupo"] == "alerta":
        fila.append({"nivel": "alerta", "icone": "fa-file-shield",
                     "texto": f"Certificado {val['rotulo'].lower()}",
                     "detalhe": "Renove antes que trave a próxima competência."})

    ultima = auditoria["ultima"]
    if ultima and ultima["status"] == STATUS_ERRO:
        fila.append({"nivel": "problema", "icone": "fa-triangle-exclamation",
                     "texto": "A última execução falhou",
                     "detalhe": "A empresa está sem auditoria válida desde então."})

    # Competência em aberto é a versão precisa de "faz tempo que ninguém roda":
    # diz QUAL mês falta, e por isso substitui o alerta por dias em vez de
    # somar-se a ele. Dois avisos dizendo a mesma coisa viram ruído.
    aberto = competencias["em_aberto"]
    if empresa.ativo and aberto:
        mostra = ", ".join(aberto[:3])
        resto = f" e mais {len(aberto) - 3}" if len(aberto) > 3 else ""
        fila.append({"nivel": "alerta", "icone": "fa-calendar-xmark",
                     "texto": f"{len(aberto)} competência(s) sem auditoria: {mostra}{resto}",
                     "detalhe": "Meses fechados entre a primeira competência "
                                "auditada e o mês passado."})

    dias = auditoria["dias_desde_ultima"]
    if (empresa.ativo and auditoria["total"] and not aberto
            and dias is not None and dias >= DIAS_SEM_AUDITORIA):
        fila.append({"nivel": "alerta", "icone": "fa-clock-rotate-left",
                     "texto": f"Sem auditoria há {dias} dias",
                     "detalhe": "Contado do último upload. Não há competência em "
                                "aberto — pode ser histórico sem competência gravada."})
    if empresa.ativo and not auditoria["total"]:
        fila.append({"nivel": "alerta", "icone": "fa-inbox",
                     "texto": "Nunca auditada",
                     "detalhe": "Empresa ativa que nunca passou pelo motor."})

    parada = curadoria["dias_parada"]
    if (curadoria["pendentes"] and parada is not None
            and parada >= DIAS_PENDENCIA_VELHA):
        fila.append({"nivel": "alerta", "icone": "fa-hourglass-half",
                     "texto": f"Curadoria parada há {parada} dias",
                     "detalhe": f"{curadoria['pendentes']} regra(s) aguardando "
                                "aprovação — o motor ignora todas elas."})

    faltando = [i["rotulo"] for i in cadastro["itens"]
                if not i["ok"] and i["rotulo"] != "Já auditada"]
    if faltando:
        fila.append({"nivel": "alerta", "icone": "fa-clipboard-list",
                     "texto": "Cadastro incompleto: " + ", ".join(faltando).lower(),
                     "detalhe": "Cada item que falta trava uma etapa mais adiante."})

    if not empresa.ativo:
        fila.append({"nivel": "info", "icone": "fa-power-off",
                     "texto": "Empresa inativa",
                     "detalhe": "Fora da carteira; o histórico continua acessível."})

    ordem = {"problema": 0, "alerta": 1, "info": 2}
    fila.sort(key=lambda a: ordem.get(a["nivel"], 9))
    return fila


# ------------------------------------------------------------------- ficha

def montar_ficha(db, tenant_id) -> dict | None:
    """Tudo que a ficha precisa, em 9 idas ao banco. `None` se a empresa sumiu.

    São elas: a empresa, as últimas execuções, as últimas medições, os totais
    agregados, a última execução de cada competência, o UNION das sete tabelas
    de regra, os perfis, os vínculos e o certificado. Nenhuma cresce com o
    tamanho do relatório de auditoria.
    """
    empresa = _empresa(db, tenant_id)
    if not empresa:
        return None

    tid = str(empresa.id)
    cnpj = _digitos(empresa.cnpj)

    execucoes = _execucoes(db, tid)
    medidas = _medidas(db, tid)
    totais = _totais(db, tid)
    linhas_comp = _competencias(db, tid)
    linhas_regra = _regras(db, tid)
    perfis, vinculos = _pessoas(db)
    certificado = _certificado(db, tid, cnpj)

    equipe, nomes = _bloco_equipe(perfis, vinculos, tid)
    auditoria = _bloco_auditoria(execucoes, medidas, totais, nomes)
    competencias = _bloco_competencias(linhas_comp, bool(auditoria["total"]))
    curadoria = _bloco_curadoria(linhas_regra)
    cadastro = _bloco_cadastro(empresa, cnpj, certificado, equipe, auditoria)

    return {
        "empresa": {
            "id": tid,
            "nome": empresa.name,
            "cnpj": formatar_cnpj(empresa.cnpj),
            "cnpj_digitos": cnpj,
            "uf": (empresa.uf or "").strip().upper(),
            "ativo": bool(empresa.ativo),
            "criada_em": hora_local(empresa.created_at),
        },
        "alertas": _alertas(empresa, certificado, cadastro, auditoria, curadoria,
                            competencias),
        "auditoria": auditoria,
        "competencias": competencias,
        "curadoria": curadoria,
        "certificado": certificado,
        "cadastro": cadastro,
        "equipe": equipe,
        "gerado_em": hora_local(datetime.utcnow()),
    }


# ------------------------------------------------- resumo para a listagem

def _ultima_concluida_por_empresa(db) -> dict:
    """A última auditoria concluída de CADA empresa, em uma consulta.

    O subselect pega a data máxima por empresa e o join volta só nessa linha —
    é isso que evita trazer o histórico inteiro para achar a última de cada
    uma. Empate de data (duas execuções no mesmo instante) é resolvido pela
    primeira que chegar; a diferença entre elas não muda o que a coluna diz.
    """
    ultimas = (
        select(AuditTask.tenant_id.label("tid"),
               func.max(AuditTask.created_at).label("quando"))
        .where(AuditTask.status == STATUS_CONCLUIDO)
        .group_by(AuditTask.tenant_id)
        .subquery()
    )
    linhas = db.execute(
        select(AuditTask.tenant_id, AuditTask.created_at,
               AuditTask.notas_processadas, AuditTask.inconsistencias,
               AuditTask.competencia)
        .join(ultimas, and_(AuditTask.tenant_id == ultimas.c.tid,
                            AuditTask.created_at == ultimas.c.quando))
    ).all()

    saida = {}
    for tid, quando, notas, incons, competencia_ in linhas:
        chave = str(tid or "")
        if chave in saida:
            continue
        saida[chave] = {
            "quando": hora_local(quando),
            "dias": _dias_desde(quando),
            "notas": notas,
            "inconsistencias": incons,
            "taxa": _taxa(incons, notas) if incons is not None else None,
            "medida": incons is not None and notas is not None,
            # A competência é o que o contador procura na linha: "o abril
            # dessa empresa saiu?". A data do upload fica ao lado, menor.
            "competencia": comp.valida(competencia_),
            "competencia_rotulo": comp.rotulo(competencia_),
        }
    return saida


def _pendentes_por_empresa(db) -> dict:
    """{tenant_id: {"total": n, "dias_parada": n}} — o represado de cada uma.

    Sem recorte de data: pendência de três meses continua pendente, e é
    justamente a que ninguém está vendo.
    """
    partes = []
    for modelo, _rotulo in TABELAS_DE_REGRA:
        partes.append(
            select(modelo.tenant_id.label("tid"),
                   func.count().label("qtd"),
                   func.min(modelo.created_at).label("mais_antiga"))
            .where(modelo.status == STATUS_PENDENTE)
            .group_by(modelo.tenant_id)
        )

    saida = {}
    for tid, qtd, antiga in db.execute(union_all(*partes)).all():
        alvo = saida.setdefault(str(tid or ""), {"total": 0, "mais_antiga": None})
        alvo["total"] += int(qtd or 0)
        if antiga and (alvo["mais_antiga"] is None or antiga < alvo["mais_antiga"]):
            alvo["mais_antiga"] = antiga

    for dados in saida.values():
        dados["dias_parada"] = _dias_desde(dados.pop("mais_antiga"))
    return saida


def resumo_para_lista(db) -> dict:
    """{tenant_id: {"ultima": ..., "pendencia": ...}} para a tabela de empresas.

    Duas consultas agregadas para a lista inteira — não uma por empresa. Com
    170 clientes, um `montar_ficha` por linha seriam 1.360 idas ao banco só
    para desenhar duas colunas.
    """
    ultimas = _ultima_concluida_por_empresa(db)
    pendentes = _pendentes_por_empresa(db)
    chaves = set(ultimas) | set(pendentes)
    return {c: {"ultima": ultimas.get(c), "pendencia": pendentes.get(c)}
            for c in chaves}
