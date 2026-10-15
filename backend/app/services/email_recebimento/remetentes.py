"""A memória de remetentes: quem silenciar, quem é quem, de qual empresa fala.

Ver `EmailRemetente` para o porquê de cada campo. Aqui ficam as três regras de
aplicação:

1. **Endereço exato ganha de domínio, e domínio ganha do domínio-pai.**
   "sgd@dominiosistemas.com.br" > "@dominiosistemas.com.br" >
   "@sistemas.com.br" não existe como regra de ninguém, mas "@nibo.com.br"
   cobre "obrigacoes@mail.nibo.com.br".

2. **Não se silencia provedor de e-mail pessoal nem sufixo público.**
   "@gmail.com" calaria metade dos clientes — o cofre registra que boa parte
   dos contatos financeiros usa Gmail e Hotmail. Nesses, só endereço exato.

3. **Aprender a empresa é só a partir de XML, e nunca do próprio escritório.**
   Funcionário que encaminha o XML do cliente não "é" o cliente; e remetente
   que já trouxe XML de duas empresas perde a empresa aprendida.
"""
import uuid

from ...domain.models.email_recebido import EmailRemetente

DOMINIOS_PESSOAIS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.br", "outlook.com",
    "outlook.com.br", "live.com", "msn.com", "yahoo.com", "yahoo.com.br",
    "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br", "icloud.com",
    "me.com", "globo.com", "globomail.com", "zipmail.com.br", "r7.com",
}
SUFIXOS_PUBLICOS = {"com", "net", "org", "gov", "edu", "br", "com.br", "net.br",
                    "org.br", "gov.br", "edu.br", "adv.br", "cnt.br", "eng.br", "ind.br"}


class PadraoInvalido(ValueError):
    """O padrão não é endereço nem domínio aceitável. A mensagem é apresentável."""


def normalizar(texto) -> str:
    """ "Fulano <A@B.com>" -> "a@b.com"; "b.com" ou "@b.com" -> "@b.com".
    Levanta `PadraoInvalido` com o motivo."""
    bruto = (texto or "").strip().lower()
    if "<" in bruto and bruto.endswith(">"):
        bruto = bruto[bruto.rfind("<") + 1:-1].strip()
    if not bruto or " " in bruto:
        raise PadraoInvalido("Informe um e-mail (fulano@empresa.com.br) ou um domínio "
                             "(@empresa.com.br).")
    local, arroba, dominio = bruto.rpartition("@")
    if not arroba:
        local, dominio = "", bruto
    dominio = dominio.strip(".")
    if "." not in dominio or ".." in dominio:
        raise PadraoInvalido(f"\"{texto}\" não parece um e-mail nem um domínio.")
    if local:
        return f"{local}@{dominio}"
    if dominio in SUFIXOS_PUBLICOS:
        raise PadraoInvalido(f"\"@{dominio}\" pegaria empresas demais. Use o domínio "
                             "completo (@empresa.com.br) ou o endereço.")
    if dominio in DOMINIOS_PESSOAIS:
        raise PadraoInvalido(f"@{dominio} é e-mail pessoal — muitos clientes usam. "
                             "Aqui só dá para cadastrar o endereço exato.")
    return f"@{dominio}"


def dominio_de(endereco: str) -> str:
    return (endereco or "").lower().rpartition("@")[2]


def carregar(db) -> dict:
    """{padrão: EmailRemetente}. A tabela é pequena (dezenas de linhas)."""
    return {r.padrao: r for r in db.query(EmailRemetente).all()}


def regra_para(endereco: str, regras: dict):
    """A regra que vale para o endereço, ou None."""
    endereco = (endereco or "").strip().lower()
    if not endereco:
        return None
    if endereco in regras:
        return regras[endereco]
    partes = dominio_de(endereco).split(".")
    for i in range(len(partes) - 1):
        regra = regras.get("@" + ".".join(partes[i:]))
        if regra is not None:
            return regra
    return None


def silenciado(endereco: str, regras: dict) -> bool:
    regra = regra_para(endereco, regras)
    return bool(regra is not None and regra.silenciar)


def aprender_empresa(db, endereco: str, tenant_ids: set, dominios_proprios: set,
                     regras: dict) -> None:
    """Liga o remetente à empresa cujo XML ele mandou. Só endereço exato, só
    quando o e-mail trouxe XML de UMA empresa, e nunca por cima do que uma
    pessoa cadastrou. Não faz commit — vai junto com o e-mail."""
    endereco = (endereco or "").strip().lower()
    if not endereco or "@" not in endereco or len(tenant_ids) != 1:
        return
    if dominio_de(endereco) in dominios_proprios:
        return
    tenant_id = next(iter(tenant_ids))
    regra = regras.get(endereco)
    if regra is None:
        regra = EmailRemetente(id=str(uuid.uuid4()), padrao=endereco, silenciar=False,
                               tenant_id=tenant_id, origem="aprendido",
                               atualizado_por="aprendido pelos XMLs")
        db.add(regra)
        regras[endereco] = regra
        return
    if regra.origem != "aprendido" or regra.varias_empresas:
        return
    if regra.tenant_id and regra.tenant_id != tenant_id:
        regra.tenant_id = None
        regra.varias_empresas = True
    elif not regra.tenant_id:
        regra.tenant_id = tenant_id


def parece_automatico(msg) -> bool:
    """Cabeçalho de lista ou de envio automático. Só SUGESTÃO para a tela."""
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    if str(msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = str(msg.get("Auto-Submitted") or "").strip().lower()
    return bool(auto and auto != "no")
