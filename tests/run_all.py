"""Executa todas as suítes de teste do Synax e resume o resultado.

Uso (a partir da RAIZ do projeto):
    ./venv/Scripts/python.exe tests/run_all.py

Cada suíte roda em processo próprio porque todas precisam neutralizar o dotenv
e redirecionar o banco para SQLite ANTES de importar a aplicação — o que só é
confiável num interpretador limpo.
"""
import os
import subprocess
import sys

# O console do Windows abre em cp1252: imprimir o detalhe de uma falha (que vem
# com acento e com o '�' do errors="replace") derrubava o resumo inteiro com
# UnicodeEncodeError — justo quando havia algo para ler.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SUITES = [
    ("test_security.py",    "Isolamento multi-tenant, RBAC, maker-checker e senha"),
    ("test_xss.py",         "Escape de HTML (XSS via descrição de produto) e cookie"),
    ("test_rbac.py",        "Operador travado fora de Admin/Curadoria"),
    ("test_no_autosave.py", "Motor não grava heurística; status por papel"),
    ("test_hardening.py",   "Headers, rate limiting, stack trace, .gitignore"),
    ("test_review_hardening.py", "Traversal no upload, IDOR do SIEG, credencial e rate limit"),
    ("test_email_gate.py",  "Gerador de e-mail restrito à equipe interna"),
    ("test_user_crud.py",   "Editar/excluir usuários (admin) e travas de lockout"),
    ("test_bugfix_delete_ipi.py", "Exclusão de regra CFOP e alvo do botão de IPI"),
    ("test_cfop_dropdown.py", "Seletor dinâmico de CFOP de entrada"),
    ("test_mapeamento_lote.py", "Lote por CFOP de origem: escopo, chave completa e recusa visível"),
    ("test_cfop_matching.py", "Casamento de CFOP por chave exata (sem regra genérica)"),
    ("test_sem_regras_globais.py", "Sem regra global no ICMS/IPI: chave exata, sem curinga, sem chute"),
    ("test_cfop_mapeado_decide_credito.py", "Credito sai do CFOP mapeado, nao do escriturado no SPED"),
    ("test_pis_cofins_valor_e_cte_outra_uf.py", "PIS/COFINS por valor, ICMSOutraUF no CT-e e itens nao conferidos"),
    ("test_sped_d190.py",    "Frete e ICMS do CT-e lidos do analítico D190"),
    ("test_pis_cofins_cte.py", "Crédito de PIS/COFINS sobre frete: base calculada (contábil − ICMS)"),
    ("test_nfse.py",         "Módulo NFS-e: parser, ADN, armazenamento e ZIP"),
    ("test_pdf_ia_auditor.py", "Auditoria de notas em PDF pela IA (Claude): contrato, falhas e Excel"),
    ("test_pdf_ia_lancamento.py", "CFOP 1933x2933 das notas de serviço: UF vem da empresa, sem chute"),
    ("test_nfse_performance.py", "Telas de certificado sem N+1 e ADN sem falha muda"),
    ("test_tenant_status_and_cert_fix.py", "Ativar/inativar empresa e correção do salvar de certificado"),
    ("test_icms_credito_itens_identicos.py", "Crédito de ICMS por soma de itens idênticos na mesma nota"),
    ("test_icms_mono.py", "ICMS Monofásico de combustíveis: opção, soma no crédito e aba"),
    ("test_difal_cfops.py", "CFOPs do relatório de DIFAL (2552 incluído), via constante única"),
    ("test_ai_error_reporter.py", "Diagnóstico automático de erros por IA (mascaramento, cooldown, sem chave)"),
    ("test_email_catalogo.py", "Gerador de e-mail: catálogo com as 19 divergências e textos padrão"),
    ("test_toast_autodispensa.py", "Toasts somem sozinhos (Curadoria empilhava avisos para sempre)"),
    ("test_relatorio_nfs_planilha.py", "Relatório de NFs: planilha sai inteira, não só a página aberta"),
    ("test_ipi_devolvido.py", "IPI devolvido (vIPIDevol) entra na conferência, com a conta aberta"),
    ("test_upload_rar_e_dropzone.py", "Pacote em RAR e arrastar-e-soltar que não falha calado"),
    ("test_ia_matcher.py", "IA casa só o não conferido, e só com confirmação humana"),
    ("test_item_match_rules.py", "Par aprovado vira regra: não se confirma o mesmo item duas vezes"),
    ("test_cod_sit.py", "COD_SIT: complementar e extemporâneo entram na apuração"),
    ("test_icms_st_cfops.py", "CFOPs de entrada que entram no cálculo de ICMS ST"),
    ("test_limpeza_uploads.py", "Limpeza de temp_uploads: apaga o abandonado, poupa o que está em uso"),
    ("test_idas_ao_banco.py", "Idas ao Supabase no caminho quente: contadas, não estimadas"),
    ("test_painel_atividade.py", "Painel do Radar: gráficos, uso por pessoa e topo de empresas"),
    ("test_ficha_empresa.py", "Ficha da empresa: última auditoria, curadoria represada e alertas"),
    ("test_competencia.py", "Competência do SPED: mês auditado, sem chute quando o 0000 falta"),
    ("test_sped_rewriter.py", "Reescrita do SPED: C190/C100/Bloco 9 refeitos, resto byte a byte"),
    ("test_cnpj_emitente.py", "CNPJ do emitente: participante do 0150 × emitente do XML"),
    ("test_xmls_faltantes.py", "XMLs Faltantes: CFOP escriturado da NF-e (C170) e do CT-e (D190)"),
    ("test_correcao_sped.py", "Correção do SPED: da divergência apontada ao arquivo baixado"),
    ("test_sped_builder.py", "Gerar SPED a partir dos XMLs, para importar no Domínio"),
    ("test_gerar_sped_mapa.py", "Gerador de SPED: CFOP de NF-e e CT-e por regra própria, aprovada antes de gerar"),
    ("test_sped_leitura.py", "Leitura do SPED: BOM, ponto decimal, linha torta e item sem documento"),
    ("test_sped_contribuicoes.py", "Segundo leiaute: EFD Contribuições por caixa na tela, com trava de arquivo trocado"),
    ("test_sped_debito_subapuracao.py", "Ajuste C195/C197 (débito sub-apuração) nos C190 de CFOP 5106/6106"),
    ("test_sped_validador.py", "Validador antigo (Python): leiaute do PVA, regras genéricas e Bloco 9, em paralelo até ser apagado"),
    ("test_validar_sped_pva.py", "Validar SPED no PVA oficial: motor falso, só 127.0.0.1, um por vez, fora do ar nunca verde"),
    ("test_novidades.py", "Novidades depois do login: só o não visto, por papel, gravado no banco"),
    ("test_reinicio_pendente.py", "Reinício pendente na Administração: só código que mudou de verdade"),
    ("test_busca_regras.py", "Busca única nas Regras por Empresa: no banco, sem vazar tenant"),
    ("test_planilhas.py", "Excel: estilo único, chave/CNPJ como texto e moeda formatada"),
    ("test_nfse_nacional.py", "NFS-e do ADN (layout nacional): nota completa, não só o número"),
    ("test_nfse_automacao.py", "Coleta agendada de NFS-e: hora de Brasília, sem rodar em dobro, adiada não é zero"),
    ("test_nfse_lote.py",    "Lote de NFS-e em segundo plano: não para no meio, pasta da empresa por dia, parar sem perder nota"),
    ("test_automacoes_informativos.py", "Informativos por e-mail: resumo das auditorias e erros do sistema, nulo não vira zero"),
    ("test_email_recebimento.py", "Leitura da caixa do escritório: XMLs dos clientes e resumo da manhã, sem mexer nos e-mails"),
    ("test_resumo_financeiro.py", "Dinheiro na tela: base não vira crédito, sentido certo"),
    ("test_erros_sistema.py", "Erros dentro do Synax: sem travessia de caminho, sem XSS"),
    ("test_configuracoes.py", "Configurações do admin: credencial do SIEG cifrada e fora da tela"),
    ("test_ux.py",          "Curadoria (data grid) e criação de usuários"),
    ("test_lucro_presumido.py", "Lucro Presumido: EFD-Contribuições pelo programa SPED PIS/COFINS, empresa e pasta conferidas"),
    ("test_dominio_programa.py", "Domínio: só leitura pelo programa; consulta livre só com a permissão do usuário"),
]

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if not os.path.isdir(os.path.join(RAIZ, "backend")):
        print("ERRO: rode a partir da raiz do projeto.")
        return 2

    total_ok = total = 0
    falhas = []

    for arquivo, descricao in SUITES:
        caminho = os.path.join(RAIZ, "tests", arquivo)
        if not os.path.exists(caminho):
            print(f"  IGNORADA  {arquivo} (não encontrada)")
            continue

        proc = subprocess.run(
            [sys.executable, caminho],
            cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        saida = (proc.stdout or "") + (proc.stderr or "")

        resultado = next(
            (l for l in saida.splitlines() if l.startswith("RESULTADO:")), None
        )
        if resultado:
            passaram, de = resultado.replace("RESULTADO:", "").split("passaram")[0].strip().split("/")
            total_ok += int(passaram)
            total += int(de)

        marcador = "OK  " if proc.returncode == 0 else "FALHA"
        print(f"  [{marcador}] {arquivo:<22} {resultado or 'sem resultado'}")
        print(f"           {descricao}")

        if proc.returncode != 0:
            falhas.append((arquivo, saida))

    print("\n" + "=" * 66)
    print(f"TOTAL: {total_ok}/{total} testes passaram em {len(SUITES)} suítes")

    if falhas:
        print(f"\n{len(falhas)} suíte(s) com falha — detalhe:\n")
        for arquivo, saida in falhas:
            print(f"--- {arquivo} " + "-" * 40)
            for linha in saida.splitlines():
                if "FALHA" in linha or "Error" in linha or "Traceback" in linha:
                    print("   ", linha)
        return 1

    print("Nenhuma regressão.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
