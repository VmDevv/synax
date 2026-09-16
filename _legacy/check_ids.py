with open('frontend/templates/partials/curation/curation_results.html', 'r', encoding='utf-8') as f:
    lines = f.read().splitlines()
buttons = [l.split('data-tab="')[1].split('"')[0] for l in lines if 'data-tab="' in l]
panels = [l.split('id="panel-')[1].split('"')[0] for l in lines if 'class="v-tab-panel' in l]

print("Buttons:")
for i, b in enumerate(buttons):
    print(f"{i}: {b}")
print("\\nPanels:")
for i, p in enumerate(panels):
    print(f"{i}: {p}")
