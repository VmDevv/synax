import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. FIX THE IDS (if they are broken)
# I need to do string replacements carefully.
# In my previous check, I saw:
# <!-- Aba 4: Divergências de Valor -->
# <div id="panel-tab-new-cfops" class="v-tab-panel hidden p-6 min-w-0">
html = html.replace('<!-- Aba 4: Divergências de Valor -->\n            \n            <div id="panel-tab-new-cfops"', '<!-- Aba 4: Divergências de Valor -->\n            \n            <div id="panel-tab-divergences"')

# <!-- Aba 3: Novos CFOPs -->
# <div role="tabpanel" class="tab-content p-6 min-w-0" id="panel-new-cfops">
# I will change it to panel-tab-new-cfops and v-tab-panel
html = html.replace('<div role="tabpanel" class="tab-content p-6 min-w-0" id="panel-new-cfops">', '<div id="panel-tab-new-cfops" class="v-tab-panel hidden p-6 min-w-0">')


# 2. UPDATE THE BADGE FOR 'tab-new-cfops'
# It was: {{ (result.unmapped_items | length) + (result.unmapped_cte_items | length) + (result.unmapped_icms_cfops | length) }}
# Now it should not include unmapped_icms_cfops
html = html.replace('{{ (result.unmapped_items | length) + (result.unmapped_cte_items | length) + (result.unmapped_icms_cfops | length) }}', '{{ (result.unmapped_items | length) + (result.unmapped_cte_items | length) }}')

# Also in the sidebar button for tab-new-cfops:
html = html.replace('{% if (result.unmapped_items|length + result.unmapped_cte_items|length + result.unmapped_icms_cfops|length) > 0 %}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{{ (result.unmapped_items|length + result.unmapped_cte_items|length + result.unmapped_icms_cfops|length) }}</span>{% endif %}', '{% if (result.unmapped_items|length + result.unmapped_cte_items|length) > 0 %}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{{ (result.unmapped_items|length + result.unmapped_cte_items|length) }}</span>{% endif %}')


# 3. ADD ICMS SIDEBAR BUTTON
icms_btn = """                <button type="button" onclick="switchVerticalTab('tab-icms-pending', this)" class="btn btn-ghost w-full justify-start text-left mb-1 text-slate-600 hover:bg-slate-100" data-tab="tab-icms-pending">Regras ICMS Pendentes {% if result.unmapped_icms_cfops %}<span class="badge badge-sm badge-error text-white font-bold ml-auto">{{ result.unmapped_icms_cfops | length }}</span>{% endif %}</button>"""
# Add it before tab-icms-issues
html = re.sub(r'(<button.*?data-tab="tab-icms-issues")', rf'{icms_btn}\n                \1', html)


# 4. ADD ICMS KPI CARD IN THE GRID
icms_card = """
                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-icms-pending')">
                            <div class="w-8 h-8 rounded-lg bg-orange-100 text-orange-600 flex items-center justify-center"><i class="fa-solid fa-coins"></i></div>
                            <div><h4 class="text-xl font-bold text-slate-800">{{ result.unmapped_icms_cfops | length if result.unmapped_icms_cfops else 0 }}</h4><span class="text-[10px] font-semibold text-slate-500 uppercase">Regras ICMS Ausentes</span></div>
                        </div>"""
# Add it before the IPI missing card (which has <i class="fa-solid fa-industry"></i> and 'Regras IPI Ausentes')
html = html.replace("""<div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-ipi-pending')">""", icms_card + """\n                        <div class="bg-white border border-slate-200 shadow-sm rounded-xl p-4 flex flex-col gap-2 transition-transform hover:-translate-y-1 cursor-pointer" onclick="openCurationTab('tab-ipi-pending')">""")


# 5. ADD ICMS PANEL
icms_panel = """
            <!-- Aba: Regras ICMS Pendentes -->
            <div id="panel-tab-icms-pending" class="v-tab-panel hidden p-6 min-w-0">
                {% if result.unmapped_icms_cfops %}
                <div class="flex justify-between items-center mb-4">
                    <p class="text-sm text-slate-600 font-semibold">Os CFOPs abaixo necessitam de mapeamento (Direito a Crédito de ICMS).</p>
                </div>
                <div class="overflow-x-auto w-full p-4 border border-slate-200 rounded-lg">
                    <table class="table table-zebra table-sm table-auto w-full text-slate-700">
                        <thead class="bg-slate-50 text-slate-500">
                            <tr>
                                <th class="pl-6 whitespace-nowrap">CFOP Origem</th>
                                <th>Permite Crédito de ICMS?</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for cfop in result.unmapped_icms_cfops %}
                            <tr id="row-icms-{{ cfop }}">
                                <td class="pl-6 font-mono font-bold text-orange-600 whitespace-nowrap">{{ cfop }}</td>
                                <td>
                                    <div class="flex gap-2">
                                        <button class="btn btn-sm btn-success text-white shadow-sm" hx-post="/api/mapping/icms_rule" hx-vals='{"cfop": "{{ cfop }}", "allow_credit": "true", "task_id": "{{ task_id }}"}' hx-target="#row-icms-{{ cfop }}" hx-swap="outerHTML">
                                            <i class="fa-solid fa-check"></i> Sim, Permite
                                        </button>
                                        <button class="btn btn-sm btn-error text-white shadow-sm" hx-post="/api/mapping/icms_rule" hx-vals='{"cfop": "{{ cfop }}", "allow_credit": "false", "task_id": "{{ task_id }}"}' hx-target="#row-icms-{{ cfop }}" hx-swap="outerHTML">
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
                    <p class="text-slate-500 font-medium mt-1">Todas as regras de ICMS já estão configuradas.</p>
                </div>
                {% endif %}
            </div>
"""
# Insert before panel-tab-ipi-pending or panel-tab-ipi-issues
if '<div id="panel-tab-ipi-pending"' in html:
    html = html.replace('<div id="panel-tab-ipi-pending"', f'{icms_panel}\n            <div id="panel-tab-ipi-pending"')
else:
    html = html.replace('<div id="panel-tab-ipi-issues"', f'{icms_panel}\n            <div id="panel-tab-ipi-issues"')


# 6. ADD BULK MAPPING TO NEW-CFOPS
bulk_form = """
                <!-- Formulário de Mapeamento Rápido em Lote -->
                <div class="mb-6 p-4 bg-slate-50 rounded-lg border border-slate-200">
                    <h4 class="text-sm font-bold text-slate-700 mb-2"><i class="fa-solid fa-layer-group mr-1"></i> Mapeamento Rápido em Lote</h4>
                    <p class="text-xs text-slate-500 mb-4">Escolha um CFOP de origem para aplicar um CFOP de entrada para todos os seus produtos correspondentes simultaneamente.</p>
                    <form class="flex gap-4 items-end" hx-post="/api/mapping/cfop/bulk_by_cfop" hx-target="#bulk-result">
                        <input type="hidden" name="task_id" value="{{ task_id }}">
                        <div class="form-control w-full max-w-xs">
                            <label class="label"><span class="label-text font-semibold">CFOP de Origem:</span></label>
                            <select name="supplier_cfop" class="select select-bordered select-sm w-full font-mono" required>
                                <option value="" disabled selected>Selecione...</option>
                                {% set unique_cfops = [] %}
                                {% for item in result.cfops_desconhecidos %}
                                    {% if item.cfop not in unique_cfops %}
                                        {% set _ = unique_cfops.append(item.cfop) %}
                                        <option value="{{ item.cfop }}">{{ item.cfop }}</option>
                                    {% endif %}
                                {% endfor %}
                            </select>
                        </div>
                        <div class="form-control w-full max-w-[120px]">
                            <label class="label"><span class="label-text font-semibold">CFOP Entrada:</span></label>
                            <input type="text" name="entry_cfop" class="input input-bordered input-sm w-full font-mono text-center" placeholder="Ex: 1102" required>
                        </div>
                        <button type="submit" class="btn btn-sm btn-primary">Aplicar a Todos</button>
                    </form>
                    <div id="bulk-result"></div>
                </div>
"""

# Insert it inside panel-new-cfops, right after the title
html = html.replace("""<p class="text-sm text-slate-600 font-semibold">Os CFOPs abaixo precisam de uma regra de entrada correspondente.</p>""", f"""<p class="text-sm text-slate-600 font-semibold">Os CFOPs abaixo precisam de uma regra de entrada correspondente.</p>\n{bulk_form}""")


with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("Updated curation_results.html with bulk mappings and ICMS.")
