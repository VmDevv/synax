import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    html = f.read()

# I will fix all panel IDs manually based on the exact template structure.
# Instead of guessing the string replacement, let's use regex based on the comment above the panel.

mappings = [
    (r'<!-- Aba 1: XMLs Faltantes -->\s+<div id="panel-[^"]+"', '<!-- Aba 1: XMLs Faltantes -->\n            \n            <div id="panel-tab-missing-xmls"'),
    (r'<!-- Aba 3: Novos CFOPs -->\s+<div id="panel-[^"]+"', '<!-- Aba 3: Novos CFOPs -->\n            \n            <div id="panel-tab-new-cfops"'),
    (r'<!-- Aba 4: Divergências de Valor -->\s+<div id="panel-[^"]+"', '<!-- Aba 4: Divergências de Valor -->\n            \n            <div id="panel-tab-divergences"'),
    (r'<!-- Aba 5: Divergências de CFOP -->\s+<div id="panel-[^"]+"', '<!-- Aba 5: Divergências de CFOP -->\n            \n            <div id="panel-tab-cfop-issues"'),
    (r'<!-- Aba 6: Regras ICMS Pendentes -->\s+<div id="panel-[^"]+"', '<!-- Aba 6: Regras ICMS Pendentes -->\n            \n            <div id="panel-tab-icms-pending"'),
    (r'<!-- Aba 7: Inconformidades ICMS -->\s+<div id="panel-[^"]+"', '<!-- Aba 7: Inconformidades ICMS -->\n            \n            <div id="panel-tab-icms-issues"'),
    (r'<!-- Aba 8: Inconformidades CT-e Tomador -->\s+<div id="panel-[^"]+"', '<!-- Aba 8: Inconformidades CT-e Tomador -->\n            \n            <div id="panel-tab-cte-issues"'),
    (r'<!-- Aba 9: ICMS ST e Notas -->\s+<div id="panel-[^"]+"', '<!-- Aba 9: ICMS ST e Notas -->\n            \n            <div id="panel-tab-icms-st"'),
    (r'<!-- Aba 10: DIFAL Interno -->\s+<div id="panel-[^"]+"', '<!-- Aba 10: DIFAL Interno -->\n            \n            <div id="panel-tab-difal-int"'),
    (r'<!-- Aba 11: DIFAL Interestadual -->\s+<div id="panel-[^"]+"', '<!-- Aba 11: DIFAL Interestadual -->\n            \n            <div id="panel-tab-difal-ext"'),
    (r'<!-- Aba 12: Divergência de CFOP \(CT-e\) -->\s+<div id="panel-[^"]+"', '<!-- Aba 12: Divergência de CFOP (CT-e) -->\n            \n            <div id="panel-tab-cte-cfop-disc"'),
    (r'<!-- Aba 13: Relatório Geral \(CT-e\) -->\s+<div id="panel-[^"]+"', '<!-- Aba 13: Relatório Geral (CT-e) -->\n            \n            <div id="panel-tab-cte-gen-rep"'),
    (r'<!-- Aba 14: Conferência de IPI -->\s+<div id="panel-[^"]+"', '<!-- Aba 14: Conferência de IPI -->\n            \n            <div id="panel-tab-ipi-issues"'),
    (r'<!-- Aba: Regras IPI Pendentes -->\s+<div id="panel-[^"]+"', '<!-- Aba: Regras IPI Pendentes -->\n            <div id="panel-tab-ipi-pending"'),
    (r'<!-- Aba 14: Conferência PIS/COFINS -->\s+<div id="panel-[^"]+"', '<!-- Aba 14: Conferência PIS/COFINS -->\n            \n            <div id="panel-tab-pis-cofins-issues"'),
    (r'<!-- Aba 15: Cálculo de Antecipação -->\s+<div id="panel-[^"]+"', '<!-- Aba 15: Cálculo de Antecipação -->\n            \n            <div id="panel-tab-anticipation"'),
    (r'<!-- Aba 16: Notas Não Lançadas \(Malha Fina\) -->\s+<div id="panel-[^"]+"', '<!-- Aba 16: Notas Não Lançadas (Malha Fina) -->\n            \n            <div id="panel-tab-missing-invoices-log"')
]

for pattern, replacement in mappings:
    html = re.sub(pattern, replacement, html)

# Now, we must inject PIS/COFINS pending tab, because it was probably lost or inserted wrong!
# Let's verify if PIS/COFINS pending exists.
if 'id="panel-tab-pis-cofins-pending"' not in html:
    print("Injecting PIS/COFINS pending panel...")
    pis_cofins_panel = """
            <!-- Aba: Regras PIS/COFINS Pendentes -->
            <div id="panel-tab-pis-cofins-pending" class="v-tab-panel hidden p-6 min-w-0">
                {% if result.unmapped_pis_cofins_rules %}
                <div class="flex justify-between items-center mb-4">
                    <p class="text-sm text-slate-600 font-semibold">Os CFOPs e CSTs abaixo necessitam de mapeamento (Direito a Crédito de PIS/COFINS).</p>
                </div>
                <div class="overflow-x-auto w-full p-4 border border-slate-200 rounded-lg">
                    <table class="table table-zebra table-sm table-auto w-full text-slate-700">
                        <thead class="bg-slate-50 text-slate-500">
                            <tr>
                                <th class="pl-6 whitespace-nowrap">CFOP Origem</th>
                                <th>CST Lido</th>
                                <th>Permite Crédito de PIS/COFINS?</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in result.unmapped_pis_cofins_rules %}
                            <tr id="row-piscofins-{{ item.cfop }}-{{ item.cst_expected }}">
                                <td class="pl-6 font-mono font-bold text-orange-600 whitespace-nowrap">{{ item.cfop }}</td>
                                <td class="font-bold text-slate-700">{{ item.cst_expected }}</td>
                                <td>
                                    <div class="flex gap-2">
                                        <button class="btn btn-sm btn-success text-white shadow-sm" hx-post="/api/mapping/pis_cofins_rule" hx-vals='{"cfop": "{{ item.cfop }}", "cst_expected": "{{ item.cst_expected }}", "allow_credit": "true", "task_id": "{{ task_id }}"}' hx-target="#row-piscofins-{{ item.cfop }}-{{ item.cst_expected }}" hx-swap="outerHTML">
                                            <i class="fa-solid fa-check"></i> Sim, Permite
                                        </button>
                                        <button class="btn btn-sm btn-error text-white shadow-sm" hx-post="/api/mapping/pis_cofins_rule" hx-vals='{"cfop": "{{ item.cfop }}", "cst_expected": "{{ item.cst_expected }}", "allow_credit": "false", "task_id": "{{ task_id }}"}' hx-target="#row-piscofins-{{ item.cfop }}-{{ item.cst_expected }}" hx-swap="outerHTML">
                                            <i class="fa-solid fa-xmark"></i> Não Permite
                                        </button>
                                    </div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <div class="flex flex-col items-center justify-center py-16 text-center w-full">
                    <div class="bg-emerald-100 p-4 rounded-full mb-4"><i class="fa-solid fa-check text-emerald-600 text-3xl"></i></div>
                    <h3 class="text-xl font-bold text-slate-800">Tudo Mapeado!</h3>
                    <p class="text-slate-500 font-medium mt-1">Todas as regras de PIS/COFINS já estão configuradas.</p>
                </div>
                {% endif %}
            </div>
"""
    # Insert it right before <!-- Aba 14: Conferência PIS/COFINS -->
    html = html.replace('<!-- Aba 14: Conferência PIS/COFINS -->', pis_cofins_panel + '\n            <!-- Aba 14: Conferência PIS/COFINS -->')

with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("IDs fixed.")
