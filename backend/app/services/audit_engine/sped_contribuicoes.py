"""O leitor do EFD Contribuições (PIS/COFINS), ligado pela caixa da tela.

Por que existe: o crédito de PIS/COFINS da auditoria é conferido BASE contra
BASE — a base recalculada pela tese do STF, feita no `xml_parser`, contra o
`VL_BC_PIS` do C170. Só que o SPED Fiscal quase nunca preenche esse campo,
porque PIS/COFINS é escriturado no OUTRO arquivo. Medido nos arquivos reais de
agosto/2026 da MEGA:

    C170 com VL_BC_PIS preenchido    EFD ICMS/IPI      EFD Contribuições
    MEGA (matriz)                    65%               100%
    outro cliente do escritório      0%                --

É por isso que a linha "SPED R$ 0,00 vs Base Calculada R$ 820,00" é esperada no
arquivo Fiscal e foi aceita como tal (decisão do Sandro em 28/08/2026). **Este
leitor não muda essa decisão** — o confronto continua base contra base, com as
mesmas regras. O que muda é que, lendo o arquivo certo, o lado do SPED passa a
existir de verdade.

O que este módulo NÃO faz, e é de propósito:

- **Não apura.** O EFD Contribuições apura no Bloco M (M105 → M100 para o
  crédito, M210 → M200 para a contribuição, e o espelho M505/M500 e M610/M600
  da COFINS). É outra cascata, mais funda que a do E110, e mexer nela é mexer
  em imposto a recolher. A auditoria lê os documentos; a apuração fica intocada.
- **Não corrige.** A tela "Corrigir SPED" e toda a cascata C170 → C190 → C100
  são do leiaute Fiscal. No arquivo de Contribuições não existe C190, e a
  cascata CRIARIA registros que o leiaute não tem — conferido em memória: uma
  única troca de CFOP injetava dois C190 e três linhas novas, reescrevia o ICMS
  do C100 e não encostava em nenhum campo de PIS/COFINS. A correção segue
  disponível só para o arquivo Fiscal.
- **Não divide por estabelecimento.** Este arquivo é por PESSOA JURÍDICA e
  cobre todas as filiais (o da MEGA traz seis, todas da mesma raiz de CNPJ),
  enquanto o Fiscal é de um estabelecimento só. O leitor REGISTRA de qual filial
  é cada documento (`estabelecimento`, do registro C010) e devolve a lista em
  `estabelecimentos`, mas não decide escopo: a auditoria é da empresa escolhida
  na tela, e separar isso é decisão de negócio, não de leitura.

O percurso do arquivo é o mesmo do leitor Fiscal — ver `sped_parser.percorrer`.
Todos os campos que a auditoria lê no C100, no C170 e no D100 estão nas mesmas
posições nos dois leiautes; o que difere fica no FIM do registro e não é usado.
A única tabela própria é a do 0000, abaixo.
"""
from .sped_parser import CONTRIBUICOES, Leiaute, percorrer

# Registro 0000 do EFD Contribuições (Guia Prático, registro 0000):
# |1 REG|2 COD_VER|3 TIPO_ESCRIT|4 IND_SIT_ESP|5 NUM_REC_ANTERIOR|6 DT_INI|
# |7 DT_FIN|8 NOME|9 CNPJ|10 UF|11 COD_MUN|12 SUFRAMA|13 IND_NAT_PJ|14 IND_ATIV|
#
# São DOIS campos a mais antes das datas do que no EFD ICMS/IPI, onde DT_INI é o
# campo 04 e o CNPJ é o 07. Ler um pelo outro não dá erro: o CNPJ sai com oito
# dígitos (é a data final), o nome da empresa sai como uma data, a competência
# sai nula — e a auditoria roda inteira assim mesmo. Por isso `ler_sped` confere
# o arquivo contra a opção marcada antes de começar.
LEIAUTE_CONTRIBUICOES = Leiaute(
    nome=CONTRIBUICOES, rotulo="EFD Contribuições (PIS/COFINS)",
    dt_ini=6, dt_fin=7, nome_empresa=8, cnpj=9)


def parse_sped_contribuicoes(path):
    """Lê o EFD Contribuições com as mesmas regras do leitor Fiscal."""
    return percorrer(path, LEIAUTE_CONTRIBUICOES)
