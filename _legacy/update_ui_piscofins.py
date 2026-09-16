import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. ADD SIDEBAR BUTTON for PIS/COFINS
sidebar_btn = """                <button type="button" onclick="switchVerticalTab('tab-pis-cofins-pending', this)" class="btn btn-ghost w-full justify-start text-left mb-1 text-slate-600 hover:bg-slate-100" data-tab="tab-pis-cofins-pending">Regras PIS/COFINS Pendentes {% if result.unmapped_pis_cofins_rules %}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{{ result.unmapped_pis_cofins_rules | length }}</span>{% endif %}</button>"""
html = re.sub(r'(<button.*?data-tab="tab-pis-cofins-issues")', rf'{sidebar_btn}\n                \1', html)

# 2. ADD KPI CARD for PIS/COFINS
kpi_card = """
                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-pis-cofins-pending')">
                            <div class="w-8 h-8 rounded-lg bg-indigo-100 text-indigo-600 flex items-center justify-center"><i class="fa-solid fa-scale-balanced"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.unmapped_pis_cofins_rules | length if result.unmapped_pis_cofins_rules else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Regras PIS/COFINS Ausentes</span></div>
                        </div>"""
html = html.replace("""<div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-pis-cofins-issues')">""", kpi_card + """\n                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-pis-cofins-issues')">""")

# 3. ADD PIS/COFINS PANEL
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
html = html.replace('<div id="panel-tab-pis-cofins-issues"', f'{pis_cofins_panel}\n            <div id="panel-tab-pis-cofins-issues"')

with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("Updated curation_results.html with PIS/COFINS interactive mapping.")
