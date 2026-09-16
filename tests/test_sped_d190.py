"""Frete e ICMS do CT-e vêm do registro analítico D190.

Bug corrigido: o parser lia `vl_icms` do campo 22 do D100, que é COD_INF (um
código, quase sempre vazio). Resultado: o ICMS de todo CT-e chegava como 0,00 e
a conferência de crédito de ICMS sobre frete nunca funcionou — glosa indevida
não era detectada, e onde havia crédito legítimo o sistema acusava divergência
do valor cheio.

Agora: soma-se o D190 (VL_OPR e VL_ICMS), que é a fonte oficial por CFOP.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.audit_engine.sped_parser import parse_sped

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))


def montar_sped(linhas):
    fd, caminho = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    cab = "|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|11111111111111|MG|||\n"
    with open(caminho, "w", encoding="latin1") as f:
        f.write(cab)
        for l in linhas:
            f.write(l + "\n")
    return caminho


# Layout D100: |REG|IND_OPER|IND_EMIT|COD_PART|COD_MOD|COD_SIT|SER|SUB|NUM_DOC|CHV|
#              DT_DOC|DT_A_P|TP_CT|CHV_REF|VL_DOC|VL_DESC|IND_FRT|VL_SERV|VL_BC|VL_ICMS|VL_NT|COD_INF|COD_CTA|
# Layout D190: |REG|CST_ICMS|CFOP|ALIQ|VL_OPR|VL_BC_ICMS|VL_ICMS|VL_RED_BC|COD_OBS|

CHV_A = "3" * 44
CHV_B = "4" * 44
CHV_C = "5" * 44

sped = montar_sped([
    # A) um único D190
    f"|D100|0|1|1091|57|00|000||100|{CHV_A}|24072026|29072026|0||198,87|0|1|198,87|198,87|23,86|||9999|",
    "|D190|000|2353|11,99|198,87|198,87|23,86|0,00||",

    # B) dois D190 (CFOPs distintos) — devem SOMAR
    f"|D100|0|1|795|57|00|002||200|{CHV_B}|28072026|30072026|0||1000,00|0|1|1000,00|1000,00|120,00|||9999|",
    "|D190|000|2353|12,00|600,00|600,00|72,00|0,00||",
    "|D190|000|1353|12,00|400,00|400,00|48,00|0,00||",

    # C) sem D190 — cai para o cabeçalho
    f"|D100|0|1|500|57|00|001||300|{CHV_C}|29072026|31072026|0||55,50|0|1|55,50|55,50|6,66|||9999|",
])

d = parse_sped(sped)
d100 = d["d100"]

print("\n=== 1. CT-e com um D190 ===")
a = d100[CHV_A]
check("veio do D190", a["tem_d190"] is True)
check("frete = VL_OPR do D190", abs(a["vl_doc"] - 198.87) < 0.001, f"={a['vl_doc']}")
check("ICMS = VL_ICMS do D190", abs(a["vl_icms"] - 23.86) < 0.001, f"={a['vl_icms']}")
check("base de calculo lida", abs(a["vl_bc_icms"] - 198.87) < 0.001, f"={a['vl_bc_icms']}")
check("CFOP capturado", a["cfops"] == ["2353"], f"={a['cfops']}")

print("\n=== 2. CT-e com dois D190: soma, nao sobrescreve ===")
b = d100[CHV_B]
check("frete somado (600 + 400)", abs(b["vl_doc"] - 1000.00) < 0.001, f"={b['vl_doc']}")
check("ICMS somado (72 + 48)", abs(b["vl_icms"] - 120.00) < 0.001, f"={b['vl_icms']}")
check("os dois CFOPs capturados", sorted(b["cfops"]) == ["1353", "2353"], f"={b['cfops']}")

print("\n=== 3. CT-e sem D190: usa o cabecalho ===")
c = d100[CHV_C]
check("marcado como sem analitico", c["tem_d190"] is False)
check("frete do cabecalho (VL_SERV)", abs(c["vl_doc"] - 55.50) < 0.001, f"={c['vl_doc']}")
check("ICMS do cabecalho (campo 20)", abs(c["vl_icms"] - 6.66) < 0.001, f"={c['vl_icms']}")

print("\n=== 4. O bug antigo nao volta ===")
check("ICMS NAO e mais lido de COD_INF (campo 22)",
      abs(a["vl_icms"] - 23.86) < 0.001 and a["vl_icms"] != 0.0,
      "campo 22 e um codigo; lia-lo zerava o ICMS de todo CT-e")
check("nenhum CT-e com ICMS zerado indevidamente",
      all(x["vl_icms"] > 0 for x in d100.values()),
      {k[-6:]: v["vl_icms"] for k, v in d100.items()})

print("\n=== 5. Rastreabilidade das duas origens ===")
check("valores do D190 preservados a parte", abs(b["vl_icms_d190"] - 120.00) < 0.001)
check("valores do cabecalho preservados a parte", abs(b["vl_icms_cab"] - 120.00) < 0.001)

os.remove(sped)

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
