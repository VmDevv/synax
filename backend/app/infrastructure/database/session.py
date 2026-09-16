import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env
load_dotenv()

# Se existir uma string do Supabase no .env, usamos ela. Caso contrário, fallback pro SQLite local.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./multi_tenant_audit.db"

# Engine
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Se a string no painel do Supabase estiver 'postgres://', o SQLAlchemy 1.4+ exige 'postgresql://'
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    # DevOps: Configuração de Pool para PostgreSQL / Supabase na nuvem (evita Erro 500 em polling concorrente)
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,        # Conexões quentes no pool (otimizado para concorrência Uvicorn)
        max_overflow=20,     # Expansão temporária sob picos de polling HTMX
        pool_timeout=15,     # Tempo de espera antes de falhar (evita travamento do ASGI)
        pool_pre_ping=True,  # Checa obrigatoriamente se o socket com Supabase está vivo antes de usar
        # Reciclar a cada 30s forçava novo handshake TCP+TLS (~300ms) o tempo todo.
        # O Supabase derruba conexões ociosas bem depois disso; 30min mantém o pool
        # quente e o pre_ping acima já protege contra socket morto.
        pool_recycle=1800,
        pool_use_lifo=True   # Reutiliza conexões quentes primeiro (LIFO)
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass

