import xml.etree.ElementTree as ET

def parse_nfe_xml(path):
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"ns": "http://www.portalfiscal.inf.br/nfe"}
        
        # Emitter
        emit = root.find(".//ns:emit", ns)
        emit_cnpj = emit.find("ns:CNPJ", ns).text if emit is not None and emit.find("ns:CNPJ", ns) is not None else ""
        emit_cpf = emit.find("ns:CPF", ns).text if emit is not None and emit.find("ns:CPF", ns) is not None else ""
        emit_name = emit.find("ns:xNome", ns).text if emit is not None else ""
        emit_id = emit_cnpj if emit_cnpj else emit_cpf
        ender_emit = emit.find("ns:enderEmit", ns) if emit is not None else None
        emit_uf = ender_emit.find("ns:UF", ns).text if ender_emit is not None and ender_emit.find("ns:UF", ns) is not None else ""

        # Destinatário: a UF de destino é metade da chave da regra de
        # antecipação de ICMS (uf_origem + uf_destino + CFOP).
        dest = root.find(".//ns:dest", ns)
        ender_dest = dest.find("ns:enderDest", ns) if dest is not None else None
        # O RESTO dos dados do destinatário entrou em 10/09/2026 para a geração
        # de SPED a partir dos XMLs: numa nota de ENTRADA o destinatário é a
        # própria empresa, e é dele que saem a inscrição estadual e o código
        # IBGE do município — dois campos obrigatórios do registro 0000 que o
        # cadastro do sistema não tem.
        def _t(pai, tag, padrao=""):
            if pai is None:
                return padrao
            el = pai.find("ns:" + tag, ns)
            return el.text if el is not None and el.text else padrao

        dest_uf = _t(ender_dest, "UF")
        dest_cnpj = _t(dest, "CNPJ") or _t(dest, "CPF")

        # Ide
        ide = root.find(".//ns:ide", ns)
        num = ide.find("ns:nNF", ns).text if ide is not None else ""
        serie = ide.find("ns:serie", ns).text if ide is not None else ""
        # Modelo, chave e datas: o C100 precisa dos quatro, e só o XML os tem.
        modelo = _t(ide, "mod", "55")
        dh_emi = _t(ide, "dhEmi") or _t(ide, "dEmi")
        dh_saida = _t(ide, "dhSaiEnt") or _t(ide, "dSaiEnt")
        _inf_nfe = root.find(".//ns:infNFe", ns)
        chave = ((_inf_nfe.attrib.get("Id") or "").replace("NFe", "").strip()
                 if _inf_nfe is not None else "")
        
        # Total value
        total = root.find(".//ns:total/ns:ICMSTot", ns)
        v_nf = float(total.find("ns:vNF", ns).text) if total is not None and total.find("ns:vNF", ns) is not None else 0.0
        v_st_total = float(total.find("ns:vST", ns).text) if total is not None and total.find("ns:vST", ns) is not None else 0.0

        def _tot(tag):
            """Total do grupo ICMSTot, em float. Ausente é zero.

            O C100 tem um campo para cada um desses totais, e derivá-los da soma
            dos itens erra por centavos de arredondamento — o XML já traz a
            conta fechada pelo emitente.
            """
            if total is None:
                return 0.0
            el = total.find("ns:" + tag, ns)
            try:
                return float(el.text) if el is not None and el.text else 0.0
            except (TypeError, ValueError):
                return 0.0
        
        # Items
        items = []
        for det in root.findall(".//ns:det", ns):
            n_item = det.attrib.get("nItem")
            prod = det.find("ns:prod", ns)
            c_prod = prod.find("ns:cProd", ns).text if prod is not None else ""
            x_prod = prod.find("ns:xProd", ns).text if prod is not None else ""
            
            # Product details
            v_prod_el = prod.find("ns:vProd", ns) if prod is not None else None
            v_prod = float(v_prod_el.text) if v_prod_el is not None and v_prod_el.text else 0.0

            # Valor unitário declarado pelo emitente — usado para agrupar itens
            # idênticos (mesmo código + descrição + valor unitário) dentro da
            # mesma nota, na comparação de crédito de ICMS.
            # Unidade comercial: campo 06 do C170 e o registro 0190 inteiro.
            u_com_el = prod.find("ns:uCom", ns) if prod is not None else None
            u_com = (u_com_el.text or "").strip() if u_com_el is not None else ""
            v_un_com_el = prod.find("ns:vUnCom", ns) if prod is not None else None
            v_un_com = float(v_un_com_el.text) if v_un_com_el is not None and v_un_com_el.text else 0.0

            # Quantidade comercial: junto do valor unitário, é o par que permite
            # casar a linha do SPED com a do XML quando o código do produto é
            # diferente nas duas fontes (ver casar_itens_da_nota).
            q_com_el = prod.find("ns:qCom", ns) if prod is not None else None
            q_com = float(q_com_el.text) if q_com_el is not None and q_com_el.text else 0.0

            v_frete_el = prod.find("ns:vFrete", ns) if prod is not None else None
            v_frete = float(v_frete_el.text) if v_frete_el is not None and v_frete_el.text else 0.0
            
            v_seg_el = prod.find("ns:vSeg", ns) if prod is not None else None
            v_seg = float(v_seg_el.text) if v_seg_el is not None and v_seg_el.text else 0.0
            
            v_outro_el = prod.find("ns:vOutro", ns) if prod is not None else None
            v_outro = float(v_outro_el.text) if v_outro_el is not None and v_outro_el.text else 0.0
            
            v_desc_el = prod.find("ns:vDesc", ns) if prod is not None else None
            v_desc = float(v_desc_el.text) if v_desc_el is not None and v_desc_el.text else 0.0
            
            cfop = prod.find("ns:CFOP", ns).text if prod is not None else ""
            ncm = prod.find("ns:NCM", ns).text if prod is not None and prod.find("ns:NCM", ns) is not None else ""
            cest = prod.find("ns:CEST", ns).text if prod is not None and prod.find("ns:CEST", ns) is not None else ""
            c_benef = prod.find("ns:cBenef", ns).text if prod is not None and prod.find("ns:cBenef", ns) is not None else ""
            
            # ST/FCP/IPI
            v_fcp_st_el = det.find(".//ns:vFCPST", ns)
            fcp_st_val = float(v_fcp_st_el.text) if v_fcp_st_el is not None and v_fcp_st_el.text else 0.0
            
            v_ipi_el = det.find(".//ns:vIPI", ns)
            v_ipi = float(v_ipi_el.text) if v_ipi_el is not None and v_ipi_el.text else 0.0
            
            v_ipi_devol_el = det.find(".//ns:vIPIDevol", ns)
            v_ipi_devol = float(v_ipi_devol_el.text) if v_ipi_devol_el is not None and v_ipi_devol_el.text else 0.0
            
            cst_ipi = ""
            vl_bc_ipi = 0.0
            ipi_el = det.find(".//ns:IPI", ns)
            if ipi_el is not None:
                for child in ipi_el:
                    cst_el = child.find("ns:CST", ns)
                    if cst_el is not None and cst_el.text:
                        cst_ipi = cst_el.text
                    v_bc_el = child.find("ns:vBC", ns)
                    if v_bc_el is not None and v_bc_el.text:
                        vl_bc_ipi = float(v_bc_el.text)

            cst_pis = ""
            vl_bc_pis = 0.0
            vl_pis = 0.0
            p_pis = 0.0
            pis_el = det.find(".//ns:PIS", ns)
            if pis_el is not None:
                for child in pis_el:
                    cst_el = child.find("ns:CST", ns)
                    if cst_el is not None and cst_el.text:
                        cst_pis = cst_el.text
                    v_bc_el = child.find("ns:vBC", ns)
                    if v_bc_el is not None and v_bc_el.text:
                        vl_bc_pis = float(v_bc_el.text)
                    v_pis_el = child.find("ns:vPIS", ns)
                    if v_pis_el is not None and v_pis_el.text:
                        vl_pis += float(v_pis_el.text)
                    # A alíquota do próprio documento. O crédito esperado é a
                    # base recalculada pela tese do STF VEZES esta alíquota —
                    # o SPED Fiscal não traz base de PIS/COFINS, só o valor.
                    p_pis_el = child.find("ns:pPIS", ns)
                    if p_pis_el is not None and p_pis_el.text:
                        p_pis = float(p_pis_el.text)

            cst_cofins = ""
            vl_bc_cofins = 0.0
            vl_cofins = 0.0
            p_cofins = 0.0
            cofins_el = det.find(".//ns:COFINS", ns)
            if cofins_el is not None:
                for child in cofins_el:
                    cst_el = child.find("ns:CST", ns)
                    if cst_el is not None and cst_el.text:
                        cst_cofins = cst_el.text
                    v_bc_el = child.find("ns:vBC", ns)
                    if v_bc_el is not None and v_bc_el.text:
                        vl_bc_cofins = float(v_bc_el.text)
                    v_cofins_el = child.find("ns:vCOFINS", ns)
                    if v_cofins_el is not None and v_cofins_el.text:
                        vl_cofins += float(v_cofins_el.text)
                    p_cofins_el = child.find("ns:pCOFINS", ns)
                    if p_cofins_el is not None and p_cofins_el.text:
                        p_cofins = float(p_cofins_el.text)
            
            imposto = det.find("ns:imposto", ns)
            icms_val = 0.0
            icms_normal_val = 0.0
            icms_sn_val = 0.0
            icms_st_val = 0.0
            v_bc = 0.0
            p_icms = 0.0
            cst_icms = ""
            orig_icms = "0"

            # ICMS Monofásico de combustíveis (NT 2023.001). Cada CST usa uma
            # tag própria; são lidas SEMPRE, mesmo quando a auditoria não vai
            # somá-las, porque a aba "ICMS Mono" do relatório mostra as quatro
            # separadas para conferência.
            #   vICMSMono      CST 02/15/53  monofásico próprio da operação
            #   vICMSMonoReten CST 15        retido para a etapa seguinte
            #   vICMSMonoRet   CST 61        cobrado anteriormente
            #   vICMSMonoDif   CST 53        diferido
            icms_mono_val = 0.0
            icms_mono_reten_val = 0.0
            icms_mono_ret_val = 0.0
            icms_mono_dif_val = 0.0
            q_bc_mono = 0.0
            ad_rem_icms = 0.0

            if imposto is not None:
                icms = imposto.find("ns:ICMS", ns)
                if icms is not None:
                    for child in icms:
                        cst_el = child.find("ns:CST", ns)
                        csosn_el = child.find("ns:CSOSN", ns)
                        cst_icms = cst_el.text if cst_el is not None else (csosn_el.text if csosn_el is not None else "")
                        # A ORIGEM da mercadoria (0 a 8) é campo separado no XML
                        # e vem colada ao CST no SPED: o C170 campo 10 tem três
                        # dígitos ("000", "160"). Sem ela, o SPED gerado diria
                        # que toda mercadoria é nacional.
                        orig_el = child.find("ns:orig", ns)
                        if orig_el is not None and orig_el.text:
                            orig_icms = orig_el.text.strip()
                        
                        v_bc_el = child.find("ns:vBC", ns)
                        if v_bc_el is not None and v_bc_el.text:
                            v_bc = float(v_bc_el.text)
                            
                        p_icms_el = child.find("ns:pICMS", ns)
                        if p_icms_el is not None and p_icms_el.text:
                            p_icms = float(p_icms_el.text)
                            
                        v_icms_el = child.find("ns:vICMS", ns)
                        if v_icms_el is not None and v_icms_el.text:
                            icms_normal_val += float(v_icms_el.text)
                            icms_val += float(v_icms_el.text)
                        
                        v_cred_el = child.find("ns:vCredICMSSN", ns)
                        if v_cred_el is not None and v_cred_el.text:
                            icms_sn_val += float(v_cred_el.text)
                            icms_val += float(v_cred_el.text)
                            
                        v_icmssn_el = child.find("ns:vICMSSN", ns)
                        if v_icmssn_el is not None and v_icmssn_el.text:
                            icms_sn_val += float(v_icmssn_el.text)
                            icms_val += float(v_icmssn_el.text)
                            
                        # --- ICMS Monofásico (combustíveis) ---
                        # Tags exatas: vICMSMonoRet e vICMSMonoReten são
                        # DIFERENTES e o find() casa nome inteiro, sem prefixo.
                        for tag, acumular in (
                            ("vICMSMono", "proprio"),
                            ("vICMSMonoReten", "reten"),
                            ("vICMSMonoRet", "ret"),
                            ("vICMSMonoDif", "dif"),
                        ):
                            el = child.find(f"ns:{tag}", ns)
                            if el is None or not el.text:
                                continue
                            valor = float(el.text)
                            if acumular == "proprio":
                                icms_mono_val += valor
                            elif acumular == "reten":
                                icms_mono_reten_val += valor
                            elif acumular == "ret":
                                icms_mono_ret_val += valor
                            else:
                                icms_mono_dif_val += valor

                        q_bc_mono_el = child.find("ns:qBCMono", ns)
                        if q_bc_mono_el is None:
                            q_bc_mono_el = child.find("ns:qBCMonoRet", ns)
                        if q_bc_mono_el is not None and q_bc_mono_el.text:
                            q_bc_mono = float(q_bc_mono_el.text)

                        ad_rem_el = child.find("ns:adRemICMS", ns)
                        if ad_rem_el is None:
                            ad_rem_el = child.find("ns:adRemICMSRet", ns)
                        if ad_rem_el is not None and ad_rem_el.text:
                            ad_rem_icms = float(ad_rem_el.text)

                        v_icms_st_el = child.find("ns:vICMSST", ns)
                        if v_icms_st_el is not None and v_icms_st_el.text:
                            icms_st_val = float(v_icms_st_el.text)
                            
            vl_item_calculado = v_prod + v_frete + v_seg + v_outro + icms_st_val + fcp_st_val + v_ipi + v_ipi_devol - v_desc
            
            # --- Tese do STF: Cálculo EXATO da Base de PIS/COFINS pelo XML ---
            # Valor Bruto = (vProd + vFrete + vSeg + vOutro + vIPI + vIPIDevol + vICMSST + vFCPST) - vDesc
            vl_bruto_item = (v_prod + v_frete + v_seg + v_outro + v_ipi + v_ipi_devol + icms_st_val + fcp_st_val) - v_desc
            
            # Base PIS/COFINS XML = Valor Bruto - (vICMS normal + vICMSSN) - vICMSST - vFCPST - vIPI - vIPIDevol
            base_pis_cofins_calculada = vl_bruto_item - icms_val - icms_st_val - fcp_st_val - v_ipi - v_ipi_devol
            
            # Trata nós inexistentes para 0.0 com segurança (não permitindo base negativa)
            vl_bc_pis = round(max(0.0, base_pis_cofins_calculada), 2)
            vl_bc_cofins = round(max(0.0, base_pis_cofins_calculada), 2)
            
            items.append({
                "num_item": n_item,
                "cod_item": c_prod,
                "descr": x_prod,
                "cfop": cfop,
                "total_item_difal": vl_item_calculado,
                "cst": cst_icms,
                "orig": orig_icms,
                "ncm": ncm,
                "cest": cest,
                "c_benef": c_benef,
                "vl_item": vl_item_calculado,
                "vl_unit": v_un_com,
                "qtd": q_com,
                "vl_prod_net": v_prod - v_desc,
                # Para a geração de SPED: o C170 escritura valor e desconto
                # SEPARADOS (campos 07 e 08), e as acessórias do item entram na
                # conta do documento.
                "unid": u_com,
                "vl_prod": v_prod,
                "vl_desc": v_desc,
                "vl_frete": v_frete,
                "vl_seg": v_seg,
                "vl_outro": v_outro,
                "vl_bc": v_bc,
                "p_icms": p_icms,
                "vl_icms": icms_val,
                "vl_icms_normal": icms_normal_val,
                "vl_icms_sn": icms_sn_val,
                # Soma das quatro tags monofásicas. Fica FORA de `vl_icms`: só
                # entra no crédito quando a auditoria é disparada com a opção
                # "Considerar ICMS Mono" marcada (ver cross_checker).
                "vl_icms_mono": round(
                    icms_mono_val + icms_mono_reten_val + icms_mono_ret_val + icms_mono_dif_val, 2),
                "vl_icms_mono_proprio": icms_mono_val,
                "vl_icms_mono_reten": icms_mono_reten_val,
                "vl_icms_mono_ret": icms_mono_ret_val,
                "vl_icms_mono_dif": icms_mono_dif_val,
                "q_bc_mono": q_bc_mono,
                "ad_rem_icms": ad_rem_icms,
                "vl_icms_st": icms_st_val,
                "fcp_st": fcp_st_val,
                "v_ipi": v_ipi,
                "v_ipi_devol": v_ipi_devol,
                "cst_ipi": cst_ipi,
                "vl_bc_ipi": vl_bc_ipi,
                "cst_pis": cst_pis,
                "vl_bc_pis": vl_bc_pis,
                "vl_pis": vl_pis,
                "p_pis": p_pis,
                "cst_cofins": cst_cofins,
                "vl_bc_cofins": vl_bc_cofins,
                "vl_cofins": vl_cofins,
                "p_cofins": p_cofins
            })
            
        return {
            "num": num,
            "serie": serie,
            "modelo": modelo,
            "chave": chave,
            "dh_emi": dh_emi,
            "dh_saida": dh_saida,
            "emit_id": emit_id,
            "emit_name": emit_name,
            "emit_uf": emit_uf,
            "emit_ie": _t(emit, "IE"),
            "emit_cod_mun": _t(ender_emit, "cMun"),
            "dest_uf": dest_uf,
            "dest_id": dest_cnpj,
            "dest_name": _t(dest, "xNome"),
            "dest_ie": _t(dest, "IE"),
            "dest_cod_mun": _t(ender_dest, "cMun"),
            "dest_mun": _t(ender_dest, "xMun"),
            "vl_doc": v_nf,
            "vl_st": v_st_total,
            "vl_merc": _tot("vProd"),
            "vl_desc_total": _tot("vDesc"),
            "vl_frete_total": _tot("vFrete"),
            "vl_seg_total": _tot("vSeg"),
            "vl_outro_total": _tot("vOutro"),
            "vl_bc_icms_total": _tot("vBC"),
            "vl_icms_total": _tot("vICMS"),
            "vl_bc_st_total": _tot("vBCST"),
            "vl_ipi_total": _tot("vIPI"),
            "vl_pis_total": _tot("vPIS"),
            "vl_cofins_total": _tot("vCOFINS"),
            "items": items
        }
    except Exception as e:
        return {"error": str(e)}

def parse_cte_xml(path):
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"ns": "http://www.portalfiscal.inf.br/cte"}
        
        ide = root.find(".//ns:ide", ns)

        def _txt(pai, tag, padrao=""):
            """Texto da tag, ou o padrão. Guarda o pai E o filho.

            `ide.find("ns:serie").text` estourava AttributeError quando a tag
            faltava, e o erro virava {"error": ...}: lá no cross_checker isso
            é um `continue` mudo e o CT-e inteiro sai da auditoria sem
            aparecer em lugar nenhum. Uma tag ausente não pode custar o
            documento todo.
            """
            if pai is None:
                return padrao
            el = pai.find(tag, ns)
            return el.text if el is not None and el.text is not None else padrao

        num = _txt(ide, "ns:nCT")
        serie = _txt(ide, "ns:serie")
        # Data e chave: o D100 precisa das duas para escriturar o frete, e elas
        # entraram junto com a geração de SPED a partir dos XMLs (10/09/2026).
        dh_emi_cte = _txt(ide, "ns:dhEmi") or _txt(ide, "ns:dEmi")
        _inf_cte = root.find(".//ns:infCte", ns)
        chave_cte = ((_inf_cte.attrib.get("Id") or "").replace("CTe", "").strip()
                     if _inf_cte is not None else "")
        cfop_el = ide.find("ns:CFOP", ns) if ide is not None else None
        cfop = cfop_el.text if cfop_el is not None else ""
        
        emit = root.find(".//ns:emit", ns)
        emit_cnpj = emit.find("ns:CNPJ", ns).text if emit is not None and emit.find("ns:CNPJ", ns) is not None else ""
        emit_name = emit.find("ns:xNome", ns).text if emit is not None else ""
        ender_emit = emit.find("ns:enderEmit", ns) if emit is not None else None
        emit_uf = ender_emit.find("ns:UF", ns).text if ender_emit is not None and ender_emit.find("ns:UF", ns) is not None else ""
        
        v_prest = root.find(".//ns:vPrest", ns)
        v_t_prest = 0.0
        if v_prest is not None:
            v_t_prest_el = v_prest.find("ns:vTPrest", ns)
            if v_t_prest_el is None:
                v_t_prest_el = v_prest.find("ns:vTprest", ns)
            v_t_prest = float(v_t_prest_el.text) if v_t_prest_el is not None and v_t_prest_el.text else 0.0
        
        imposto = root.find(".//ns:imp", ns)
        icms_proprio = 0.0
        icms_outra_uf = 0.0
        v_bc = 0.0
        if imposto is not None:
            icms = imposto.find("ns:ICMS", ns)
            if icms is not None:
                for child in icms:
                    v_icms_el = child.find("ns:vICMS", ns)
                    if v_icms_el is not None and v_icms_el.text:
                        icms_proprio = float(v_icms_el.text)
                    # Grupo ICMSOutraUF: o CT-e com ICMS devido a outra UF traz o
                    # valor em vICMSOutraUF, tag DIFERENTE de vICMS. Lendo só
                    # vICMS, esse frete chegava com ICMS zero e a auditoria
                    # comparava crédito nenhum. É crédito aproveitável —
                    # decisão do escritório em 27/08/2026.
                    v_outra_uf_el = child.find("ns:vICMSOutraUF", ns)
                    if v_outra_uf_el is not None and v_outra_uf_el.text:
                        icms_outra_uf = float(v_outra_uf_el.text)
                    v_bc_el = child.find("ns:vBC", ns)
                    if v_bc_el is None:
                        v_bc_el = child.find("ns:vBCOutraUF", ns)
                    if v_bc_el is not None and v_bc_el.text:
                        v_bc = float(v_bc_el.text)

        # `vl_icms` é o crédito total do frete: as duas parcelas somam. As
        # colunas separadas ficam no retorno para permitir conferir de onde
        # veio cada real sem abrir o XML.
        icms_val = round(icms_proprio + icms_outra_uf, 2)
                        
        rem_name_el = root.find(".//ns:rem/ns:xNome", ns)
        rem_name = rem_name_el.text if rem_name_el is not None else ""
        
        dest_name_el = root.find(".//ns:dest/ns:xNome", ns)
        dest_name = dest_name_el.text if dest_name_el is not None else ""
        
        exped_name_el = root.find(".//ns:exped/ns:xNome", ns)
        exped_name = exped_name_el.text if exped_name_el is not None else ""
        
        receb_name_el = root.find(".//ns:receb/ns:xNome", ns)
        receb_name = receb_name_el.text if receb_name_el is not None else ""
        
        toma_cnpj = ""
        toma_cpf = ""
        toma_role = ""
        toma_name = ""
        
        toma3 = ide.find("ns:toma3", ns) if ide is not None else None
        toma4 = ide.find("ns:toma4", ns) if ide is not None else None
        
        if toma4 is not None:
            toma_role = "outros"
            t_name_el = toma4.find("ns:xNome", ns)
            toma_name = t_name_el.text if t_name_el is not None else ""
            cnpj_el = toma4.find("ns:CNPJ", ns)
            toma_cnpj = cnpj_el.text if cnpj_el is not None else ""
            cpf_el = toma4.find("ns:CPF", ns)
            toma_cpf = cpf_el.text if cpf_el is not None else ""
        elif toma3 is not None:
            toma_val = toma3.find("ns:toma", ns).text if toma3.find("ns:toma", ns) is not None else ""
            role_map = {"0": "rem", "1": "exped", "2": "receb", "3": "dest"}
            toma_role = role_map.get(toma_val, "")
            
            if toma_role == "rem":
                toma_name = rem_name
            elif toma_role == "dest":
                toma_name = dest_name
            elif toma_role == "exped":
                toma_name = exped_name
            elif toma_role == "receb":
                toma_name = receb_name
                
            if toma_role:
                role_el = root.find(f".//ns:{toma_role}", ns)
                if role_el is not None:
                    cnpj_el = role_el.find("ns:CNPJ", ns)
                    toma_cnpj = cnpj_el.text if cnpj_el is not None else ""
                    cpf_el = role_el.find("ns:CPF", ns)
                    toma_cpf = cpf_el.text if cpf_el is not None else ""
        
        toma_id = toma_cnpj if toma_cnpj else toma_cpf
        
        nfe_keys = []
        nfe_nums = []
        inf_nfes = root.findall(".//ns:infNFe", ns)
        for inf_nfe in inf_nfes:
            chave_el = inf_nfe.find("ns:chave", ns)
            if chave_el is not None and chave_el.text:
                k = chave_el.text.strip()
                nfe_keys.append(k)
                if len(k) == 44:
                    nfe_nums.append(str(int(k[25:34])))
                else:
                    nfe_nums.append("N/A")
        
        return {
            "num": num,
            "serie": serie,
            "data": dh_emi_cte,
            "chave": chave_cte,
            "emit_id": emit_cnpj,
            "emit_name": emit_name,
            "emit_uf": emit_uf,
            "cfop": cfop,
            "vl_doc": v_t_prest,
            "vl_bc": v_bc,
            "vl_icms": icms_val,
            "vl_icms_proprio": icms_proprio,
            "vl_icms_outra_uf": icms_outra_uf,
            "tomador_id": toma_id,
            "tomador_name": toma_name,
            "tomador_role": toma_role,
            "rem_name": rem_name,
            "dest_name": dest_name,
            "nfe_keys": nfe_keys,
            "nfe_nums": nfe_nums
        }
    except Exception as e:
        return {"error": str(e)}
