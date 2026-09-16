from typing import Generic, TypeVar, Type, Optional, List, Any
from sqlalchemy.orm import Session
from sqlalchemy import select

T = TypeVar('T')

class BaseRepository(Generic[T]):
    def __init__(self, session: Session, model_class: Type[T], tenant_id: str):
        self.session = session
        self.model_class = model_class
        self.tenant_id = tenant_id

    def get_all(self) -> List[T]:
        # Só as regras do próprio tenant — mesmo critério de
        # RulesRepository._tenant_scope. Não existe regra global: uma regra
        # marcada is_global valia para todas as empresas do sistema, decidindo
        # crédito de ICMS/IPI que ninguém daquela empresa aprovou.
        stmt = select(self.model_class).where(
            self.model_class.tenant_id == self.tenant_id
        )
        if hasattr(self.model_class, 'status'):
            stmt = stmt.where(self.model_class.status == 'aprovado')
        return list(self.session.scalars(stmt).all())

    def get_by_id(self, id: Any) -> Optional[T]:
        stmt = select(self.model_class).where(
            self.model_class.id == id,
            self.model_class.tenant_id == self.tenant_id
        )
        return self.session.scalars(stmt).first()

    def add(self, obj: T) -> T:
        """Adiciona um novo objeto garantindo que o tenant_id correto seja injetado.

        SEM `refresh()` depois do commit. Ele custava uma segunda ida ao banco só
        para reler o que acabou de ser gravado, e nenhum chamador usa o objeto
        recarregado — nem o retorno. Cada ida ao Supabase custa ~140ms de rede
        contra ~0.1ms de execução, então o que pesa é o número de viagens.

        Se algum dia alguém precisar de um valor que o BANCO preenche (um default
        de coluna, por exemplo), chame `session.refresh(obj)` naquele ponto — e
        não aqui, onde todo mundo paga por um caso raro.
        """
        if hasattr(obj, 'tenant_id'):
            obj.tenant_id = self.tenant_id
        self.session.add(obj)
        self.session.commit()
        return obj
    
    def add_all(self, objs: List[T]):
        """Adiciona múltiplos objetos."""
        for obj in objs:
            if hasattr(obj, 'tenant_id'):
                obj.tenant_id = self.tenant_id
            self.session.add(obj)
        self.session.commit()
