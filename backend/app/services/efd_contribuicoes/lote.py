"""O lote do Presumido em segundo plano, com o andamento na tela — o desenho de `services/nfse/lote.py`.

O clique só COMEÇA o lote e volta na hora; uma thread roda o programa SPED PIS/COFINS (`lote --json`) e lê as linhas
de progresso que ele já imprime (`[n/total] empresa`), e a tela pergunta o andamento a cada 2 s. O resultado — os
arquivos em `<pasta>\\AAAA-MM`, a planilha de resumo, o que não passou na validação em `_com_erro` — é do programa, e
fica no histórico dele, o mesmo que a tela de lá mostra.

Três regras:

- **Um lote por vez.** Daqui, pela trava do processo; e o programa pode estar rodando um lote pedido pela tela dele
  ou pela automação mensal, que gravam na MESMA pasta do mês — por isso, antes de começar, o histórico de lá é
  consultado, e lote "rodando" há menos de `JANELA_OCUPADO` recusa o pedido. Mais velho que isso é sobra de um
  servidor que caiu no meio (lá ele só vira "interrompida" quando a tela de lá reinicia).
- **O estado vive na memória.** Reiniciar o Synax no meio perde a barra de progresso, não o lote: o processo do
  programa segue até o fim e o resultado aparece no Histórico.
- **Nunca "verde com zero".** O fim é a mensagem do próprio programa ("31 conferem · 5 diferentes · …"), e lote que
  falhou antes de começar diz por quê.
"""
import logging
import re
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import programa

logger = logging.getLogger(__name__)

JANELA_OCUPADO = timedelta(hours=2)
_PROGRESSO = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*(.*)$")


class LoteOcupado(Exception):
    """Já há um lote do Presumido rodando, daqui ou do programa. A mensagem vai para a tela."""


@dataclass
class Lote:
    id: str
    competencia: str
    codigos: list[int]
    pedido_por: str
    total: int
    feitos: int = 0
    atual: str = ""
    inicio: datetime = field(default_factory=datetime.now)
    fim: datetime | None = None
    execucao: dict | None = None      # a execução que o programa devolveu no fim (histórico de lá)
    erro: str = ""

    @property
    def rodando(self) -> bool:
        return self.fim is None

    @property
    def percentual(self) -> int:
        return int(100 * self.feitos / self.total) if self.total else 0


_guarda = threading.Lock()
_atual: Lote | None = None
_thread: threading.Thread | None = None


def atual() -> Lote | None:
    return _atual


def _rodando_no_programa(agora: datetime) -> dict | None:
    for ex in programa.historico(limite=10):
        if ex.get("status") != "rodando":
            continue
        try:
            inicio = datetime.fromisoformat(str(ex.get("inicio")))
        except ValueError:
            continue
        if inicio.tzinfo is not None:
            inicio = inicio.astimezone().replace(tzinfo=None)
        if agora - inicio < JANELA_OCUPADO:
            return ex
    return None


def iniciar(competencia: str, codigos: list[int], pedido_por: str) -> Lote:
    """Começa o lote e volta na hora. `codigos` são os codi_emp do Domínio que a pessoa pode gerar (a rota já
    conferiu o acesso)."""
    global _atual, _thread
    with _guarda:
        if _atual is not None and _atual.rodando:
            raise LoteOcupado(f"Já há um lote rodando, pedido por {_atual.pedido_por} às "
                              f"{_atual.inicio:%H:%M}. Espere ele terminar.")
        outro = _rodando_no_programa(datetime.now())
        if outro:
            raise LoteOcupado(f"O programa SPED PIS/COFINS está rodando outro lote ({outro.get('titulo')}, pedido "
                              f"por {outro.get('origem')}). Espere ele terminar: os dois gravariam na mesma pasta.")
        cmd, raiz, env = programa.comando(programa.args_do_lote(competencia, codigos, f"Synax · {pedido_por}"))
        lote = Lote(id=str(uuid.uuid4()), competencia=competencia, codigos=list(codigos), pedido_por=pedido_por,
                    total=len(codigos))
        _atual = lote
        _thread = threading.Thread(target=_rodar, args=(lote, cmd, raiz, env), name="lote-presumido", daemon=True)
        _thread.start()
        return lote


def esperar(segundos: float = 30.0) -> None:
    """Para os testes: espera a thread do lote terminar."""
    t = _thread
    if t is not None:
        t.join(segundos)


def _rodar(lote: Lote, cmd: list[str], raiz, env) -> None:
    ultima = ""
    try:
        # stderr num arquivo, não num pipe: aviso de biblioteca no stderr não pode encher o pipe e travar o lote
        # enquanto a thread só lê o stdout.
        with tempfile.TemporaryFile() as erros:
            with subprocess.Popen(cmd, cwd=raiz, env=env, stdout=subprocess.PIPE, stderr=erros, text=True,
                                  encoding="utf-8", errors="replace", creationflags=programa.SEM_JANELA) as proc:
                for linha in proc.stdout:
                    if not linha.strip():
                        continue
                    ultima = linha
                    m = _PROGRESSO.match(linha)
                    if m:
                        lote.feitos, lote.total, lote.atual = int(m.group(1)) - 1, int(m.group(2)), m.group(3).strip()
                codigo = proc.wait()
            erros.seek(0)
            stderr = erros.read().decode("utf-8", "replace")
        lote.execucao = programa.ler_resposta(ultima, stderr, codigo)["execucao"]
        lote.feitos, lote.atual = lote.total, ""
    except (programa.ProgramaIndisponivel, programa.FalhaDoPrograma) as exc:
        lote.erro = str(exc)
    except FileNotFoundError:
        lote.erro = "O Python do programa SPED PIS/COFINS não foi encontrado nesta máquina."
    except Exception as exc:  # noqa: BLE001 — a tela precisa saber que o lote acabou, seja como for
        logger.exception("Lote do Presumido falhou")
        lote.erro = f"O lote parou com um erro inesperado ({type(exc).__name__}); o detalhe está no log do servidor."
    finally:
        lote.fim = datetime.now()
        logger.info("Lote do Presumido %s %s por %s: %s", lote.competencia, lote.id, lote.pedido_por,
                    lote.erro or (lote.execucao or {}).get("mensagem"))
