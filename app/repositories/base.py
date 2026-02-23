from typing import Generic, List, Optional, Type, TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    def __init__(self, model: Type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id: UUID, *, id_field: str = None) -> Optional[ModelType]:
        pk_col = self._get_pk_column(id_field)
        result = await self.session.execute(
            select(self.model).where(pk_col == id)
        )
        return result.scalar_one_or_none()

    async def get_all(
        self, *, skip: int = 0, limit: int = 100
    ) -> List[ModelType]:
        result = await self.session.execute(
            select(self.model).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, obj_in: dict) -> ModelType:
        db_obj = self.model(**obj_in)
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def update(
        self, id: UUID, obj_in: dict, *, id_field: str = None
    ) -> Optional[ModelType]:
        db_obj = await self.get_by_id(id, id_field=id_field)
        if db_obj is None:
            return None
        for key, value in obj_in.items():
            if value is not None:
                setattr(db_obj, key, value)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def delete(self, id: UUID, *, id_field: str = None) -> bool:
        db_obj = await self.get_by_id(id, id_field=id_field)
        if db_obj is None:
            return False
        await self.session.delete(db_obj)
        await self.session.flush()
        return True

    async def count(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(self.model)
        )
        return result.scalar_one()

    def _get_pk_column(self, id_field: str = None):
        if id_field:
            return getattr(self.model, id_field)
        mapper = self.model.__mapper__
        pk_cols = mapper.primary_key
        if len(pk_cols) == 1:
            return pk_cols[0]
        raise ValueError(
            f"Model {self.model.__name__} has composite PK. Specify id_field."
        )
