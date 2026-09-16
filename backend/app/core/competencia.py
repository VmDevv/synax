"""A competência do SPED — o mês que a auditoria realmente cobre.

Até 03/09/2026 o sistema só sabia QUANDO alguém subiu o arquivo. Isso não
responde a pergunta que o escritório faz todo mês: *"o abril desta empresa já
foi conferido?"*. Rodar abril em setembro é rotina — atraso do cliente,
retificação, competência reaberta —, então a data do upload não diz nada sobre
o mês auditado. A competência vem do registro **0000** do SPED (campo DT_INI) e
é gravada em `audit_tasks.competencia`.

Decisões:

1. **Formato "AAAA-MM", texto.** Ordena cronologicamente como string, agrupa
   direto no banco e não depende de fuso — a competência é um mês do
   calendário fiscal, não um instante. Para ler, `rotulo()` devolve "04/2026",
   que é como a pessoa fala.

2. **O mês corrente NUNCA conta como atrasado.** O SPED de setembro se entrega
   em outubro; cobrar a competência em curso acusaria toda empresa da carteira
   todo dia do mês. `fechada()` devolve o mês anterior, e é ele o último que a
   ficha cobra.

3. **Sem 0000 legível, a competência é `None` — nunca uma data chutada.** O
   parser já teve `"2026-01-01"` fixo como padrão: um arquivo com cabeçalho
   ilegível era gravado como janeiro, e a tela afirmaria com toda a confiança
   que janeiro estava auditado. `None` diz "não sei", que é a verdade.
"""
from __future__ import annotations

import re
from datetime import date

FORMATO = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Padrão do próprio SPED e do resto do sistema: AAAA-MM-DD.
_DATA_ISO = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-\d{2}")


def de_data(data_iso) -> str | None:
    """"2026-04-01" -> "2026-04". Qualquer outra coisa vira None."""
    achado = _DATA_ISO.match(str(data_iso or "").strip())
    return f"{achado.group(1)}-{achado.group(2)}" if achado else None


def valida(comp) -> str | None:
    """Devolve a competência se ela for uma competência; senão, None."""
    texto = str(comp or "").strip()
    return texto if FORMATO.match(texto) else None


def rotulo(comp) -> str:
    """"2026-04" -> "04/2026". O que não for competência vira travessão."""
    ok = valida(comp)
    return f"{ok[5:]}/{ok[:4]}" if ok else "—"


def mes_curto(comp) -> str:
    """"2026-04" -> "abr". Para o eixo de um gráfico, onde não cabe mais."""
    MESES = ("jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez")
    ok = valida(comp)
    return MESES[int(ok[5:]) - 1] if ok else "—"


def anterior(comp: str) -> str:
    """O mês antes de `comp`. Vira o ano sozinho."""
    ano, mes = int(comp[:4]), int(comp[5:])
    return f"{ano - 1}-12" if mes == 1 else f"{ano}-{mes - 1:02d}"


def fechada(hoje: date | None = None) -> str:
    """A última competência que já dá para cobrar: o mês ANTERIOR ao de hoje."""
    hoje = hoje or date.today()
    return anterior(f"{hoje.year}-{hoje.month:02d}")


def sequencia(quantidade: int, ate: str | None = None) -> list[str]:
    """As `quantidade` competências que terminam em `ate`, em ordem cronológica.

    `ate` vazio significa a última fechada. Usada para desenhar a régua de
    meses: o mês sem auditoria precisa aparecer como coluna vazia, senão o
    gráfico esconde justamente o que interessa ver.
    """
    fim = valida(ate) or fechada()
    meses = [fim]
    for _ in range(max(quantidade, 1) - 1):
        meses.append(anterior(meses[-1]))
    return list(reversed(meses))
