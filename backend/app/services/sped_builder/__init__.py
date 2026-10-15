"""Monta um SPED Fiscal a partir dos XMLs de entrada.

Para que serve: empresa que não escritura e não gera SPED. O escritório junta os
XMLs do período, sai um arquivo no leiaute da EFD ICMS/IPI, e ele é IMPORTADO no
Domínio — que a partir dali cadastra produtos, participantes e lança as notas.
Poupa a digitação de centenas de documentos.

**O que sai daqui é um arquivo de ENTRADAS, não uma escrituração completa.** Isso
não é limitação de implementação, é o que os XMLs contêm:

- **Não há saídas.** Só entram os documentos em que a empresa é a destinatária.
  Sem saídas não há apuração, e por isso o Bloco E sai declarado como "sem
  dados" em vez de inventado — quem apura é o Domínio, depois de receber os
  lançamentos.
- **Não há inventário, ativo, produção nem energia** (Blocos H, G, K e os C500).
  Nenhum deles nasce de nota fiscal de terceiro.
- **O CFOP de entrada não existe no XML.** O documento traz o CFOP do
  FORNECEDOR (5xxx/6xxx) e o do TRANSPORTADOR no CT-e. O de entrada sai das
  regras do PRÓPRIO gerador (`GeradorCfopRegra` para o item, `GeradorCteCfopRegra`
  para o frete) — separadas das da auditoria, com o mesmo maker-checker. Desde
  09/10/2026 as rotas geram com `exigir_regra`: sem regra aprovada para cada
  item e cada frete, o arquivo não sai. A conversão do primeiro dígito (5→1,
  6→2, 7→3) virou só a sugestão que pré-preenche a tela de mapeamento — e
  só quando o espelho existe na tabela oficial de CFOP: 5405 não vira 1405.
  Para algumas saídas sem espelho, um mapa curto sugere a equivalência (5405 →
  1403), também como presunção e também exigindo aprovação.

Tudo que o gerador preencheu sem ter no XML vira uma `Presuncao` e aparece no
relatório. É a regra da casa: o que a máquina supôs tem de estar visível antes
de alguém assinar embaixo.
"""
from .gerador import (Cadastro, Presuncao, Resultado, cfop_de_entrada,
                      cfop_de_entrada_valido, chave_frete, chave_item,
                      deduzir_cfop, empresa_industrial, gerar,
                      gerar_de_pasta, id_documento, levantar_combinacoes,
                      levantar_combinacoes_da_pasta, rotulo_mes)

__all__ = ["Cadastro", "Presuncao", "Resultado", "cfop_de_entrada",
           "cfop_de_entrada_valido", "chave_frete", "chave_item",
           "deduzir_cfop", "empresa_industrial", "gerar",
           "gerar_de_pasta", "id_documento", "levantar_combinacoes",
           "levantar_combinacoes_da_pasta", "rotulo_mes"]
