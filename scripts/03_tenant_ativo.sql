-- ==========================================
-- ADICIONA O STATUS ATIVO/INATIVO DA EMPRESA (TENANT)
-- ==========================================
-- Rode no SQL Editor do painel do Supabase. A aplicação já aplica esta mesma
-- coluna automaticamente em bancos SQLite locais (ver
-- backend/app/infrastructure/database/schema_upgrades.py); este script é o
-- equivalente manual para a instância Postgres/Supabase de produção, que roda
-- fora do controle da aplicação.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'tenants' AND column_name = 'ativo') THEN
        ALTER TABLE tenants ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;
END $$;
