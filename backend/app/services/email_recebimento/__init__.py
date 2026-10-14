"""Leitura da caixa de e-mail do escritório (28/09/2026).

- `imap`: conversa com o servidor, SÓ LEITURA.
- `anexos`: abre o e-mail, tira os XMLs dos anexos (soltos, ZIP ou RAR) e diz
  de que empresa é cada um.
- `armazenamento`: onde o XML aproveitado fica em disco.
- `leitura`: uma rodada (de hora em hora, pelo agendador das Automações).
- `recebidos`: o que as telas de Auditoria e Gerar SPED consomem.
- `resumo`: o informativo da manhã, com o texto da IA.
"""
