import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. ADD BADGES TO SIDEBAR BUTTONS
badges = {
    'tab-missing-xmls': 'result.xmls_faltantes',
    'tab-new-cfops': '(result.unmapped_items|length + result.unmapped_cte_items|length + result.unmapped_icms_cfops|length)',
    'tab-divergences': 'result.divergencias_valor',
    'tab-cfop-issues': 'result.cfop_classifications_issues',
    'tab-icms-issues': 'result.icms_issues',
    'tab-cte-issues': 'result.cte_tomador_issues',
    'tab-icms-st': 'result.icms_st_notes',
    'tab-difal-int': 'result.difal_interno',
    'tab-difal-ext': 'result.difal_interestadual',
    'tab-cte-cfop-disc': 'result.cte_cfop_classifications_issues',
    'tab-ipi-issues': 'result.ipi_issues',
    'tab-pis-cofins-issues': 'result.pis_cofins_issues',
    'tab-anticipation': 'result.anticipation_results',
    'tab-missing-invoices-log': 'result.unrecorded_invoices',
}

for tab_id, data_prop in badges.items():
    if "length" in data_prop:
        jinja_expr = f"{{{{ {data_prop} }}}}"
        badge_cond = f"{{% if {data_prop} > 0 %}}"
    else:
        jinja_expr = f"{{{{ {data_prop} | length }}}}"
        badge_cond = f"{{% if {data_prop} %}}"
        
    badge_html = f'{badge_cond}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{jinja_expr}</span>{{% endif %}}</button>'
    pattern = rf'(<button type="button" onclick="switchVerticalTab\(\'{tab_id}\'.*?>.*?)(</button>)'
    html = re.sub(pattern, rf'\1 {badge_html}', html)

# ADD THE IPI PENDING RULE BUTTON TO SIDEBAR
ipi_btn = """                <button type="button" onclick="switchVerticalTab('tab-ipi-pending', this)" class="btn btn-ghost w-full justify-start text-left mb-1 text-slate-600 hover:bg-slate-100" data-tab="tab-ipi-pending">Regras IPI Pendentes {% if result.unmapped_ipi_rules %}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{{ result.unmapped_ipi_rules | length }}</span>{% endif %}</button>"""
html = re.sub(r'(data-tab="tab-ipi-issues".*?</button>)', rf'\1\n{ipi_btn}', html)


# 2. ADD KPI GRID INSIDE OVERVIEW PANEL
kpi_grid = """
                    <!-- GRID DE KPIS DINÂMICO -->
                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 mt-6">
                        
                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-missing-xmls')">
                            <div class="w-8 h-8 rounded-lg bg-red-100 text-red-600 flex items-center justify-center"><i class="fa-solid fa-file-circle-xmark"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.xmls_faltantes | length if result.xmls_faltantes else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">XMLs Faltantes</span></div>
                        </div>

                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-divergences')">
                            <div class="w-8 h-8 rounded-lg bg-amber-100 text-amber-600 flex items-center justify-center"><i class="fa-solid fa-triangle-exclamation"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.divergencias_valor | length if result.divergencias_valor else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Divergências de Valor</span></div>
                        </div>

                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-icms-issues')">
                            <div class="w-8 h-8 rounded-lg bg-purple-100 text-purple-600 flex items-center justify-center"><i class="fa-solid fa-coins"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.icms_issues | length if result.icms_issues else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Inconformidades ICMS</span></div>
                        </div>
                        
                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-ipi-pending')">
                            <div class="w-8 h-8 rounded-lg bg-orange-100 text-orange-600 flex items-center justify-center"><i class="fa-solid fa-industry"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.unmapped_ipi_rules | length if result.unmapped_ipi_rules else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Regras IPI Ausentes</span></div>
                        </div>

                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-ipi-issues')">
                            <div class="w-8 h-8 rounded-lg bg-pink-100 text-pink-600 flex items-center justify-center"><i class="fa-solid fa-industry"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.ipi_issues | length if result.ipi_issues else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Conferência IPI</span></div>
                        </div>

                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-pis-cofins-issues')">
                            <div class="w-8 h-8 rounded-lg bg-lime-100 text-lime-600 flex items-center justify-center"><i class="fa-solid fa-building-columns"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.pis_cofins_issues | length if result.pis_cofins_issues else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">PIS / COFINS</span></div>
                        </div>

                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-new-cfops')">
                            <div class="w-8 h-8 rounded-lg bg-cyan-100 text-cyan-600 flex items-center justify-center"><i class="fa-solid fa-circle-plus"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ (result.unmapped_items | length) + (result.unmapped_cte_items | length) + (result.unmapped_icms_cfops | length) }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Itens Novos a Mapear</span></div>
                        </div>
                        
                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-missing-invoices-log')">
                            <div class="w-8 h-8 rounded-lg bg-rose-100 text-rose-600 flex items-center justify-center"><i class="fa-solid fa-magnifying-glass-chart"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.unrecorded_invoices | length if result.unrecorded_invoices else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Malha Fina</span></div>
                        </div>
                    </div>
"""

html = html.replace(
    '<p class="text-sm text-slate-500 mt-4">Navegue pelas abas acima para ver os detalhes das divergências.</p>\n                </div>\n            </div>',
    f'<p class="text-sm text-slate-500 mt-4">Navegue pelo menu lateral para auditar as divergências encontradas.</p>\n{kpi_grid}\n                </div>\n            </div>'
)

# 3. ADD TAB PANEL FOR IPI PENDING
ipi_panel = """
            <!-- Aba: Regras IPI Pendentes -->
            <div id="panel-tab-ipi-pending" class="v-tab-panel hidden p-6 min-w-0">
                {% if result.unmapped_ipi_rules %}
                <div class="flex justify-between items-center mb-4">
                    <p class="text-sm text-slate-600 font-semibold">Os CFOPs abaixo necessitam de mapeamento (Direito a Crédito de IPI).</p>
                </div>
                <div class="overflow-x-auto w-full p-4 border border-slate-200 rounded-lg">
                    <table class="table table-zebra table-sm table-auto w-full text-slate-700">
                        <thead class="bg-slate-50 text-slate-500">
                            <tr>
                                <th class="pl-6 whitespace-nowrap">CFOP + CST</th>
                                <th>Produto</th>
                                <th>Permite Crédito?</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in result.unmapped_ipi_rules %}
                            <tr id="row-ipi-{{ item.cfop }}-{{ item.cst_ipi }}">
                                <td class="pl-6 font-mono font-bold text-orange-600 whitespace-nowrap">{{ item.cfop }} <span class="text-slate-400 font-normal">| CST {{ item.cst_ipi }}</span></td>
                                <td>{{ item.descr }}</td>
                                <td>
                                    <div class="flex gap-2">
                                        <button class="btn btn-sm btn-success text-white shadow-sm" hx-post="/api/mapping/ipi_rule" hx-vals='{"cfop": "{{ item.cfop }}", "cst_ipi": "{{ item.cst_ipi }}", "allow_credit": "true", "task_id": "{{ task_id }}"}' hx-target="#row-ipi-{{ item.cfop }}-{{ item.cst_ipi }}" hx-swap="outerHTML">
                                            <i class="fa-solid fa-check"></i> Sim, Permite
                                        </button>
                                        <button class="btn btn-sm btn-error text-white shadow-sm" hx-post="/api/mapping/ipi_rule" hx-vals='{"cfop": "{{ item.cfop }}", "cst_ipi": "{{ item.cst_ipi }}", "allow_credit": "false", "task_id": "{{ task_id }}"}' hx-target="#row-ipi-{{ item.cfop }}-{{ item.cst_ipi }}" hx-swap="outerHTML">
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
                    <p class="text-slate-500 font-medium mt-1">Todas as regras de IPI já estão configuradas.</p>
                </div>
                {% endif %}
            </div>
"""

# Try to insert it before Aba 13
if '<!-- Aba 13: Conferência IPI -->' in html:
    html = html.replace('<!-- Aba 13: Conferência IPI -->', f'{ipi_panel}\n            <!-- Aba 13: Conferência IPI -->')
else:
    # If standard text is missing, insert before panel-tab-ipi-issues
    html = html.replace('<div id="panel-tab-ipi-issues"', f'{ipi_panel}\n            <div id="panel-tab-ipi-issues"')


# 4. REMOVE OOB KPI SWAPS AT THE BOTTOM
oob_start = html.find('<!-- Atualizações OOB dos KPIs no Dashboard -->')
if oob_start != -1:
    script_start = html.find('<!-- Script de Paginação e Abas -->')
    if script_start != -1:
        html = html[:oob_start] + html[script_start:]

with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("Updated curation_results.html successfully.")
