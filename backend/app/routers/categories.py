import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession
from app.models import Category, Meeting, User
from app.schemas import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])

DUPLICATE = "You already have a category with this name"


def get_owned_category(db: Session, user: User, category_id: uuid.UUID) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    return category


def _count(db: Session, category_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(Meeting).where(Meeting.category_id == category_id))


def _out(category: Category, meeting_count: int) -> CategoryOut:
    return CategoryOut(
        id=category.id,
        name=category.name,
        color=category.color,
        meeting_count=meeting_count,
        created_at=category.created_at,
    )


@router.get("", response_model=list[CategoryOut])
def list_categories(user: CurrentUser, db: DbSession):
    counts = (
        select(Meeting.category_id, func.count().label("n"))
        .where(Meeting.owner_id == user.id)
        .group_by(Meeting.category_id)
        .subquery()
    )
    rows = db.execute(
        select(Category, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .where(Category.owner_id == user.id)
        .order_by(Category.name)
    ).all()
    return [_out(category, count) for category, count in rows]


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(body: CategoryCreate, user: CurrentUser, db: DbSession):
    category = Category(owner_id=user.id, name=body.name, color=body.color)
    db.add(category)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE) from None
    return _out(category, 0)


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(category_id: uuid.UUID, body: CategoryUpdate, user: CurrentUser, db: DbSession):
    category = get_owned_category(db, user, category_id)
    if "name" in body.model_fields_set and body.name is not None:
        category.name = body.name
    if "color" in body.model_fields_set:
        category.color = body.color
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE) from None
    return _out(category, _count(db, category.id))


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Meetings in this category are kept and become uncategorized."""
    category = get_owned_category(db, user, category_id)
    db.execute(update(Meeting).where(Meeting.category_id == category.id).values(category_id=None))
    db.delete(category)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
