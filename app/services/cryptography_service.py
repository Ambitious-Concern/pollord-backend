from uuid import UUID

from app.core.security import (
    decrypt_vote,
    encrypt_vote,
    generate_secure_token,
    generate_voter_hash,
    sign_vote,
    verify_vote_signature,
)


class CryptographyService:
    def encrypt_vote_data(self, candidate_ids: list[str]) -> bytes:
        return encrypt_vote({"candidate_ids": candidate_ids})

    def decrypt_vote_data(self, encrypted: bytes) -> dict:
        return decrypt_vote(encrypted)

    def generate_voter_hash(self, user_id: UUID, category_id: UUID) -> str:
        return generate_voter_hash(user_id, category_id)

    def sign_vote(self, encrypted_data: bytes, cast_at: str) -> str:
        return sign_vote(encrypted_data, cast_at)

    def verify_signature(
        self, encrypted_data: bytes, cast_at: str, signature: str
    ) -> bool:
        return verify_vote_signature(encrypted_data, cast_at, signature)

    def generate_receipt_code(self) -> str:
        return generate_secure_token(32)
