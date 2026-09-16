import os
import re

def fix_template_response(directory):
    pattern = re.compile(r'(templates\.TemplateResponse\()(\s*)([\"\'][^\"\']+[\"\'])(\s*,\s*\{)')
    count = 0
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                new_content = pattern.sub(r'\1request, \3\4', content)
                
                if new_content != content:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    count += 1
                    print(f'Fixed {filepath}')
    print(f'Total files modified: {count}')

fix_template_response('c:/Users/sandro.filho/Desktop/Conferencia entradas/backend')
