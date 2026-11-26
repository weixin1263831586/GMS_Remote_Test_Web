from features.users.storage_paths import owner_storage_key


def test_long_owner_keys_keep_distinct_hash_suffixes():
    prefix = "a" * 180
    keys = {owner_storage_key(prefix + suffix) for suffix in ("x", "y", "/x", "/y")}
    assert len(keys) == 4
    assert all(len(key) == 160 for key in keys)


def test_short_owner_keys_are_stable():
    assert owner_storage_key("user-123") == "user-123"
    assert owner_storage_key("") == "anonymous"
    assert owner_storage_key("a/b") == "a_b_c14cddc033f6"
