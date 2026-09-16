import os
import re
import logging
from ...domain.models.rules import CestMvaRule
from ...infrastructure.database.session import SessionLocal

logger = logging.getLogger(__name__)

_CEST_MVA_CACHE = {}
_IS_LOADED = False

def clean_cest(val):
    if not val:
        return ""
    return re.sub(r"\D", "", str(val))

def clean_float_mva(val):
    if val is None or val == "" or val == "-":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).strip().replace("%", "").replace(",", ".")
    try:
        f = float(val_str)
        if f > 15.0:
            return f / 100.0
        return f
    except ValueError:
        return 0.0

def load_cest_mva_rules(force_reload=False):
    global _CEST_MVA_CACHE, _IS_LOADED
    if _IS_LOADED and not force_reload and _CEST_MVA_CACHE:
        return _CEST_MVA_CACHE
        
    db = SessionLocal()
    try:
        rules_in_db = db.query(CestMvaRule).all()
        if not rules_in_db:
            excel_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../Portaria 16.xlsx"))
            if not os.path.exists(excel_path):
                excel_path = os.path.abspath("Portaria 16.xlsx")
                
            if os.path.exists(excel_path):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(excel_path, data_only=True)
                    sheet = wb.active
                    count = 0
                    seen_cests = set()
                    for row in sheet.iter_rows(min_row=2, values_only=True):
                        cest_raw = str(row[0] or "").strip()
                        if not cest_raw or len(clean_cest(cest_raw)) < 4:
                            continue
                        if cest_raw in seen_cests:
                            continue
                        seen_cests.add(cest_raw)
                        ncm_raw = str(row[1] or "").strip()
                        mva_orig = clean_float_mva(row[2])
                        mva_4 = clean_float_mva(row[3])
                        mva_7 = clean_float_mva(row[4])
                        mva_12 = clean_float_mva(row[5])
                        linha_item = str(row[6] or "").strip()
                        descr = str(row[7] or "").strip()
                        
                        rule_obj = CestMvaRule(
                            cest=cest_raw,
                            tenant_id="default",
                            ncm=ncm_raw,
                            description=descr,
                            linha_item=linha_item,
                            mva_original=mva_orig,
                            mva_aj_4=mva_4,
                            mva_aj_7=mva_7,
                            mva_aj_12=mva_12,
                            status="aprovado",
                            is_global=True,
                            created_by="system_seeder"
                        )
                        db.merge(rule_obj)
                        count += 1
                    db.commit()
                    logger.info(f"[CEST_MVA] Semeados {count} registros de Portaria 16.xlsx no banco.")
                    rules_in_db = db.query(CestMvaRule).all()
                except Exception as e:
                    logger.error(f"[CEST_MVA] Erro ao semear Portaria 16.xlsx: {e}")
                    db.rollback()
                    
        new_cache = {}
        for r in rules_in_db:
            rule_dict = {
                "cest": r.cest,
                "ncm": r.ncm,
                "description": r.description,
                "linha_item": r.linha_item,
                "mva_original": r.mva_original,
                "mva_aj_4": r.mva_aj_4,
                "mva_aj_7": r.mva_aj_7,
                "mva_aj_12": r.mva_aj_12
            }
            new_cache[r.cest] = rule_dict
            new_cache[clean_cest(r.cest)] = rule_dict
            
        _CEST_MVA_CACHE = new_cache
        _IS_LOADED = True
        return _CEST_MVA_CACHE
    finally:
        db.close()

def get_cest_mva_rule(cest: str) -> dict | None:
    cache = load_cest_mva_rules()
    if not cest:
        return None
    res = cache.get(str(cest).strip())
    if res:
        return res
    return cache.get(clean_cest(cest))

def get_mva_for_aliquota(rule: dict, aliquota_icms: float) -> tuple[float, str]:
    if not rule:
        return 0.0, "MVA Não Encontrada"
    aliq = float(aliquota_icms or 0.0)
    if aliq <= 4.5:
        return rule["mva_aj_4"], "MVA Aj. 4%"
    elif aliq <= 7.5:
        return rule["mva_aj_7"], "MVA Aj. 7%"
    elif aliq <= 13.0:
        return rule["mva_aj_12"], "MVA Aj. 12%"
    else:
        return rule["mva_original"], "MVA Original"
