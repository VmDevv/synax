import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv(override=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL e SUPABASE_KEY precisam ser configurados no arquivo .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_supabase_admin_client() -> Client:
    """Retorna um cliente Supabase com privilégios de Admin (Bypass RLS e Gestão de Usuários)"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_SERVICE_KEY precisa ser configurado no arquivo .env para gerenciar usuários.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
