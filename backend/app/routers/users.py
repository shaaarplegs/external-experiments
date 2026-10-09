from fastapi import APIRouter, HTTPException, Response, status

from app.deps import CurrentUser, DbSession
from app.routers.admin import ensure_not_last_admin
from app.routers.auth import revoke_all_refresh_tokens
from app.schemas import PasswordChange, UserOut, UserUpdate
from app.security import hash_password, verify_password

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def get_me(user: CurrentUser):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: UserUpdate, user: CurrentUser, db: DbSession):
    if "full_name" in body.model_fields_set:
        user.full_name = body.full_name
    db.commit()
    return user


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordChange, user: CurrentUser, db: DbSession):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    revoke_all_refresh_tokens(db, user.id)  # sign out other devices
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user: CurrentUser, db: DbSession):
    """Deletes the account and all of its meetings (Google Play requires in-app deletion)."""
    ensure_not_last_admin(db, user)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
