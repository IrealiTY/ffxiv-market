CREATE TABLE base_items(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    lodestone_id TEXT UNIQUE NOT NULL
);
CREATE INDEX idx_base_items_name_lower ON base_items(LOWER(name));

CREATE TABLE items(
    id SERIAL PRIMARY KEY,
    base_item_id INTEGER NOT NULL REFERENCES(base_items),
    hq BOOLEAN NOT NULL
);
CREATE INDEX idx_items_base_item_id ON items(base_item_id);

CREATE TABLE users(
    id SERIAL NOT NULL PRIMARY KEY,
    registered_ts TIMESTAMP DEFAULT DATE_TRUNC('second', NOW() AT TIME ZONE 'utc'),
    last_seen_ts TIMESTAMP DEFAULT NULL,
    anonymous BOOLEAN DEFAULT true NOT NULL,
    status SMALLINT DEFAULT 0 NOT NULL,
    password_hash_candidate_ts TIMESTAMP DEFAULT NULL,
    name TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_hash_candidate TEXT DEFAULT NULL
);

CREATE TABLE user_interactions(
    subject INTEGER NOT NULL REFERENCES users(id),
    actor INTEGER NOT NULL REFERENCES users(id),
    ts TIMESTAMP DEFAULT DATE_TRUNC('second', NOW() AT TIME ZONE 'utc') NOT NULL,
    action TEXT NOT NULL,
    comment TEXT NOT NULL
);

CREATE TABLE prices(
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    ts TIMESTAMP DEFAULT DATE_TRUNC('second', NOW() AT TIME ZONE 'utc') NOT NULL,
    value INTEGER NOT NULL,
    submitting_user INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (item_id, ts)
);

CREATE TABLE flags(
    price_item_id INTEGER NOT NULL,
    price_ts TIMESTAMP NOT NULL,
    reported_by INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (price_item_id, price_ts),
    FOREIGN KEY (price_item_id, price_ts) REFERENCES prices(item_id, ts) ON DELETE CASCADE
);

CREATE TABLE flags_history(
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price_ts TIMESTAMP NOT NULL,
    submitting_user INTEGER NOT NULL REFERENCES users(id),
    reported_by INTEGER NOT NULL REFERENCES users(id),
    deleted BOOLEAN NOT NULL
);

CREATE TABLE watchlist(
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE related_crafted_from(
    base_item_id INTEGER NOT NULL REFERENCES base_items(id) ON DELETE CASCADE,
    related_base_item_id INTEGER NOT NULL REFERENCES base_items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, related_item_id)
);

CREATE TABLE related_crafts_into(
    base_item_id INTEGER NOT NULL REFERENCES base_items(id) ON DELETE CASCADE,
    related_base_item_id INTEGER NOT NULL REFERENCES base_items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, related_item_id)
);
