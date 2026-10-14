"""Planilha de um LOTE de NFS-e (pedido do Sandro em 01/10/2026).

Sai ao fim de cada lote — botão "Baixar relatório" no painel, e uma cópia em
`NFS-E-\\_relatorios\\AAAA-MM\\` —, no mesmo estilo da planilha da coleta
automática (services/nfse/relatorio_coleta.py):

- Visão Geral: quem rodou, quando, e os números do lote;
- Por empresa: o status de cada uma, com recebidas e emitidas separadas;
- Notas: uma linha por nota que veio NESTE lote, já com empresa e tipo;
- Pendências: o que não terminou bem (falha, adiada, faltou tempo, parada).

"Nota do lote" é a que o próprio lote gravou — o lote guarda a chave de cada
uma (`Linha.chaves`). Não dá para tirar pela data de gravação: nota que já
estava no banco e veio de novo não muda a data, e sumiria do relatório
justamente no "Recomeçar do zero".

Recebida e emitida saem da MESMA regra da tela (services/nfse/classificacao),
e as retenções não se somam entre os lados: na recebida a empresa reteve e
recolhe; na emitida, o cliente reteve dela.
"""
import io
import logging
import os
from collections import defaultdict
from datetime import datetime

from openpyxl import Workbook

from ...core.fuso import hora_local
from ...core.templating import moeda_br
from ...domain.models.nfse import NfseDocument
from .. import planilha_estilo as estilo
from . import classificacao, storage

logger = logging.getLogger(__name__)

PASTA = "_relatorios"
_LOTE_CONSULTA = 400        # números por IN: lote grande não vira uma consulta gigante

STATUS_FINAL = {"concluido": "Finalizado", "parado": "Parado", "falhou": "Interrompido por erro"}


def nome_arquivo(lote) -> str:
    local = hora_local(lote.inicio)
    return f"lote_nfse_{local:%Y-%m-%d_%H%M}.xlsx"


def notas_do_lote(db, lote) -> list[tuple]:
    """[(linha da empresa, nota)] — só as notas que o lote gravou."""
    saida = []
    for linha in lote.linhas:
        chaves = set(linha.chaves or [])
        if not chaves:
            continue
        numeros = sorted({n for _p, n, _m in chaves})
        for i in range(0, len(numeros), _LOTE_CONSULTA):
            parte = numeros[i:i + _LOTE_CONSULTA]
            for nota in (db.query(NfseDocument)
                         .filter(NfseDocument.tenant_id == linha.tenant_id,
                                 NfseDocument.numero.in_(parte)).all()):
                if (nota.prestador, nota.numero, nota.mun_prest or "") in chaves:
                    saida.append((linha, nota))
    saida.sort(key=lambda par: (par[0].empresa, par[1].dt_emissao or datetime.min))
    return saida


def _data(valor):
    return valor.strftime("%d/%m/%Y") if valor else ""


def gerar(db, lote, status_final: str) -> bytes:
    pares = notas_do_lote(db, lote)
    por_empresa = defaultdict(lambda: {"rq": 0, "rv": 0.0, "eq": 0, "ev": 0.0,
                                       "iss": 0.0, "ret": 0.0, "outras": 0})

    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Notas")
    ws.append(["Empresa", "CNPJ da empresa", "Tipo", "Nº da nota", "Emissão", "Competência",
               "CNPJ/CPF da contraparte", "Contraparte", "Valor do serviço", "Valor do ISS",
               "ISS retido", "IR", "PIS", "COFINS", "CSLL", "Valor do INSS", "Valor líquido",
               "Situação", "Arquivo"])
    for linha, nota in pares:
        d = classificacao.descrever(nota, linha.cnpj, linha.empresa)
        acum = por_empresa[linha.tenant_id]
        if d["tipo"] == classificacao.RECEBIDA:
            acum["rq"] += 1
            acum["rv"] += nota.valor_servico or 0.0
        elif d["tipo"] == classificacao.EMITIDA:
            acum["eq"] += 1
            acum["ev"] += nota.valor_servico or 0.0
        else:
            acum["outras"] += 1
        acum["iss"] += classificacao.iss_retido(nota)
        acum["ret"] += classificacao.retencoes_federais(nota)
        ws.append([linha.empresa, linha.cnpj, d["rotulo"], nota.numero, _data(nota.dt_emissao),
                   _data(nota.dt_competencia), d["contraparte_doc"], d["contraparte_nome"],
                   nota.valor_servico or 0.0, nota.iss or 0.0, nota.iss_retido or "",
                   nota.ir or 0.0, nota.pis or 0.0, nota.cofins or 0.0, nota.csll or 0.0,
                   nota.inss or 0.0, nota.valor_liquido or 0.0, nota.status or "",
                   storage.caminho_completo(nota.arquivo_xml) if nota.arquivo_xml else ""])

    ws = wb.create_sheet("Por empresa")
    ws.append(["Empresa", "CNPJ", "Status", "Documentos gravados", "Qtd. recebidas",
               "Valor recebidas", "Qtd. emitidas", "Valor emitidas", "Valor do ISS retido",
               "Retenções federais", "Destino ou motivo"])
    for linha in lote.linhas:
        a = por_empresa[linha.tenant_id]
        ws.append([linha.empresa, linha.cnpj, linha.status, linha.qtd, a["rq"], a["rv"],
                   a["eq"], a["ev"], a["iss"], a["ret"],
                   linha.destino + (f" — {linha.detalhe}" if linha.detalhe else "")])

    ws = wb.create_sheet("Pendências")
    ws.append(["Empresa", "CNPJ", "Status", "Documentos gravados", "Motivo"])
    from .lote import SUCESSO, VAZIO
    for linha in lote.linhas:
        if linha.status not in (SUCESSO, VAZIO):
            ws.append([linha.empresa, linha.cnpj, linha.status, linha.qtd,
                       linha.destino + (f" — {linha.detalhe}" if linha.detalhe else "")])

    estilo.estilizar_planilha(wb, cores={"Notas": estilo.VERDE, "Pendências": estilo.VERMELHO})

    c = lote.contagem()
    tot = {k: sum(a[k] for a in por_empresa.values()) for k in ("rq", "rv", "eq", "ev", "iss", "ret")}
    inicio = hora_local(lote.inicio)
    estilo.montar_capa(wb, "Lote de NFS-e", [
        ("Iniciado por", lote.dono_nome or "—"),
        ("Início", f"{inicio:%d/%m/%Y %H:%M}"),
        ("Situação", STATUS_FINAL.get(status_final, status_final)),
        ("Empresas", c["total"]),
        ("Com documento", c["com_documento"]),
        ("Em dia (nada novo)", c["vazias"]),
        ("Com mais documentos para a próxima busca", c["continua"]),
        ("Adiadas pelo ADN", c["adiadas"]),
        ("Com falha", c["falhas"]),
        ("Não terminadas", c["interrompidas"] + c["nao_consultadas"]),
        ("Documentos gravados", c["documentos"]),
        ("Notas recebidas", f"{tot['rq']} · R$ {moeda_br(tot['rv'])}"),
        ("Notas emitidas", f"{tot['eq']} · R$ {moeda_br(tot['ev'])}"),
        ("ISS retido", f"R$ {moeda_br(tot['iss'])}"),
        ("Retenções federais (IR, PIS, COFINS, CSLL, INSS)", f"R$ {moeda_br(tot['ret'])}"),
        ("Pasta das notas", storage.raiz()),
    ], subtitulo="Recebida: a empresa é a tomadora. Emitida: a empresa é a prestadora.")

    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def salvar(lote, conteudo: bytes) -> str | None:
    """Cópia na pasta das NFS-e. None se a rede estiver fora — o botão de
    baixar continua funcionando, porque o arquivo fica também na memória."""
    local = hora_local(lote.inicio)
    rel = os.path.join(PASTA, f"{local:%Y-%m}", nome_arquivo(lote))
    destino = os.path.join(storage.raiz(), rel)
    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "wb") as f:
            f.write(conteudo)
        return rel
    except OSError:
        logger.warning("Não foi possível salvar o relatório do lote em %s", destino, exc_info=True)
        return None
