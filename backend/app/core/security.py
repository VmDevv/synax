import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# Fail-closed: uma chave gerada na hora seria diferente a cada restart/worker,
# tornando toda credencial já criptografada (ex: API key do SIEG) irrecuperável
# de forma silenciosa. Sem a chave configurada, o servidor não deve subir.
ENCRYPTION_KEY_B64 = os.getenv("FERNET_SECRET_KEY")
if not ENCRYPTION_KEY_B64:
    raise ValueError(
        "FERNET_SECRET_KEY não configurada no .env. Gere uma com "
        "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
        "e defina FERNET_SECRET_KEY antes de subir o servidor."
    )

fernet = Fernet(ENCRYPTION_KEY_B64.encode('utf-8'))

def encrypt_data(data: str) -> str:
    """Encrypts a string and returns the base64 encoded string."""
    if not data:
        return data
    return fernet.encrypt(data.encode('utf-8')).decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    """Decrypts a base64 encoded string back to plaintext."""
    if not encrypted_data:
        return encrypted_data
    return fernet.decrypt(encrypted_data.encode('utf-8')).decode('utf-8')
