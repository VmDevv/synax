import os

def clean_float(val):
    if not val:
        return 0.0
    val = val.strip().replace(".", "").replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0

# COD_SIT (campo 6 do C100/D100) que tira o documento da apuração: cancelado,
# escrituração extemporânea de cancelado, denegado e numeração inutilizada. Não
# há crédito a conferir em nenhum deles.
#
# Todo o RESTO entra — e isso inclui os que ficavam de fora antes: 01
# (extemporâneo regular), 06 (COMPLEMENTAR), 07 (complementar extemporâneo) e 08
# (regime especial). O filtro era `sit == "00"`, e por isso o CT-e 211146 da
# Transportes Mobiline, escriturado com COD_SIT 06, sumia do motor inteiro: o
# documento nunca chegava a `d100_entries`, então o crédito de R$ 30,45 do XML
# contra os R$ 0,00 do D190 não tinha como ser comparado. Descartar calado é o
# pior dos mundos — o documento existe, tem imposto, e ninguém é avisado.
SITUACOES_SEM_EFEITO = {"02", "03", "04", "05"}


def parse_sped(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo SPED não encontrado: {path}")
        
    company_cnpj = ""
    company_name = ""
    # NULO quando o 0000 não trouxer as datas. Aqui já esteve "2026-01-01"
    # fixo: arquivo com cabeçalho ilegível saía gravado como janeiro, e a
    # competência da auditoria — que decide se o mês da empresa está em aberto
    # — afirmava um mês que ninguém leu. Quem consome trata a ausência: a busca
    # no SIEG fica indisponível e a planilha omite a linha do período.
    periodo_inicio = None
    periodo_fim = None
    c100_entries = {}
    d100_entries = {}
    prod_descriptions = {}
    # {COD_PART: {"nome", "cnpj", "cpf", ...}} — ver o 0150 adiante. Era
    # {COD_PART: nome}; quem consome deve usar os helpers do cross_checker
    # (`nome_participante` / `id_participante`) em vez de indexar direto.
    participants = {}
    seen_keys = set()
    duplicate_nfs = []
    # O arquivo escritura PIS/COFINS de frete? Só é verdadeiro se algum D101 ou
    # D105 apareceu. Sem isso, "SPED zerado" significa que o layout não tem o
    # dado — não que o cliente deixou de creditar.
    bloco_d_tem_pis_cofins = False
    
    with open(path, "r", encoding="latin1") as f:
        current_c100 = None
        current_d100 = None
        for line in f:
            if not line.startswith("|"):
                continue
            fields = line.strip().split("|")
            if len(fields) < 2:
                continue
            reg = fields[1]
            
            if reg == "0000":
                company_cnpj = fields[7] if len(fields) > 7 else ""
                company_name = fields[6] if len(fields) > 6 else ""
                dt_ini_raw = fields[4] if len(fields) > 4 else ""
                dt_fin_raw = fields[5] if len(fields) > 5 else ""
                if len(dt_ini_raw) == 8:
                    periodo_inicio = f"{dt_ini_raw[4:8]}-{dt_ini_raw[2:4]}-{dt_ini_raw[0:2]}"
                if len(dt_fin_raw) == 8:
                    periodo_fim = f"{dt_fin_raw[4:8]}-{dt_fin_raw[2:4]}-{dt_fin_raw[0:2]}"
            elif reg == "0150":
                # Layout 0150: |1 REG|2 COD_PART|3 NOME|4 COD_PAIS|5 CNPJ|6 CPF|
                #              |7 IE|8 COD_MUN|9 SUFRAMA|10 END|...
                #
                # Aqui guardava-se SÓ o nome. O CNPJ do participante é o único
                # lugar do SPED que diz de QUEM é a nota — o C100 traz apenas o
                # COD_PART, um código interno do cliente —, e sem ele não havia
                # como conferir contra o emitente do XML. Nota lançada no
                # fornecedor errado passava limpa: os valores batiam (são os do
                # XML certo), só o participante estava trocado.
                cod_part = fields[2] if len(fields) > 2 else ""
                participants[cod_part] = {
                    "cod": cod_part,
                    "nome": fields[3] if len(fields) > 3 else "",
                    "cod_pais": fields[4] if len(fields) > 4 else "",
                    "cnpj": (fields[5] if len(fields) > 5 else "").strip(),
                    "cpf": (fields[6] if len(fields) > 6 else "").strip(),
                    "ie": fields[7] if len(fields) > 7 else "",
                    "cod_mun": fields[8] if len(fields) > 8 else "",
                }
            elif reg == "0200":
                cod_item = fields[2] if len(fields) > 2 else ""
                descr = fields[3] if len(fields) > 3 else ""
                prod_descriptions[cod_item] = descr
            elif reg == "C100":
                oper = fields[2] if len(fields) > 2 else ""
                ind_emit = fields[3] if len(fields) > 3 else ""
                emit = fields[4] if len(fields) > 4 else ""
                mod = fields[5] if len(fields) > 5 else ""
                sit = fields[6] if len(fields) > 6 else ""
                num = fields[8] if len(fields) > 8 else ""
                key = fields[9] if len(fields) > 9 else ""
                vl_doc = clean_float(fields[12]) if len(fields) > 12 else 0.0
                vl_icms = clean_float(fields[22]) if len(fields) > 22 else 0.0
                vl_icms_st = clean_float(fields[24]) if len(fields) > 24 else 0.0
                
                if oper in ["0", "1"] and sit not in SITUACOES_SEM_EFEITO:
                    if key and key in seen_keys:
                        duplicate_nfs.append({
                            "chave": key,
                            "num": num,
                            "ind_oper": oper,
                            "vl_doc": vl_doc,
                            "tipo": "NF-e",
                            "tipo_operacao": "Entrada" if oper == "0" else "Saída"
                        })
                    elif key:
                        seen_keys.add(key)
                    current_c100 = key
                    c100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "ind_oper": oper,
                        "vl_doc": vl_doc,
                        "vl_icms": vl_icms,
                        "vl_icms_st": vl_icms_st,
                        "vl_opr_c190_sum": 0.0,
                        "items": []
                    }
                else:
                    current_c100 = None
            elif reg == "C170" and current_c100:
                num_item = fields[2] if len(fields) > 2 else ""
                cod_item = fields[3] if len(fields) > 3 else ""
                qtd = clean_float(fields[5]) if len(fields) > 5 else 0.0
                vl_item = clean_float(fields[7]) if len(fields) > 7 else 0.0
                cst = fields[10] if len(fields) > 10 else ""
                cfop = fields[11] if len(fields) > 11 else ""
                vl_bc_icms = clean_float(fields[13]) if len(fields) > 13 else 0.0
                vl_icms = clean_float(fields[15]) if len(fields) > 15 else 0.0
                vl_bc_icms_st = clean_float(fields[16]) if len(fields) > 16 else 0.0
                vl_icms_st = clean_float(fields[18]) if len(fields) > 18 else 0.0
                
                # Novas Extrações - IPI
                cst_ipi = fields[20] if len(fields) > 20 else ""
                vl_bc_ipi = clean_float(fields[22]) if len(fields) > 22 else 0.0
                vl_ipi = clean_float(fields[24]) if len(fields) > 24 else 0.0
                
                # Novas Extrações - PIS
                cst_pis = fields[25] if len(fields) > 25 else ""
                vl_bc_pis = clean_float(fields[26]) if len(fields) > 26 else 0.0
                vl_pis = clean_float(fields[30]) if len(fields) > 30 else 0.0
                
                # Novas Extrações - COFINS
                cst_cofins = fields[31] if len(fields) > 31 else ""
                vl_bc_cofins = clean_float(fields[32]) if len(fields) > 32 else 0.0
                vl_cofins = clean_float(fields[36]) if len(fields) > 36 else 0.0
                
                c100_entries[current_c100]["items"].append({
                    "num_item": num_item,
                    "cod_item": cod_item,
                    "qtd": qtd,
                    "vl_item": vl_item,
                    "cst": cst,
                    "cfop": cfop,
                    "vl_bc_icms": vl_bc_icms,
                    "vl_icms": vl_icms,
                    "vl_bc_icms_st": vl_bc_icms_st,
                    "vl_icms_st": vl_icms_st,
                    "cst_ipi": cst_ipi,
                    "vl_bc_ipi": vl_bc_ipi,
                    "vl_ipi": vl_ipi,
                    "cst_pis": cst_pis,
                    "vl_bc_pis": vl_bc_pis,
                    "vl_pis": vl_pis,
                    "cst_cofins": cst_cofins,
                    "vl_bc_cofins": vl_bc_cofins,
                    "vl_cofins": vl_cofins
                })
            elif reg == "C190" and current_c100:
                vl_opr = clean_float(fields[5]) if len(fields) > 5 else 0.0
                if current_c100 in c100_entries:
                    c100_entries[current_c100]["vl_opr_c190_sum"] = round(
                        c100_entries[current_c100].get("vl_opr_c190_sum", 0.0) + vl_opr, 2
                    )
            elif reg == "D100":
                oper = fields[2] if len(fields) > 2 else ""
                ind_emit = fields[3] if len(fields) > 3 else ""
                emit = fields[4] if len(fields) > 4 else ""
                mod = fields[5] if len(fields) > 5 else ""
                sit = fields[6] if len(fields) > 6 else ""
                num = fields[9] if len(fields) > 9 else ""
                key = fields[10] if len(fields) > 10 else ""
                # Layout D100: ...|14 CHV_REF|15 VL_DOC|16 VL_DESC|17 IND_FRT|
                #              18 VL_SERV|19 VL_BC_ICMS|20 VL_ICMS|21 VL_NT|22 COD_INF|
                # O índice 22 (COD_INF) vinha sendo lido como se fosse o ICMS — é um
                # código, quase sempre vazio, então o ICMS do CT-e chegava zerado.
                vl_doc = clean_float(fields[15]) if len(fields) > 15 else 0.0
                vl_serv = clean_float(fields[18]) if len(fields) > 18 else 0.0
                vl_bc_icms = clean_float(fields[19]) if len(fields) > 19 else 0.0
                vl_icms = clean_float(fields[20]) if len(fields) > 20 else 0.0
                
                if oper in ["0", "1"] and sit not in SITUACOES_SEM_EFEITO:
                    if key and key in seen_keys:
                        duplicate_nfs.append({
                            "chave": key,
                            "num": num,
                            "ind_oper": oper,
                            "vl_doc": vl_doc,
                            "tipo": "CT-e",
                            "tipo_operacao": "Entrada" if oper == "0" else "Saída"
                        })
                    elif key:
                        seen_keys.add(key)
                    current_d100 = key
                    d100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "ind_oper": oper,
                        # Valores do cabeçalho, usados só como fallback quando o
                        # documento não traz analítico (D190).
                        "vl_doc_cab": vl_doc,
                        "vl_serv_cab": vl_serv,
                        "vl_bc_cab": vl_bc_icms,
                        "vl_icms_cab": vl_icms,
                        # Somatório do analítico D190 — é a fonte oficial.
                        "tem_d190": False,
                        "vl_opr_d190": 0.0,
                        "vl_bc_d190": 0.0,
                        "vl_icms_d190": 0.0,
                        "cfops": [],
                        # PIS/COFINS do frete (D101 e D105). Ficam zerados quando o
                        # arquivo não traz esses registros — ver `tem_pis_cofins`.
                        "cst_pis": "",
                        "vl_bc_pis": 0.0,
                        "vl_pis": 0.0,
                        "cst_cofins": "",
                        "vl_bc_cofins": 0.0,
                        "vl_cofins": 0.0,
                        "tem_pis_cofins": False,
                    }
                else:
                    current_d100 = None
            elif reg == "D190" and current_d100:
                # Layout D190: |1 REG|2 CST_ICMS|3 CFOP|4 ALIQ_ICMS|5 VL_OPR|
                #              |6 VL_BC_ICMS|7 VL_ICMS|8 VL_RED_BC|9 COD_OBS|
                # Um CT-e pode ter vários D190 (CST/CFOP/alíquota diferentes);
                # por isso somamos em vez de sobrescrever.
                cfop = fields[3] if len(fields) > 3 else ""
                entrada = d100_entries[current_d100]
                if cfop and cfop not in entrada["cfops"]:
                    entrada["cfops"].append(cfop)
                entrada["tem_d190"] = True
                entrada["vl_opr_d190"] += clean_float(fields[5]) if len(fields) > 5 else 0.0
                entrada["vl_bc_d190"] += clean_float(fields[6]) if len(fields) > 6 else 0.0
                entrada["vl_icms_d190"] += clean_float(fields[7]) if len(fields) > 7 else 0.0
            elif reg in ("D101", "D105") and current_d100:
                # PIS/COFINS do serviço de transporte. Layout (EFD Contribuições):
                # |1 REG|2 IND_NAT_FRT|3 VL_ITEM|4 CST|5 NAT_BC_CRED|6 VL_BC|
                # |7 ALIQ|8 VL|9 COD_CTA|  — D101 é o PIS, D105 é a COFINS.
                #
                # CUIDADO: "D101" existe nos DOIS arquivos e são registros
                # DIFERENTES. No SPED Fiscal (EFD ICMS/IPI) o D101 é o
                # complemento de DIFAL da EC 87/15 —
                # |1 REG|2 VL_FCP_UF_DEST|3 VL_ICMS_UF_DEST|4 VL_ICMS_UF_REM|
                # |5 COD_CTA| — e lê-lo como se fosse PIS transforma o valor de
                # ICMS do remetente em "CST" e marca o documento como
                # escriturado, fazendo o motor cobrar crédito que não existe.
                # Por isso a linha só é aceita quando tem a forma do registro
                # de Contribuições: o campo 4 é um CST de dois dígitos.
                cst = fields[4].strip() if len(fields) > 4 else ""
                if not (len(fields) > 8 and len(cst) == 2 and cst.isdigit()):
                    continue

                # NÃO existe PIS/COFINS de frete no D100 nem no D190: quem não
                # traz esses dois registros não escriturou nada, e o motor não
                # pode confundir ausência com erro. Daí `tem_pis_cofins`.
                #
                # Somamos em vez de sobrescrever pelo mesmo motivo do D190: o
                # documento pode trazer mais de um registro (naturezas de base
                # de crédito diferentes) e cada um é uma parcela do total.
                entrada = d100_entries[current_d100]
                vl_bc = clean_float(fields[6])
                vl = clean_float(fields[8])
                sufixo = "pis" if reg == "D101" else "cofins"
                if not entrada[f"cst_{sufixo}"]:
                    entrada[f"cst_{sufixo}"] = cst
                entrada[f"vl_bc_{sufixo}"] = round(entrada[f"vl_bc_{sufixo}"] + vl_bc, 2)
                entrada[f"vl_{sufixo}"] = round(entrada[f"vl_{sufixo}"] + vl, 2)
                entrada["tem_pis_cofins"] = True
                bloco_d_tem_pis_cofins = True

    # Consolida os valores oficiais de cada CT-e.
    # Regra: quando existe analítico (D190), ele manda — é onde ficam o valor da
    # prestação e o ICMS efetivamente escriturados por CFOP. O cabeçalho D100 só
    # entra quando o documento não traz D190 algum.
    for _d in d100_entries.values():
        if _d["tem_d190"]:
            _d["vl_doc"] = _d["vl_opr_d190"]
            _d["vl_bc_icms"] = _d["vl_bc_d190"]
            _d["vl_icms"] = _d["vl_icms_d190"]
        else:
            _d["vl_doc"] = _d["vl_serv_cab"] or _d["vl_doc_cab"]
            _d["vl_bc_icms"] = _d["vl_bc_cab"]
            _d["vl_icms"] = _d["vl_icms_cab"]
        # Valor contábil do frete — a base do crédito de PIS/COFINS sai daqui
        # (contábil menos o ICMS destacado). É o VL_OPR do analítico; sem D190,
        # o VL_DOC do cabeçalho, que é o total do documento. Campo separado de
        # `vl_doc` de propósito: ali o fallback prefere VL_SERV (prestação), que
        # não inclui as demais parcelas do documento.
        _d["vl_contabil"] = _d["vl_opr_d190"] if _d["tem_d190"] else (
            _d["vl_doc_cab"] or _d["vl_serv_cab"])

    return {
        "cnpj": company_cnpj,
        "name": company_name,
        "periodo_inicio": periodo_inicio,
        "periodo_fim": periodo_fim,
        "c100": c100_entries,
        "d100": d100_entries,
        "bloco_d_tem_pis_cofins": bloco_d_tem_pis_cofins,
        "products": prod_descriptions,
        "participants": participants,
        "duplicate_nfs": duplicate_nfs
    }
