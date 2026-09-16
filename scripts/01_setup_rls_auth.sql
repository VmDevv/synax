-- ==========================================
-- SAAS TAX AUDIT: SETUP DE RLS E ISOLAMENTO MULTI-TENANT
-- ==========================================
-- Rode este script no SQL Editor do seu painel do Supabase.

-- 1. Criação das Tabelas de Negócio e Relacionamento com Supabase Auth
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    cnpj VARCHAR(20) UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'cliente', -- 'admin' ou 'cliente'
    name VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Adicionar o conceito de Regra Global nas tabelas existentes de regras
-- Se o robô não achar uma regra local, ele usa a global. A local SEMPRE sobrepõe a global.
DO $$ 
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cfop_classifications' AND column_name='is_global') THEN
        ALTER TABLE cfop_classifications ADD COLUMN is_global BOOLEAN DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cfop_credit_rules' AND column_name='is_global') THEN
        ALTER TABLE cfop_credit_rules ADD COLUMN is_global BOOLEAN DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cte_cfop_classifications' AND column_name='is_global') THEN
        ALTER TABLE cte_cfop_classifications ADD COLUMN is_global BOOLEAN DEFAULT FALSE;
    END IF;
END $$;

-- 3. Inserir a Empresa Teste (VmHub Contabilidade)
INSERT INTO tenants (name, cnpj) VALUES ('VmHub Contabilidade', '00000000000000') ON CONFLICT DO NOTHING;

-- ==========================================
-- BLINDAGEM DE BANCO DE DADOS (ROW LEVEL SECURITY - RLS)
-- ==========================================

-- Habilitar a segurança a nível de linha
ALTER TABLE cfop_classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE cfop_credit_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE cte_cfop_classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

-- ----------------------------------------------------
-- POLÍTICAS DE RLS (Como o Postgres vai filtrar os dados)
-- O SQLAlchemy via FastAPI irá injetar 'app.current_tenant' e 'app.current_role' na sessão.
-- ----------------------------------------------------

-- Tabela: cfop_classifications
CREATE POLICY "Admin vê tudo (classifications)" ON cfop_classifications FOR ALL 
USING (current_setting('app.current_role', true) = 'admin');

CREATE POLICY "Tenant isolado e Globais (classifications)" ON cfop_classifications FOR ALL 
USING (tenant_id = current_setting('app.current_tenant', true) OR is_global = true);

-- Tabela: cfop_credit_rules
CREATE POLICY "Admin vê tudo (credit_rules)" ON cfop_credit_rules FOR ALL 
USING (current_setting('app.current_role', true) = 'admin');

CREATE POLICY "Tenant isolado e Globais (credit_rules)" ON cfop_credit_rules FOR ALL 
USING (tenant_id = current_setting('app.current_tenant', true) OR is_global = true);

-- Tabela: cte_cfop_classifications
CREATE POLICY "Admin vê tudo (cte)" ON cte_cfop_classifications FOR ALL 
USING (current_setting('app.current_role', true) = 'admin');

CREATE POLICY "Tenant isolado e Globais (cte)" ON cte_cfop_classifications FOR ALL 
USING (tenant_id = current_setting('app.current_tenant', true) OR is_global = true);

-- Tabela: user_profiles
CREATE POLICY "Usuário vê seu próprio perfil" ON user_profiles FOR SELECT
USING (id::text = current_setting('app.current_user_id', true));

CREATE POLICY "Admin gerencia perfis" ON user_profiles FOR ALL
USING (current_setting('app.current_role', true) = 'admin');

-- Tabela: tenants
CREATE POLICY "Usuário vê sua própria empresa" ON tenants FOR SELECT
USING (id::text = current_setting('app.current_tenant', true));

CREATE POLICY "Admin gerencia empresas" ON tenants FOR ALL
USING (current_setting('app.current_role', true) = 'admin');
