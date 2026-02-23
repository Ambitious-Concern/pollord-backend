import uuid

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    encrypt_vote,
    decrypt_vote,
    generate_secure_token,
    generate_voter_hash,
    hash_password,
    sign_vote,
    verify_password,
    verify_vote_signature,
)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        password = "TestPassword123!"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("CorrectPassword1!")
        assert not verify_password("WrongPassword1!", hashed)

    def test_different_hashes_for_same_password(self):
        password = "SamePassword123!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)
        assert hash1 != hash2  # bcrypt uses random salt


class TestJWT:
    def test_create_and_decode_access_token(self):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == user_id
        assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self):
        user_id = str(uuid.uuid4())
        token = create_refresh_token(user_id)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == user_id
        assert payload["type"] == "refresh"

    def test_invalid_token_returns_none(self):
        payload = decode_token("invalid.token.here")
        assert payload is None


class TestVoteEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        vote_data = {"candidate_ids": [str(uuid.uuid4())]}
        encrypted = encrypt_vote(vote_data)
        decrypted = decrypt_vote(encrypted)
        assert decrypted == vote_data

    def test_different_data_different_ciphertext(self):
        data1 = {"candidate_ids": ["a"]}
        data2 = {"candidate_ids": ["b"]}
        enc1 = encrypt_vote(data1)
        enc2 = encrypt_vote(data2)
        assert enc1 != enc2


class TestVoterHash:
    def test_deterministic(self):
        user_id = uuid.uuid4()
        election_id = uuid.uuid4()
        hash1 = generate_voter_hash(user_id, election_id)
        hash2 = generate_voter_hash(user_id, election_id)
        assert hash1 == hash2

    def test_different_inputs_different_hash(self):
        user_id = uuid.uuid4()
        election1 = uuid.uuid4()
        election2 = uuid.uuid4()
        hash1 = generate_voter_hash(user_id, election1)
        hash2 = generate_voter_hash(user_id, election2)
        assert hash1 != hash2


class TestVoteSignature:
    def test_sign_and_verify(self):
        vote_data = b"encrypted_vote_data"
        cast_at = "2026-01-01T00:00:00"
        signature = sign_vote(vote_data, cast_at)
        assert verify_vote_signature(vote_data, cast_at, signature)

    def test_tampered_data_fails_verification(self):
        vote_data = b"encrypted_vote_data"
        cast_at = "2026-01-01T00:00:00"
        signature = sign_vote(vote_data, cast_at)
        assert not verify_vote_signature(b"tampered_data", cast_at, signature)


class TestSecureToken:
    def test_generates_string(self):
        token = generate_secure_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_unique_tokens(self):
        tokens = {generate_secure_token() for _ in range(100)}
        assert len(tokens) == 100
