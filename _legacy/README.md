# Código legado — não faz parte do sistema em execução

Nada aqui é importado, servido ou executado pela aplicação atual. Os arquivos
foram **movidos** (não apagados) para que ninguém — pessoa ou agente de IA —
edite o arquivo errado achando que está mexendo no sistema.

Verificado antes de mover: nenhum arquivo em `backend/`, `frontend/`, `scripts/`
ou `tests/` importa ou referencia qualquer item desta pasta.

## O que é cada coisa

| Item | O que era |
|---|---|
| `server.py` | Segunda geração do sistema: Flask monolítico (~2.500 linhas), single-tenant, com o JavaScript embutido como string Python. Usava `audit_rules.db` e `config.json`. |
| `config.json` | Configuração de caminhos do `server.py` (SPED, pasta de XMLs, pasta de CT-e). |
| `Antigo/` | Primeira geração: protótipo Flask em arquivo único. |
| `static/` | Frontend da geração antiga. A aplicação atual serve `frontend/static`. |
| `fix_ids.py`, `fix_ids2.py`, `fix_templates.py`, `refactor_tabs.py`, `update_ui*.py` | Scripts pontuais de migração/refatoração, já executados. |
| `check_ids.py`, `check_rules.py`, `dump_schema.py` | Scripts de diagnóstico usados durante a migração. |
| `generate_context.py` | Gerava o `synax_full_context.md` (dump do código para leitura por IA). |
| `test.html`, `old_rules*.txt` | Rascunhos. |

## O sistema atual

- Backend: `backend/app/` (FastAPI)
- Frontend: `frontend/templates/` + `frontend/static/`
- Scripts de manutenção **em uso**: `scripts/`
- Testes: `tests/` — rode `./venv/Scripts/python.exe tests/run_all.py`

## Pode apagar?

Provavelmente sim, mas **o repositório ainda não tem nenhum commit** — apagar
agora é irreversível. Recomendado: fazer o primeiro commit e só então remover
esta pasta, quando o histórico já servir de backup.
