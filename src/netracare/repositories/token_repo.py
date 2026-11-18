from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from netracare.db.models import RefreshToken
from netracare.repositories.base import Repository


class RefreshTokenRepository(Repository[RefreshToken]):
    model = RefreshToken

    def by_hash(self, token_hash: str) -> RefreshToken | None:
        return self.session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )

    def revoke(self, token: RefreshToken, now: datetime, replaced_by_id: str | None = None) -> None:
        token.revoked_at = now
        token.replaced_by_id = replaced_by_id
        self.session.flush()

    def revoke_family(self, family_id: str, now: datetime) -> int:
        result = self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def revoke_all_for_user(self, user_id: str, now: datetime) -> int:
        result = self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
