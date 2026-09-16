# Testes do Synax

**Rode antes de entregar qualquer alteração:**

```bash
./venv/Scripts/python.exe tests/run_all.py
```

Saída esperada: `TOTAL: 850/850 testes passaram em 22 suítes`.

---

## Por que estes testes existem

Cada suíte protege um comportamento que **já quebrou uma vez** neste projeto. As
falhas eram silenciosas — o sistema continuava respondendo 200, só que com a
regra de negócio errada. Nenhuma delas apareceria em teste manual rápido.

| Suíte | O que protege | Falha real que originou |
|---|---|---|
| `test_security.py` | Isolamento entre empresas, RBAC, maker-checker, reset de senha | Qualquer usuário logado lia a auditoria de outro cliente sabendo o `task_id`; três rotas SIEG estavam sem autenticação |
| `test_rbac.py` | Operador não acessa Admin nem Curadoria | Menus liberados por e-mail literal em vez de papel |
| `test_no_autosave.py` | Motor não grava regra deduzida; status conforme o papel | O motor criou 1.514 regras pendentes sozinho |
| `test_hardening.py` | Headers, rate limiting, stack trace, `.gitignore` | Login sem limite de tentativas; traceback exibido ao cliente |
| `test_review_hardening.py` | Nome de arquivo enviado pelo cliente, ZIP, proxy do SIEG, credenciais, poda do rate limit, regras globais | `UploadFile.filename` com `../` gravava fora da pasta efêmera; qualquer usuário logado baixava a NF-e de qualquer empresa pela chave; chave da API SIEG fixa no código; auditoria rodava sobre notas fictícias |
| `test_email_gate.py` | Gerador de e-mail restrito à equipe interna | — (trava preventiva) |
| `test_tenant_status_and_cert_fix.py` | Empresa inativa some para operador/cliente e some com tag para admin; salvar senha de certificado | `<form>` envolvendo `<td>`s é inválido em HTML5 — o navegador descarta o elemento e o botão "Salvar" fica sem form para submeter |
| `test_icms_credito_itens_identicos.py` | Crédito de ICMS somado por **código do produto** dentro da nota, nos dois lados | Casamento pegava sempre o 1º item do XML com aquele código: gerava divergência falsa quando a soma batia, e escondia divergência real (zero issues) quando a soma batia por acaso linha a linha. Duas tentativas de correção falharam antes: pôr descrição/valor unitário na chave (campos de fontes diferentes — 0200 x xProd, VL_ITEM/QTD x vUnCom), e depois casar só por código (o COD_ITEM do SPED é interno da empresa, o cProd do XML é do fornecedor — o casamento real é posicional). As duas zeravam um dos lados e inundavam a tela de divergências falsas |
| `test_ai_error_reporter.py` | Diagnóstico automático de erros por IA: mascara CNPJ/e-mail antes de sair para a Anthropic, não repete chamada por erro em loop (cooldown por assinatura), não trava sem `ANTHROPIC_API_KEY`, não entra em loop de log se a própria chamada à IA falhar | — (recurso novo, trava preventiva) |
| `test_email_catalogo.py` | Gerador de e-mail: catálogo único com as 19 categorias de divergência, texto padrão de cada uma, e escape do HTML | A aba tinha 5 opções para 19 tipos de apontamento; `unmapped_icms_cfops` é lista de strings e o montador chamava `.get()` nela (erro 500); a seção de CT-e lia `cte_issues`, chave que o motor nunca produziu — ficava sempre vazia |
| `test_toast_autodispensa.py` | Todo toast some sozinho, por conta do observador global em `layout/base.html` | O auto-dispensar era responsabilidade de cada tela; a Curadoria não mandava o script e os avisos de aprovação em lote empilhavam para sempre, cobrindo a tela |
| `test_icms_mono.py` | ICMS Monofásico de combustíveis: leitura das 4 tags, soma no crédito só com a opção marcada, e a aba do relatório | `vICMSMonoRet` (cobrado antes) e `vICMSMonoReten` (retido p/ frente) têm nomes quase iguais e valores/CSTs diferentes — trocá-las falseia o crédito |
| `test_ux.py` | Curadoria e criação de usuários | Botão de aprovar em lote apontava para rota inexistente |

## Regras de segurança dos testes

Todas as suítes:

1. **Neutralizam o `dotenv`** antes de importar a aplicação, para nunca lerem o
   `.env` real;
2. **Forçam SQLite** em arquivo temporário e **abortam** se o engine não for
   SQLite — trava contra rodar em produção por engano;
3. **Substituem o cliente Supabase** por um dublê em memória, sem rede.

Nenhum teste toca o banco de produção. Se você alterar o bootstrap, mantenha
essas três travas.

## Estrutura

Cada arquivo é **autocontido**: repete o bloco de bootstrap em vez de importar um
fixture comum. É duplicação proposital — assim uma suíte nunca derruba as outras,
e cada uma pode ser executada isoladamente:

```bash
./venv/Scripts/python.exe tests/test_security.py
```

## Ao adicionar comportamento novo

Se a mudança tocar **autorização, isolamento por empresa, status de regra
(aprovado/pendente) ou dados fiscais**, adicione o caso na suíte correspondente.
São exatamente as áreas onde um erro não gera exceção — só resultado errado.
