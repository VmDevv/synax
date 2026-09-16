import os
from pathlib import Path

# Configurações
OUTPUT_FILE = 'synax_full_context.md'
ROOT_DIR = '.'

IGNORE_DIRS = {
    '.git', '__pycache__', 'venv', 'env', 'node_modules', '.venv',
    'static/img', '.vscode', '.idea', 'alembic/versions', 'scratch', '.system_generated'
}

IGNORE_FILES = {
    OUTPUT_FILE, 'generate_context.py', '.DS_Store', 'project_context_claude.md'
}

ALLOWED_EXTENSIONS = {
    '.py', '.html', '.css', '.js', '.sql', '.example', '.ini'
}

ALLOWED_FILENAMES = {
    'alembic.ini', '.env.example'
}

def should_ignore_dir(dir_path, root):
    rel_path = os.path.relpath(dir_path, root).replace('\\', '/')
    for ignore in IGNORE_DIRS:
        if ignore in rel_path.split('/'):
            return True
        if rel_path.endswith(ignore) or rel_path.startswith(ignore):
             return True
    return False

def generate_tree(dir_path, prefix=""):
    tree_str = ""
    try:
        entries = sorted(os.listdir(dir_path))
    except PermissionError:
        return ""

    entries = [e for e in entries if e not in IGNORE_DIRS and e not in IGNORE_FILES]
    valid_entries = []
    for e in entries:
        full_path = os.path.join(dir_path, e)
        if os.path.isdir(full_path):
            if not should_ignore_dir(full_path, ROOT_DIR):
                valid_entries.append(e)
        else:
            valid_entries.append(e)
            
    entries = valid_entries
    
    for i, entry in enumerate(entries):
        path = os.path.join(dir_path, entry)
        is_last = (i == len(entries) - 1)
        connector = "└── " if is_last else "├── "
        
        if os.path.isdir(path):
            tree_str += f"{prefix}{connector}{entry}/\n"
            extension_prefix = "    " if is_last else "│   "
            tree_str += generate_tree(path, prefix + extension_prefix)
        else:
            tree_str += f"{prefix}{connector}{entry}\n"
            
    return tree_str

def get_allowed_files(root_dir):
    file_list = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if not should_ignore_dir(os.path.join(dirpath, d), root_dir)]
        
        for filename in filenames:
            if filename in IGNORE_FILES:
                continue
            
            ext = os.path.splitext(filename)[1].lower()
            if ext in ALLOWED_EXTENSIONS or filename in ALLOWED_FILENAMES:
                file_list.append(os.path.join(dirpath, filename))
                
    return sorted(file_list)

def get_markdown_language(filename):
    ext = os.path.splitext(filename)[1].lower()
    mapping = {
        '.py': 'python',
        '.html': 'html',
        '.css': 'css',
        '.js': 'javascript',
        '.sql': 'sql',
        '.ini': 'ini',
        '.example': 'bash'
    }
    return mapping.get(ext, 'text')

def main():
    print("Iniciando geração de contexto do projeto Synax...")
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out_f:
        out_f.write("# Contexto Profundo da Arquitetura - Synax\n\n")
        
        print("Gerando árvore de diretórios...")
        out_f.write("## 1. Estrutura de Diretórios\n\n")
        out_f.write("```text\n")
        out_f.write(".\n")
        out_f.write(generate_tree(ROOT_DIR))
        out_f.write("```\n\n")
        
        print("Coletando arquivos de código...")
        out_f.write("## 2. Código Fonte dos Componentes\n\n")
        
        files_to_read = get_allowed_files(ROOT_DIR)
        print(f"Encontrados {len(files_to_read)} arquivos válidos.")
        
        for filepath in files_to_read:
            rel_path = os.path.relpath(filepath, ROOT_DIR).replace('\\', '/')
            lang = get_markdown_language(rel_path)
            
            out_f.write(f"### Arquivo: `{rel_path}`\n\n")
            out_f.write(f"```{lang}\n")
            
            try:
                with open(filepath, 'r', encoding='utf-8') as in_f:
                    out_f.write(in_f.read())
            except UnicodeDecodeError:
                try:
                    with open(filepath, 'r', encoding='latin-1') as in_f:
                        out_f.write(in_f.read())
                except Exception as e:
                    out_f.write(f"// ERRO AO LER ARQUIVO: {str(e)}\n")
            except Exception as e:
                out_f.write(f"// ERRO AO LER ARQUIVO: {str(e)}\n")
                
            out_f.write("\n```\n\n")
            
    print(f"Contexto gerado com sucesso em: {os.path.abspath(OUTPUT_FILE)}")

if __name__ == "__main__":
    main()
