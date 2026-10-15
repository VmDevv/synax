"""Automações do escritório além da coleta de NFS-e: os informativos.

A coleta de NFS-e (services/nfse/automacao.py) veio primeiro e é dona do
agendador — a thread, a trava contra rodar em dobro, o histórico e o envio de
e-mail. Os informativos (25/09/2026, pedido do Sandro) reaproveitam tudo isso
e só dizem O QUE levantar:

- `resumo_auditorias`: auditorias do período, inconsistências e valor em jogo;
- `informe_erros`: o que a aba "Erros do Sistema" registrou no período.

`tipos` é a fonte única do que cada tipo aceita de configuração, e `janela`
diz qual período uma rodada cobre.
"""
