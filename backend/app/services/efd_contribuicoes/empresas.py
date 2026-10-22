"""Quais empresas do Domínio cada usuário pode ver — a mesma regra nas páginas Lucro Presumido e Domínio.

A lista vem do Domínio (as matrizes ativas, pelo programa SPED PIS/COFINS) e é cruzada com os tenants do Synax pelo
CNPJ. O admin vê todas — a que não está cadastrada aqui vem com `tenant` vazio, e a tela marca —; o operador vê só as
de `user_tenants`. O código da empresa chega pelo formulário, então TODA ação confere de novo com `permitida`.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ...core.authz import allowed_tenant_ids
from ...domain.models.auth_models import Tenant
from . import programa


class EmpresaRecusada(Exception):
    """A empresa pedida não é deste usuário (ou o código não é um código). A mensagem vai para a tela."""


@dataclass
class Empresa:
    codi_emp: int
    nome: str
    cnpj: str
    presumido: bool
    tenant: Tenant | None

    @property
    def cnpj_formatado(self) -> str:
        c = self.cnpj
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


def so_digitos(texto) -> str:
    return "".join(c for c in str(texto or "") if c.isdigit())


def do_usuario(db_session: Session, profile, atualizar: bool = False) -> list[Empresa]:
    """As empresas do Domínio que este perfil pode ver. Levanta ProgramaIndisponivel/FalhaDoPrograma."""
    lista = programa.empresas(atualizar)
    por_cnpj = {so_digitos(t.cnpj): t for t in db_session.query(Tenant).all() if t.cnpj}
    permitidos = allowed_tenant_ids(profile)
    saida = []
    for e in lista:
        tenant = por_cnpj.get(so_digitos(e.get("cnpj")))
        if permitidos is not None and (tenant is None or not tenant.ativo or str(tenant.id) not in permitidos):
            continue
        saida.append(Empresa(int(e["codi_emp"]), str(e.get("nome") or ""), so_digitos(e.get("cnpj")),
                             bool(e.get("presumido")), tenant))
    return saida


def permitida(db_session: Session, profile, codi_emp) -> Empresa:
    try:
        codi = int(str(codi_emp).strip())
    except ValueError:
        raise EmpresaRecusada("Escolha a empresa.")
    for e in do_usuario(db_session, profile):
        if e.codi_emp == codi:
            return e
    raise EmpresaRecusada("Empresa fora da sua lista. Se ela é sua, peça ao admin para vinculá-la ao seu usuário.")
