import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Substituir o tablist inicial
content = content.replace(
    '<div role="tablist" class="tabs tabs-bordered w-full mb-6 overflow-x-auto justify-start">',
    '''<div class="flex flex-col lg:flex-row gap-6 w-full">
            <!-- Coluna Esquerda (Menu de Navegação) -->
            <div class="w-full lg:w-1/4 border-r border-slate-200 pr-4 flex flex-col gap-1" id="vertical-tab-menu">
            </div>
            
            <!-- Coluna Direita (Conteúdo) -->
            <div class="w-full lg:w-3/4" id="vertical-tab-content">'''
)

# Extrair todas as abas para gerar os botoes
tabs = re.findall(r'<input type="radio" name="audit_tabs" id="(.*?)" role="tab" class=".*?" aria-label="(.*?)"\s*(checked="checked")?\s*/>', content)

# Gerar botoes
buttons_html = ""
for i, tab in enumerate(tabs):
    tab_id = tab[0]
    label = tab[1]
    is_checked = "checked" in tab[2]
    
    # Define classes based on state
    if is_checked:
        btn_class = "btn btn-ghost w-full justify-start text-left mb-1 bg-blue-50 text-blue-600"
    else:
        btn_class = "btn btn-ghost w-full justify-start text-left mb-1 text-slate-600 hover:bg-slate-100"
        
    buttons_html += f'                <button type="button" onclick="switchVerticalTab(\'{tab_id}\', this)" class="{btn_class}" data-tab="{tab_id}">{label}</button>\n'

# Injetar botoes no menu
content = content.replace(
    '<div class="w-full lg:w-1/4 border-r border-slate-200 pr-4 flex flex-col gap-1" id="vertical-tab-menu">\n            </div>',
    f'<div class="w-full lg:w-1/4 border-r border-slate-200 pr-4 flex flex-col gap-1" id="vertical-tab-menu">\n{buttons_html}            </div>'
)

# Alterar os inputs de radio e tabpanels para divs comuns ocultas
for tab in tabs:
    tab_id = tab[0]
    is_checked = "checked" in tab[2]
    display_class = "block" if is_checked else "hidden"
    
    # Remover o input type="radio" original completo usando Regex para lidar com variações
    content = re.sub(
        rf'<input type="radio" name="audit_tabs" id="{tab_id}" role="tab" class=".*?" aria-label=".*?"\s*(checked="checked")?\s*/>',
        '',
        content
    )
    
    # O próximo '<div role="tabpanel"' vira a tab-panel normal
    # Mas pode haver múltiplos tabpanels. Para ser seguro, vamos trocar apenas aquele logo após a posição do input.
    # Mas como removemos o input, podemos só dar replace na string exata do tabpanel que tem p-6 min-w-0
    # Na verdade, é melhor trocar no conteúdo da string:
    content = content.replace(
        '<div role="tabpanel" class="tab-content p-6 min-w-0">',
        f'<div id="panel-{tab_id}" class="v-tab-panel {display_class} p-6 min-w-0">',
        1 # Replace just the next one
    )

# Fechar a div do main wrapper que agora é um flex. Originalmente a tag do tablist tinha um </div> correspondente.
# Na verdade, o </div> do tablist precisa ser encontrado. Ele fica no final de todos os tabpanels.
# Podemos procurar o último </div> antes de <!-- Atualizações OOB... e subtituí-lo.
content = content.replace('<!-- Atualizações OOB dos KPIs no Dashboard -->', '<!-- Fim coluna direita -->\n            </div>\n<!-- Atualizações OOB dos KPIs no Dashboard -->')
# Na real tem divs sobrando. Vamos adicionar o script para trocar as abas.

script = """
<!-- Script de Paginação e Abas -->
<script>
    function switchVerticalTab(tabId, btn) {
        // Esconde todos os paineis
        document.querySelectorAll('.v-tab-panel').forEach(p => p.classList.add('hidden'));
        document.querySelectorAll('.v-tab-panel').forEach(p => p.classList.remove('block'));
        
        // Remove active state dos botoes
        document.querySelectorAll('#vertical-tab-menu button').forEach(b => {
            b.classList.remove('bg-blue-50', 'text-blue-600');
            b.classList.add('text-slate-600', 'hover:bg-slate-100');
        });
        
        // Mostra o painel correto
        document.getElementById('panel-' + tabId).classList.remove('hidden');
        document.getElementById('panel-' + tabId).classList.add('block');
        
        // Adiciona active state no botao
        btn.classList.add('bg-blue-50', 'text-blue-600');
        btn.classList.remove('text-slate-600', 'hover:bg-slate-100');
    }
</script>
"""

content = content.replace('<!-- Script de Paginação Visível -->', script + '\n<!-- Script de Paginação Visível -->')

with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("HTML Refactored!")
