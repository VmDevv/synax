import re

def extract_js():
    with open('server.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Encontrar a variável JS_CONTENT
    match = re.search(r'JS_CONTENT = r"""(.*?)"""', content, re.DOTALL)
    if match:
        js_code = match.group(1)
        with open('frontend/static/js/app.js', 'w', encoding='utf-8') as js_file:
            js_file.write(js_code)
        print("JS extraído com sucesso para frontend/static/js/app.js")
    else:
        print("Não foi possível encontrar JS_CONTENT em server.py")

if __name__ == '__main__':
    extract_js()
