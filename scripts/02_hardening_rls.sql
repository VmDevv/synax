-- =====================================================================
-- SYNAX — BLINDAGEM DO RLS (defesa em profundidade)
-- =====================================================================
--
-- ############  NÃO APLIQUE AINDA — PRÉ-REQUISITO PENDENTE  ############
--
-- Verificado em 2026-08-10: a aplicação abre 53 de 54 conexões pela
-- dependência `get_db`, que NÃO injeta `app.current_tenant` / `app.current_role`.
-- Só o dashboard usa `get_rls_db`, que injeta.
--
-- Hoje isso não quebra nada porque a conexão usa `postgres` (BYPASSRLS) e as
-- políticas são ignoradas. Mas ao trocar para `synax_app` (sem bypass), as
-- variáveis estarão nulas na maioria das consultas, a política avaliará como
-- NULO e o Postgres devolverá ZERO LINHAS.
--
-- Sintoma: telas de auditoria, regras e empresas vazias, SEM erro e SEM log.
-- Parece perda de dados, mas os dados continuam lá — só invisíveis.
--
-- PRÉ-REQUISITO: injetar o contexto no nível da SESSÃO (event listener do
-- SQLAlchemy no `SessionLocal`), e não numa dependência de rota. Assim toda
-- sessão — inclusive as das tarefas em segundo plano, que não têm request —
-- carrega tenant e papel. Só depois disso a PARTE 2 pode ser aplicada.
--
-- A PARTE 1 (criar o papel) é inofensiva e pode ser executada antes.
-- ######################################################################
--
-- CONTEXTO (verificado em produção em 2026-08-06):
--   * A aplicação conecta como `postgres`, que tem rolbypassrls = true.
--     => TODA política de RLS é ignorada nessa conexão.
--   * Apenas 4 de 28 tabelas tinham RLS habilitado
--     (cfop_classifications, cfop_credit_rules, cte_cfop_classifications,
--      tenants, user_profiles); nenhuma com FORCE.
--   * Como o dono da tabela também é `postgres`, mesmo sem BYPASSRLS o RLS
--     seria ignorado sem FORCE ROW LEVEL SECURITY.
--
-- Ou seja: hoje o isolamento entre clientes é garantido SOMENTE pelo filtro
-- em Python. Este script adiciona a segunda camada no banco.
--
-- COMO APLICAR (requer decisão de infraestrutura — NÃO rode no escuro):
--   1) Rode a PARTE 1 para criar o papel da aplicação.
--   2) Troque DATABASE_URL no .env para usar `synax_app`.
--   3) Rode a PARTE 2 (habilita e força RLS + políticas).
--   4) Valide com a PARTE 3.
-- Aplicar a PARTE 2 sem trocar o usuário da conexão NÃO quebra nada
-- (postgres continua ignorando o RLS), mas também não protege.
-- =====================================================================


-- ---------------------------------------------------------------------
-- PARTE 1 — Papel dedicado da aplicação, SEM bypass de RLS
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synax_app') THEN
        -- Troque a senha antes de rodar.
        CREATE ROLE synax_app LOGIN PASSWORD 'TROQUE_ESTA_SENHA' NOBYPASSRLS;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO synax_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO synax_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO synax_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO synax_app;


-- ---------------------------------------------------------------------
-- PARTE 2 — Habilitar + FORÇAR RLS nas tabelas com dado de cliente
-- ---------------------------------------------------------------------
-- Tabelas cujo isolamento é por tenant_id (texto).
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'cfop_classifications',
        'cfop_credit_rules',
        'cte_cfop_classifications',
        'ipi_credit_rules',
        'pis_cofins_credit_rules',
        'icms_anticipation_rules',
        'audit_tasks',
        'missing_invoices_log',
        'tenant_integrations'
    ] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name = t) THEN
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
            EXECUTE format('DROP POLICY IF EXISTS synax_tenant_isolation ON public.%I', t);
            EXECUTE format($f$
                CREATE POLICY synax_tenant_isolation ON public.%I
                FOR ALL
                USING (
                    current_setting('app.current_role', true) = 'admin'
                    OR tenant_id = current_setting('app.current_tenant', true)
                    OR tenant_id = ANY (string_to_array(
                           coalesce(current_setting('app.allowed_tenants', true), ''), ','))
                    %s
                )
            $f$, t, CASE WHEN EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=t AND column_name='is_global')
                    THEN 'OR is_global = true' ELSE '' END);
        END IF;
    END LOOP;
END $$;

-- tenants / user_profiles / user_tenants: isolamento por identidade
ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS synax_tenants ON public.tenants;
CREATE POLICY synax_tenants ON public.tenants FOR ALL
USING (
    current_setting('app.current_role', true) = 'admin'
    OR id::text = current_setting('app.current_tenant', true)
    OR id::text = ANY (string_to_array(
           coalesce(current_setting('app.allowed_tenants', true), ''), ','))
);

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_profiles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS synax_user_profiles ON public.user_profiles;
CREATE POLICY synax_user_profiles ON public.user_profiles FOR ALL
USING (
    current_setting('app.current_role', true) = 'admin'
    OR id::text = current_setting('app.current_user_id', true)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name='user_tenants') THEN
        ALTER TABLE public.user_tenants ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.user_tenants FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS synax_user_tenants ON public.user_tenants;
        CREATE POLICY synax_user_tenants ON public.user_tenants FOR ALL
        USING (
            current_setting('app.current_role', true) = 'admin'
            OR user_id::text = current_setting('app.current_user_id', true)
        );
    END IF;
END $$;

-- cest_mva_rules é tabela de referência legal (Portaria 16), pública por natureza.
-- Fica fora do isolamento por tenant de propósito.


-- ---------------------------------------------------------------------
-- PARTE 3 — Validação
-- ---------------------------------------------------------------------
-- Deve listar rls=true e forced=true para todas as tabelas de cliente:
SELECT c.relname            AS tabela,
       c.relrowsecurity     AS rls_habilitado,
       c.relforcerowsecurity AS rls_forcado,
       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS politicas
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;

-- O papel da aplicação NÃO pode ter bypass:
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'synax_app';
