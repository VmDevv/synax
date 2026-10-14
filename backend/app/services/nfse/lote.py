"""Busca de NFS-e em LOTE, em segundo plano, com o andamento na tela.

Pedido do Sandro em 01/10/2026: "no VmHub o sistema não para quando eu coloco
para baixar diversas empresas; no Synax ele para do nada". Parava porque o
lote rodava DENTRO do clique: a requisição tinha 240 s para todas as empresas,
cabiam 5 ou 6, e o resto saía "não consultada: tempo esgotado" — com o limite
de 6 cliques a cada 15 minutos barrando, sem aviso na tela, quem clicava de
novo para continuar. A última empresa de cada leva ficava com o tempo que
sobrava: uma página do ADN (50 documentos), e o cursor parado no NSU 50.

Agora o clique só COMEÇA o lote e volta na hora. Uma thread consulta empresa
por empresa — com a MESMA `_buscar_uma_empresa` da busca avulsa e da coleta
automática, e com o teto por empresa da coleta automática — e a tela acompanha
pelo log e pela tabela.

Três regras:

- **Um lote por vez, e nunca junto da coleta automática.** Os dois pegam a
  mesma trava (`automacao._trava("coleta_nfse")`): duas buscas no ADN com o
  mesmo certificado ao mesmo tempo é o caminho mais curto para o "consumo
  indevido".
- **O estado vive na memória do processo.** Reiniciar o servidor no meio perde
  a tela do lote, não as notas: cada documento é gravado antes de o cursor de
  NSU andar, e a próxima busca continua de onde parou.
- **Nenhum lote termina "verde com zero".** O fim diz quantas empresas vieram
  com documento, quantas estavam em dia, quantas o ADN adiou e quantas
  falharam — o VmHub dizia "finalizado com sucesso" com empresa falhando.
"""
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import certificados, storage

logger = logging.getLogger(__name__)

AGUARDANDO = "AGUARDANDO"
BAIXANDO = "BAIXANDO"
SUCESSO = "SUCESSO"
VAZIO = "SUCESSO (Vazio)"
CONTINUA = "SUCESSO (continua)"
ADIADA = "ADIADA"
FALHOU = "FALHOU"
SEM_SENHA = "FALHOU (Sem Senha)"
INTERROMPIDA = "INTERROMPIDA"
NAO_CONSULTADA = "NÃO CONSULTADA"

# Tom de cada status: escolhe a cor na tela.
TONS = {
    AGUARDANDO: "neutro", BAIXANDO: "info", SUCESSO: "ok", VAZIO: "ok",
    CONTINUA: "aviso", ADIADA: "aviso", FALHOU: "erro", SEM_SENHA: "erro",
    INTERROMPIDA: "aviso", NAO_CONSULTADA: "neutro",
}


@dataclass
class Linha:
    tenant_id: str
    empresa: str
    cnpj: str
    status: str = AGUARDANDO
    qtd: int = 0
    destino: str = ""
    detalhe: str = ""
    # Destino é a pasta (e não uma mensagem): a tela mostra em fonte de código.
    e_pasta: bool = False
    # (prestador, número, município) de cada nota gravada — o relatório lista
    # exatamente estas, inclusive as que já estavam no banco.
    chaves: list = field(default_factory=list)

    @property
    def tom(self) -> str:
        return TONS.get(self.status, "neutro")


@dataclass
class Lote:
    id: str
    dono_id: str
    dono_nome: str
    reiniciar: bool
    linhas: list[Linha]
    inicio: datetime = field(default_factory=datetime.utcnow)
    fim: datetime | None = None
    status: str = "rodando"          # rodando | concluido | parado | falhou
    parar: bool = False
    log: list[tuple[datetime, str, str]] = field(default_factory=list)
    relatorio: bytes | None = None       # a planilha, para o botão de baixar
    relatorio_rel: str | None = None     # a cópia na pasta da rede (relativa)

    def registrar(self, texto: str, tom: str = "info") -> None:
        self.log.append((datetime.utcnow(), texto, tom))

    @property
    def rodando(self) -> bool:
        return self.status == "rodando"

    def contagem(self) -> dict:
        c = {"total": len(self.linhas), "feitas": 0, "com_documento": 0, "vazias": 0,
             "continua": 0, "adiadas": 0, "falhas": 0, "interrompidas": 0,
             "nao_consultadas": 0, "documentos": 0}
        for ln in list(self.linhas):
            c["documentos"] += ln.qtd
            if ln.status in (AGUARDANDO, BAIXANDO):
                continue
            c["feitas"] += 1
            if ln.status in (SUCESSO, CONTINUA):
                c["com_documento"] += 1
            if ln.status == CONTINUA:
                c["continua"] += 1
            elif ln.status == VAZIO:
                c["vazias"] += 1
            elif ln.status == ADIADA:
                c["adiadas"] += 1
            elif ln.status in (FALHOU, SEM_SENHA):
                c["falhas"] += 1
            elif ln.status == INTERROMPIDA:
                c["interrompidas"] += 1
            elif ln.status == NAO_CONSULTADA:
                c["nao_consultadas"] += 1
        return c


_guarda = threading.Lock()
_atual: Lote | None = None
_thread: threading.Thread | None = None


def atual() -> Lote | None:
    return _atual


def pode_ver(lote: Lote | None, profile) -> bool:
    """Detalhe do lote (empresas, pastas, erros) é de quem o começou e do admin.
    O operador que chega depois só fica sabendo que há um lote rodando."""
    if lote is None or profile is None:
        return False
    return profile.role == "admin" or str(profile.id) == lote.dono_id


def iniciar(sessao_factory, empresas: list[tuple[str, str, str]], reiniciar: bool,
            dono_id: str, dono_nome: str) -> tuple[Lote | None, str | None]:
    """Começa o lote. `empresas` = [(tenant_id, nome, cnpj)], já autorizadas.

    Devolve (lote, None), ou (None, motivo) quando não dá para começar agora.
    """
    global _atual, _thread
    from .automacao import _trava

    with _guarda:
        if _atual is not None and _atual.rodando:
            return None, (f"Já há um lote em andamento, iniciado por "
                          f"{_atual.dono_nome or 'outra pessoa'}. Acompanhe abaixo — "
                          "um lote por vez, para não dobrar as chamadas ao ADN.")
        trava = _trava("coleta_nfse")
        if not trava.acquire(blocking=False):
            return None, ("A coleta automática de NFS-e está rodando agora. "
                          "Espere ela terminar para começar o lote: as duas juntas "
                          "dobram as chamadas ao ADN.")
        lote = Lote(id=str(uuid.uuid4()), dono_id=dono_id, dono_nome=dono_nome,
                    reiniciar=reiniciar,
                    linhas=[Linha(tenant_id=t, empresa=n, cnpj=c) for t, n, c in empresas])
        _atual = lote
        _thread = threading.Thread(target=_rodar, args=(lote, sessao_factory, trava),
                                   name="nfse-lote", daemon=True)
        _thread.start()
        return lote, None


def parar() -> bool:
    """Pede para o lote parar. A empresa em andamento para na página seguinte
    do ADN; o que já veio fica gravado."""
    lote = _atual
    if lote is None or not lote.rodando:
        return False
    if not lote.parar:
        lote.parar = True
        lote.registrar("Parada pedida: o lote termina a página em andamento e para.", "aviso")
    return True


def esperar(segundos: float = 30.0) -> None:
    """Para os testes: espera a thread do lote terminar."""
    t = _thread
    if t is not None:
        t.join(segundos)


# ------------------------------------------------------------------ execução

def _rodar(lote: Lote, sessao_factory, trava) -> None:
    from ...api.routes import nfse as rota
    from .automacao import TETO_EMPRESA_SEG

    db = None
    try:
        db = sessao_factory()
        lote.registrar(f"Lote iniciado: {len(lote.linhas)} empresa(s). "
                       f"Destino: {storage.raiz()}")
        if lote.reiniciar:
            lote.registrar("Recomeçando do zero: o histórico de NSU foi ignorado.", "aviso")
        # Uma listagem de cada pasta da rede para o lote inteiro.
        existentes = storage.pastas_existentes()
        arquivos_cert = certificados.listar_arquivos()
        if not storage.disponivel():
            lote.registrar("A pasta da rede não está acessível: as notas entram no "
                           "Synax, mas os arquivos NÃO serão gravados na pasta.", "erro")

        for linha in lote.linhas:
            if lote.parar:
                linha.status = NAO_CONSULTADA
                linha.destino = "Lote parado antes desta empresa."
                continue
            _uma_empresa(db, rota, lote, linha, existentes, arquivos_cert, TETO_EMPRESA_SEG)

        final = "parado" if lote.parar else "concluido"
        # O relatório sai ANTES de o status mudar: enquanto ele é montado a
        # tela continua "em andamento", e o botão de baixar só aparece pronto.
        _gerar_relatorio(db, lote, final)
        lote.status = final
        _registrar_fim(lote)
    except Exception:
        logger.exception("O lote de NFS-e %s parou por erro inesperado", lote.id)
        lote.status = "falhou"
        for linha in lote.linhas:
            if linha.status in (AGUARDANDO, BAIXANDO):
                linha.status = NAO_CONSULTADA
                linha.destino = "O lote parou por um erro inesperado."
        lote.registrar("O lote parou por um erro inesperado. O que foi baixado até aqui "
                       "está gravado; veja o log do servidor.", "erro")
    finally:
        lote.fim = datetime.utcnow()
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        trava.release()


def _uma_empresa(db, rota, lote: Lote, linha: Linha, existentes, arquivos_cert,
                 teto_seg: float) -> None:
    linha.status = BAIXANDO
    lote.registrar(f"{linha.empresa}: consultando o ADN…")
    cnpj = re.sub(r"\D", "", linha.cnpj or "")
    if len(cnpj) != 14:
        _falhou(lote, linha, FALHOU, "Empresa sem CNPJ válido no cadastro.")
        return

    try:
        pfx, senha = certificados.carregar_material(db, linha.tenant_id, cnpj)
    except ValueError as err:
        _falhou(lote, linha, FALHOU, str(err))
        return
    if not senha:
        pfx = b""
        _falhou(lote, linha, SEM_SENHA, "O certificado está sem senha cadastrada.")
        return

    pasta = rota._pasta_do_dia(db, linha.tenant_id, cnpj, existentes, arquivos_cert)

    def ao_gravar(resumo):
        linha.qtd = resumo["arquivos"]
        if lote.parar:
            raise rota.BuscaInterrompida()

    try:
        r = rota._buscar_uma_empresa(db, linha.tenant_id, cnpj, pfx, senha, lote.reiniciar,
                                     limite_seg=teto_seg, teto_seg=teto_seg,
                                     pasta_rel=pasta, ao_gravar=ao_gravar, lote_id=lote.id)
    except Exception:
        logger.exception("Lote NFS-e: falha inesperada no CNPJ %s", cnpj)
        try:
            db.rollback()
        except Exception:
            pass
        _falhou(lote, linha, FALHOU, "Erro inesperado na consulta; veja o log do servidor.")
        return
    finally:
        pfx, senha = b"", ""

    linha.qtd = r["arquivos"]
    linha.chaves = list(r.get("chaves") or [])
    caminho = storage.caminho_completo(pasta)
    partes = []
    if r["novos"]:
        partes.append(f"{r['novos']} nota(s) nova(s)")
    if r["atualizados"]:
        partes.append(f"{r['atualizados']} já conhecida(s)")
    if r["eventos"]:
        partes.append(f"{r['eventos']} evento(s)")
    detalhe = ", ".join(partes)

    if r["erro"]:
        gravados = (f" {linha.qtd} documento(s) gravado(s) antes disso em {caminho}."
                    if linha.qtd else "")
        status = ADIADA if r["tipo_erro"] == "adiada" else FALHOU
        _falhou(lote, linha, status, r["erro"] + gravados)
        return

    if r["parada"] == "interrompida":
        linha.status = INTERROMPIDA
        linha.destino = caminho if linha.qtd else "Parada pedida durante a consulta."
        linha.e_pasta = bool(linha.qtd)
        linha.detalhe = detalhe
        lote.registrar(f"{linha.empresa}: interrompida com {linha.qtd} documento(s).", "aviso")
        return

    if not linha.qtd:
        linha.status = VAZIO
        linha.destino = "Nada novo no ADN desde a última busca."
        lote.registrar(f"{linha.empresa}: nada novo.", "ok")
        return

    linha.destino = caminho
    linha.e_pasta = True
    linha.detalhe = detalhe
    if r["parada"] in ("tempo", "paginas"):
        linha.status = CONTINUA
        linha.detalhe = (detalhe + ". " if detalhe else "") + (
            "Ainda há documentos no ADN: rode o lote de novo para continuar.")
        lote.registrar(f"{linha.empresa}: {linha.qtd} documento(s) — ainda há mais "
                       "no ADN, continua na próxima busca.", "aviso")
    else:
        linha.status = SUCESSO
        lote.registrar(f"{linha.empresa}: {linha.qtd} documento(s) ({detalhe}).", "ok")


def _gerar_relatorio(db, lote: Lote, final: str) -> None:
    """Planilha do lote. Falhar aqui não derruba o lote: as notas já estão
    gravadas, e o log diz que o relatório não saiu."""
    from . import relatorio_lote
    try:
        lote.relatorio = relatorio_lote.gerar(db, lote, final)
    except Exception:
        logger.exception("Relatório do lote de NFS-e %s não montou", lote.id)
        lote.registrar("O relatório do lote não pôde ser gerado; veja o log do servidor.", "erro")
        return
    lote.relatorio_rel = relatorio_lote.salvar(lote, lote.relatorio)
    if lote.relatorio_rel:
        lote.registrar(f"Relatório salvo em {storage.caminho_completo(lote.relatorio_rel)}")
    else:
        lote.registrar("Relatório gerado, mas a cópia na pasta da rede não foi salva "
                       "(pasta indisponível). Baixe pelo botão.", "aviso")


def _falhou(lote: Lote, linha: Linha, status: str, motivo: str) -> None:
    linha.status = status
    linha.destino = motivo
    lote.registrar(f"{linha.empresa}: {status.lower()} — {motivo}",
                   "aviso" if status == ADIADA else "erro")


def _registrar_fim(lote: Lote) -> None:
    c = lote.contagem()
    partes = [f"{c['com_documento']} com documento ({c['documentos']} arquivo(s))",
              f"{c['vazias']} em dia"]
    if c["continua"]:
        partes.append(f"{c['continua']} com mais documentos para a próxima busca")
    if c["adiadas"]:
        partes.append(f"{c['adiadas']} adiada(s) pelo ADN")
    if c["falhas"]:
        partes.append(f"{c['falhas']} com falha")
    if c["interrompidas"] or c["nao_consultadas"]:
        partes.append(f"{c['interrompidas'] + c['nao_consultadas']} não terminada(s)")
    inicio = "Lote parado" if lote.status == "parado" else "Lote NFS-e finalizado"
    tom = ("erro" if c["falhas"] else
           "aviso" if (c["adiadas"] or c["continua"] or c["interrompidas"]
                       or c["nao_consultadas"]) else "ok")
    lote.registrar(f"{inicio}: " + ", ".join(partes) + ".", tom)
