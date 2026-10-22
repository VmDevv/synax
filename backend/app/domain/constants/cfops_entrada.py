"""Descrições dos CFOPs de entrada (Convênio S/Nº de 1970 — Ajuste SINIEF).

Usado só para rotular as opções na interface. A decisão fiscal continua sendo do
operador/admin: nada aqui é aplicado automaticamente.

Série 1xxx = entrada estadual · 2xxx = interestadual · 3xxx = importação.
"""

CFOPS_ENTRADA: dict[str, str] = {
    # --- Compras para industrialização / comercialização ---
    "1101": "Compra para industrialização",
    "2101": "Compra para industrialização (interestadual)",
    "1102": "Compra para comercialização",
    "2102": "Compra para comercialização (interestadual)",
    "1111": "Compra para industrialização de mercadoria recebida em consignação",
    "1116": "Compra para industrialização originada de encomenda para recebimento futuro",
    "1120": "Compra para industrialização, em venda à ordem",
    "1122": "Compra para industrialização com mercadoria sujeita a ST",
    "1124": "Industrialização efetuada por outra empresa",
    "1126": "Compra para utilização na prestação de serviço",
    "2126": "Compra para utilização na prestação de serviço (interestadual)",

    # --- Transferências ---
    "1151": "Transferência para industrialização",
    "2151": "Transferência para industrialização (interestadual)",
    "1152": "Transferência para comercialização",
    "2152": "Transferência para comercialização (interestadual)",
    "1154": "Transferência para utilização na prestação de serviço",
    "2154": "Transferência para utilização na prestação de serviço (interestadual)",

    # --- Devoluções ---
    "1201": "Devolução de venda de produção do estabelecimento",
    "2201": "Devolução de venda de produção (interestadual)",
    "1202": "Devolução de venda de mercadoria adquirida de terceiros",
    "2202": "Devolução de venda de mercadoria de terceiros (interestadual)",
    "1410": "Devolução de venda de produção com ST",
    "1411": "Devolução de venda de mercadoria de terceiros com ST",

    # --- Substituição tributária ---
    "1401": "Compra para industrialização com ST",
    "2401": "Compra para industrialização com ST (interestadual)",
    "1403": "Compra para comercialização com ST",
    "2403": "Compra para comercialização com ST (interestadual)",
    "1407": "Compra de material de uso ou consumo com ST",
    "2407": "Compra de material de uso ou consumo com ST (interestadual)",

    # --- Ativo imobilizado e uso/consumo ---
    "1551": "Compra de bem para o ativo imobilizado",
    "2551": "Compra de bem para o ativo imobilizado (interestadual)",
    "1556": "Compra de material para uso ou consumo",
    "2556": "Compra de material para uso ou consumo (interestadual)",
    "1557": "Transferência de material para uso ou consumo",

    # --- Combustíveis e serviços ---
    "1653": "Compra de combustível ou lubrificante para comercialização",
    "2653": "Compra de combustível ou lubrificante para comercialização (interestadual)",
    "1352": "Aquisição de serviço de transporte por estabelecimento industrial",
    "1353": "Aquisição de serviço de transporte por estabelecimento comercial",
    # Frete iniciado em UF diferente da inscrição do prestador — CFOP de CT-e.
    # O seletor da aba de CT-e usa esta mesma tabela.
    "1932": "Aquisição de serviço de transporte iniciado em UF diversa da inscrição do prestador",
    "2932": "Aquisição de serviço de transporte iniciado em UF diversa da inscrição do prestador (interestadual)",

    # --- Remessas e retornos ---
    "1902": "Retorno de mercadoria remetida para industrialização por encomenda",
    "1904": "Retorno de remessa para venda fora do estabelecimento",
    "1908": "Entrada de bem por conta de contrato de comodato ou locação",
    "2908": "Entrada de bem por conta de contrato de comodato ou locação (interestadual)",
    "1910": "Entrada de bonificação, doação ou brinde",
    "2910": "Entrada de bonificação, doação ou brinde (interestadual)",
    "1911": "Entrada de amostra grátis",
    "2911": "Entrada de amostra grátis (interestadual)",
    "1915": "Entrada de mercadoria para conserto ou reparo",
    "1916": "Retorno de mercadoria remetida para conserto ou reparo",
    "2916": "Retorno de mercadoria remetida para conserto ou reparo (interestadual)",
    "1920": "Entrada de vasilhame ou sacaria",
    "2920": "Entrada de vasilhame ou sacaria (interestadual)",
    "1922": "Simples faturamento decorrente de compra para recebimento futuro",
    "2922": "Simples faturamento decorrente de compra para recebimento futuro (interestadual)",
    "1949": "Outra entrada de mercadoria ou prestação de serviço",
    "2949": "Outra entrada de mercadoria ou prestação de serviço (interestadual)",

    # --- Importação ---
    "3101": "Compra para industrialização (importação)",
    "3102": "Compra para comercialização (importação)",
    "3551": "Compra de bem para o ativo imobilizado (importação)",
    "3556": "Compra de material para uso ou consumo (importação)",
}


def descrever(cfop: str) -> str:
    """Rótulo curto do CFOP. Devolve string vazia se não for conhecido."""
    return CFOPS_ENTRADA.get(str(cfop or "").strip(), "")


def rotulo(cfop: str) -> str:
    """'1102 — Compra para comercialização', ou só o código se não houver descrição."""
    d = descrever(cfop)
    return f"{cfop} — {d}" if d else str(cfop)
