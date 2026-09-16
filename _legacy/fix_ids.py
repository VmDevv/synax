import re

with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    html = f.read()

# We need to ensure that the panel IDs match the buttons.
# Let's fix the known ones first:

# Aba 4 is Divergências de Valor (tab-divergences)
# Aba 5 is Divergências CFOP (tab-cfop-issues) - wait, panel 4 is tab-divergences again!
html = html.replace('<!-- Aba 5: Divergências de CFOP -->\n            \n            <div id="panel-tab-divergences"', '<!-- Aba 5: Divergências de CFOP -->\n            \n            <div id="panel-tab-cfop-issues"')

# Aba 6 is Regras ICMS Pendentes (tab-icms-pending)
html = html.replace('<!-- Aba 6: Regras ICMS Pendentes -->\n            \n            <div id="panel-tab-icms-issues"', '<!-- Aba 6: Regras ICMS Pendentes -->\n            \n            <div id="panel-tab-icms-pending"')

# Aba 7 is Inconformidades ICMS (tab-icms-issues)
html = html.replace('<!-- Aba 7: Inconformidades ICMS -->\n            \n            <div id="panel-tab-cte-issues"', '<!-- Aba 7: Inconformidades ICMS -->\n            \n            <div id="panel-tab-icms-issues"')

# Aba 8 is CT-e Tomador (tab-cte-issues)
html = html.replace('<!-- Aba 8: Inconformidades CT-e Tomador -->\n            \n            <div id="panel-tab-icms-st"', '<!-- Aba 8: Inconformidades CT-e Tomador -->\n            \n            <div id="panel-tab-cte-issues"')

# Aba 9 is ICMS ST (tab-icms-st)
html = html.replace('<!-- Aba 9: ICMS ST e Notas -->\n            \n            <div id="panel-tab-difal-int"', '<!-- Aba 9: ICMS ST e Notas -->\n            \n            <div id="panel-tab-icms-st"')

# Aba 10 is DIFAL Interno (tab-difal-int)
html = html.replace('<!-- Aba 10: DIFAL Interno -->\n            \n            <div id="panel-tab-difal-ext"', '<!-- Aba 10: DIFAL Interno -->\n            \n            <div id="panel-tab-difal-int"')

# Aba 11 is DIFAL Interestadual (tab-difal-ext)
html = html.replace('<!-- Aba 11: DIFAL Interestadual -->\n            \n            <div id="panel-tab-cte-cfop-disc"', '<!-- Aba 11: DIFAL Interestadual -->\n            \n            <div id="panel-tab-difal-ext"')

# Aba 12 is CFOP CT-e (tab-cte-cfop-disc)
html = html.replace('<!-- Aba 12: Divergência de CFOP (CT-e) -->\n            \n            <div id="panel-tab-icms-pending"', '<!-- Aba 12: Divergência de CFOP (CT-e) -->\n            \n            <div id="panel-tab-cte-cfop-disc"')

# Aba 13 is Relatório CT-e (tab-cte-gen-rep)
html = html.replace('<!-- Aba 13: Relatório Geral (CT-e) -->\n            \n            <div id="panel-tab-ipi-pending"', '<!-- Aba 13: Relatório Geral (CT-e) -->\n            \n            <div id="panel-tab-cte-gen-rep"')

# Aba 14 is Conferência IPI (tab-ipi-issues)
html = html.replace('<!-- Aba 14: Conferência de IPI -->\n            \n            <div id="panel-tab-cte-gen-rep"', '<!-- Aba 14: Conferência de IPI -->\n            \n            <div id="panel-tab-ipi-issues"')

# Aba 15 is Regras IPI Pendentes (tab-ipi-pending)
html = html.replace('<!-- Aba: Regras IPI Pendentes -->\n            <div id="panel-tab-ipi-issues"', '<!-- Aba: Regras IPI Pendentes -->\n            <div id="panel-tab-ipi-pending"')

# Aba 16 is Conferência PIS/COFINS (tab-pis-cofins-issues)
# Note: I renamed it to panel-tab-anticipation earlier by mistake!
html = html.replace('<!-- Aba 14: Conferência PIS/COFINS -->\n            \n            <div id="panel-tab-ipi-issues"', '<!-- Aba 14: Conferência PIS/COFINS -->\n            \n            <div id="panel-tab-pis-cofins-issues"')

# Wait, there are multiple "Aba 14" now? Let's fix them strictly by their content comment:
with open('frontend/templates/partials/curation/curation_results.html', 'w', encoding='utf-8') as f:
    f.write(html)
