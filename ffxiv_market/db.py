import collections
import datetime
import logging
import threading
import time

import bcrypt
import psycopg2
import psycopg2.pool

from common import (
    CONFIG,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
)

_THREE_HOURS = 3600 * 3
_TWELVE_HOURS = _THREE_HOURS * 4

ItemPrice = collections.namedtuple('Price', ['timestamp', 'value', 'reporter', 'flagged'])
ItemState = collections.namedtuple('ItemState', ['name', 'id', 'price'])
ItemRef = collections.namedtuple('ItemRef', ['item_state', 'average'])
UserRef = collections.namedtuple('UserRef', ['name', 'id', 'anonymous'])
Flag = collections.namedtuple('Flag', ['item', 'user'])

_logger = logging.getLogger('db')

#Schema
"""
CREATE TABLE items(
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

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
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    related_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, related_item_id)
);

CREATE TABLE related_crafts_into(
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    related_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, related_item_id)
);
"""

_EPOCH = datetime.datetime.utcfromtimestamp(0)
def _datetime_to_epoch(timestamp):
    if timestamp is None:
        return None
    return int((timestamp - _EPOCH).total_seconds())
    
_epoch_to_datetime = datetime.datetime.utcfromtimestamp

class _Cache(object):
    """
    This should be replaced by a Materialized View when Postgres >= 9.3 is
    available.
    """
    _lock = None
    _item_refs = None
    _item_refs_by_name = None
    
    def __init__(self, item_data):
        self._lock = threading.Lock()
        self._item_refs = list(item_data)
        self._item_refs.sort(key=(lambda i: i.item_state.id))
        self._item_refs_by_name = dict((i.item_state.name, i) for i in self._item_refs)
        
    def _find_index(self, item_id):
        low = 0
        high = len(self._item_refs) - 1
        while low <= high:
            position = int((low + high) / 2)
            value = self._item_refs[position].item_state.id
            if value < item_id:
                low = position + 1
            elif value > item_id:
                high = position - 1
            else:
                return position
        return None
        
    def update(self, item_ref):
        with self._lock:
            record_index = self._find_index(item_ref.item_state.id)
            if record_index is None:
                self._item_refs_by_name[item_ref.item_state.name] = item_ref
                self._item_refs.append(item_ref)
                self._item_refs.sort(key=(lambda i: i.item_state.id))
            elif self._item_refs[record_index].item_state.price.timestamp < item_ref.item_state.price.timestamp:
                self._item_refs_by_name[self._item_refs[record_index].item_state.name] = item_ref
                self._item_refs[record_index] = item_ref
                
    def delete(self, item_id, timestamp):
        with self._lock:
            record_index = self._find_index(item_id)
            if record_index is not None:
                if self._item_refs[record_index].item_state.price.timestamp == timestamp:
                    self._item_refs_by_name.pop(self._item_refs.pop(record_index).item_state.name)
                    return True
        return False
        
    def get_item_names(self):
        with self._lock:
            return self._item_refs_by_name.keys()
            
    def get_item_by_id(self, item_id):
        with self._lock:
            record_index = self._find_index(item_id)
            if record_index is None:
                return None
            return self._item_refs[record_index]
            
    def get_item_by_name(self, item_name):
        with self._lock:
            return self._item_refs_by_name.get(item_name)
            
    def query(self, query_func):
        with self._lock:
            return query_func(self._item_refs)
            
class _Cursor(object):
    _pool = None
    _conn = None
    _cursor = None
    
    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._conn.set_session(autocommit=True)
        
    def __enter__(self):
        _logger.debug("Obtaining database connection")
        self._cursor = self._conn.cursor()
        return self._cursor
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cursor.close()
        self._pool.putconn(self._conn)
        _logger.debug("Released database connection")
        
class _Pool(psycopg2.pool.ThreadedConnectionPool):
    def get_cursor(self):
        return _Cursor(self)
        
class _Database(object):
    _pool = None
    _cache = None
    _related_lock = None
    
    def __init__(self):
        self._related_lock = threading.Lock()
        self._pool = _Pool(
            minconn=CONFIG['server']['postgres']['connections_min'],
            maxconn=CONFIG['server']['postgres']['connections_max'],
            host=CONFIG['server']['postgres']['host'],
            database=CONFIG['server']['postgres']['database'],
            user=CONFIG['server']['postgres']['username'],
            password=CONFIG['server']['postgres']['password'],
        )
        _logger.info("Initialising cache...")
        self._cache = _Cache(self._get_cache_data())
        _logger.info("Cache initialised...")
        
    def _iterate_results(self, cursor, buffer_size=128):
        while True:
            results = cursor.fetchmany(buffer_size)
            if results:
                for result in results:
                    yield result
            else:
                break
                
    def _get_cache_data(self):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT DISTINCT ON (items.id) items.name, items.id, prices.ts, prices.value
                FROM items, prices
                WHERE prices.item_id = items.id
                ORDER BY items.id ASC, prices.ts DESC""")
            for (item_name, item_id, ts, value) in self._iterate_results(cursor, buffer_size=512):
                yield ItemRef(
                    ItemState(
                        item_name, item_id, ItemPrice(
                            _datetime_to_epoch(ts), value, None, False
                        ),
                    ),
                    self._items_compute_average(item_id),
                )
                
    def users_create(self, username, password):
        with self._pool.get_cursor() as cursor:
            _logger.info("Clearing out stale registrations...")
            cursor.execute("""DELETE
                FROM users
                WHERE users.registered_ts <> NULL
                  AND users.registered_ts < (current_date - integer '7')""")
            _logger.info("Clearing out stale watchlists...")
            cursor.execute("""DELETE
                FROM watchlist
                USING users
                WHERE watchlist.user_id = users.id
                  AND users.last_seen_ts <> NULL
                  AND users.last_seen_ts < (current_date - integer '28')""")
            
            salt = bcrypt.gensalt()
            pwhash = bcrypt.hashpw(password, salt)
            _logger.info("Creating registration for user {user}...".format(
                user=username,
            ))
            cursor.execute("""INSERT
                INTO users (name, password_hash, password_salt)
                VALUES (%(name)s, %(hash)s, %(salt)s)""", {
                'name': username,
                'hash': pwhash,
                'salt': salt,
            })
            
    def users_set_recovery_password(self, username, password):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT users.id, users.password_salt, users.status
                FROM users
                WHERE users.name = %(name)s
                LIMIT 1""", {
                'name': username,
            })
            user = cursor.fetchone()
            if not user:
                return False
                
            (user_id, salt, status) = user
            if status == USER_STATUS_BANNED:
                return False
                
            pwhash = bcrypt.hashpw(password, salt)
            _logger.info("Creating password candidate for user {user}...".format(
                user=username,
            ))
            cursor.execute("""UPDATE users
                SET
                    password_hash_candidate = %(pwhash)s,
                    password_hash_candidate_ts = DATE_TRUNC('second', NOW() AT TIME ZONE 'utc')
                WHERE users.id = %(user_id)s""", {
                'user_id': user_id,
                'pwhash': pwhash,
            })
            return True
            
    def users_accept_recovery_password(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""UPDATE users
                SET
                    password_hash = users.password_hash_candidate,
                    password_hash_candidate = NULL,
                    password_hash_candidate_ts = NULL
                WHERE users.id = %(user_id)s""", {
                'user_id': user_id,
            })
            
    def users_login(self, username, password):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT users.id, users.password_hash, users.password_salt, users.status
                FROM users
                WHERE users.name = %(name)s
                LIMIT 1""", {
                'name': username,
            })
            result = cursor.fetchone()
            if result is not None:
                (user_id, password_hash, password_salt, status) = result
                if status == USER_STATUS_BANNED:
                    _logger.info("User {user} failed to log in: banned".format(
                        user=username,
                    ))
                    return -1
                    
                if bcrypt.hashpw(password, password_salt) == password_hash:
                    _logger.info("User {user} logged in".format(
                        user=username,
                    ))
                    return user_id
                else:
                    _logger.info("User {user} failed to log in: incorrect password".format(
                        user=username,
                    ))
            else:
                _logger.info("User {user} does not exist".format(
                    user=username,
                ))
            return None
            
    def users_mark_active(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""UPDATE users
                SET
                    last_seen_ts = DATE_TRUNC('second', NOW() AT TIME ZONE 'utc'),
                    password_hash_candidate = NULL,
                    password_hash_candidate_ts = NULL
                WHERE users.id = %(user_id)s""", {
                'user_id': user_id,
            })
            
    def users_list(self, status=None, order_most_recent=False):
        query = [
            "SELECT users.id, users.name, users.status "
            "FROM users",
        ]
        if status:
            query.append("WHERE users.status = %(status)s\n")
        if order_most_recent:
            query.append("ORDER BY users.last_seen_ts DESC")
        else:
            query.append("ORDER BY users.name ASC")
        with self._pool.get_cursor() as cursor:
            cursor.execute('\n'.join(query), {
                'status': status,
            })
            return list(self._iterate_results(cursor))
            
    def users_get_profile(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT users.name, users.anonymous, users.status, users.last_seen_ts, users.password_hash_candidate_ts
                FROM users
                WHERE users.id = %(user_id)s
                LIMIT 1""", {
                'user_id': user_id,
            })
            profile = cursor.fetchone()
            if profile is None:
                return None
                
            cursor.execute("""SELECT users.name, user_interactions.actor, user_interactions.ts, user_interactions.action, user_interactions.comment
                FROM user_interactions, users
                WHERE user_interactions.subject = %(user_id)s
                  AND users.id = user_interactions.actor
                ORDER BY ts DESC""", {
                'user_id': user_id,
            })
            actions_received = map(
                lambda (actor_name, actor, ts, action, comment): (actor_name, actor, _datetime_to_epoch(ts), action, comment),
                self._iterate_results(cursor)
            )
            cursor.execute("""SELECT users.name, user_interactions.subject, user_interactions.ts, user_interactions.action, user_interactions.comment
                FROM user_interactions, users
                WHERE user_interactions.actor = %(user_id)s
                  AND users.id = user_interactions.subject
                ORDER BY ts DESC""", {
                'user_id': user_id,
            })
            actions_performed = map(
                lambda (subject_name, subject, ts, action, comment): (subject_name, subject, _datetime_to_epoch(ts), action, comment),
                self._iterate_results(cursor)
            )
            
            cursor.execute("""SELECT COUNT(prices.submitting_user)
                FROM prices
                WHERE prices.submitting_user = %(user_id)s""", {
                'user_id': user_id,
            })
            prices_submitted = cursor.fetchone()[0]
            
            cursor.execute("""SELECT COUNT(flags.reported_by)
                FROM flags
                WHERE flags.reported_by = %(user_id)s""", {
                'user_id': user_id,
            })
            unresolved_flags = cursor.fetchone()[0]
            
            cursor.execute("""SELECT SUM(
                    CASE WHEN flags_history.deleted = true THEN 1
                         ELSE 0
                    END
                    ) AS valid, SUM(
                    CASE WHEN flags_history.deleted = false THEN 1
                         ELSE 0
                    END
                    ) AS invalid
                FROM flags_history
                WHERE flags_history.reported_by = %(user_id)s""", {
                'user_id': user_id,
            })
            (valid_flags_reported, invalid_flags_reported) = cursor.fetchone()
            #SUM over an empty set returns NULL, not 0
            valid_flags_reported = valid_flags_reported or 0
            invalid_flags_reported = invalid_flags_reported or 0
            
            cursor.execute("""SELECT COUNT(flags_history.submitting_user)
                FROM flags_history
                WHERE flags_history.submitting_user = %(user_id)s
                  AND flags_history.deleted = true""", {
                'user_id': user_id,
            })
            invalid_prices_submitted = cursor.fetchone()[0]
            
            (p_name, p_visible, p_status, p_last_seen_ts, p_password_hash_candidate_ts) = profile
            return (
                (p_name, p_visible, p_status,
                    _datetime_to_epoch(p_last_seen_ts),
                    _datetime_to_epoch(p_password_hash_candidate_ts),
                ),
                actions_received, actions_performed,
                prices_submitted, invalid_prices_submitted,
                unresolved_flags, valid_flags_reported, invalid_flags_reported,
            )
            
    def users_set_status(self, user_id, status):
        _logger.info("Changing user {id}'s status to {status}...".format(
            id=user_id,
            status=status,
        ))
        with self._pool.get_cursor() as cursor:
            cursor.execute("""UPDATE users
                SET
                    status = %(status)s,
                    registered_ts = NULL
                WHERE users.id = %(user_id)s""", {
                'status': status,
                'user_id': user_id,
            })
            
    def users_set_anonymous(self, user_id, anonymous):
        _logger.info("Changing user {id}'s visibility={visibility}...".format(
            id=user_id,
            visibility=(not anonymous),
        ))
        with self._pool.get_cursor() as cursor:
            cursor.execute("""UPDATE users
                SET
                    anonymous = %(anonymous)s
                WHERE users.id = %(user_id)s""", {
                'anonymous': anonymous,
                'user_id': user_id,
            })
            
    def users_get_identity(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT users.name, users.status, users.anonymous
                FROM users
                WHERE users.id = %(user_id)s
                LIMIT 1""", {
                'user_id': user_id,
            })
            return cursor.fetchone()
            
    def interactions_record(self, subject, actor, action, comment):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""INSERT
                INTO user_interactions (subject, actor, action, comment)
                VALUES (%(subject)s, %(actor)s, %(action)s, %(comment)s)""", {
                'subject': subject,
                'actor': actor,
                'action': action,
                'comment': comment,
            })
            
    def items_get_names(self):
        return self._cache.get_item_names()
        
    def items_name_to_id(self, item_name):
        item_ref = self._cache.get_item_by_name(item_name)
        return item_ref and item_ref.item_state.id
        
    def items_id_to_name(self, item_id):
        item_ref = self._cache.get_item_by_id(item_id)
        return item_ref and item_ref.item_state.name
        
    def items_get_latest_by_name(self, item_name):
        return self._cache.get_item_by_name(item_name)
        
    def items_get_latest_by_id(self, item_id):
        return self._cache.get_item_by_id(item_id)
        
    def _query__items_get_recently_updated(self, limit, max_age, items):
        candidates = [i for i in items if i.item_state.price.timestamp > max_age]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp), reverse=True)[:limit]
    def items_get_recently_updated(self, limit, max_age):
        return self._cache.query(lambda items: self._query__items_get_recently_updated(limit, max_age, items))
        
    def _query__items_get_most_valuable(self, limit, max_age, min_value, max_value, items):
        candidates = [i for i in items if i.item_state.price.timestamp > max_age and min_value <= i.item_state.price.value <= max_value]
        return sorted(candidates, key=(lambda i: i.item_state.price.value), reverse=True)[:limit]
    def items_get_most_valuable(self, limit, max_age, min_value, max_value):
        return self._cache.query(lambda items: self._query__items_get_most_valuable(limit, max_age, min_value, max_value, items))
        
    def _query__items_get_no_supply(self, limit, max_age, items):
        candidates = [i for i in items if i.item_state.price.timestamp > max_age and i.item_state.price.value == 0]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp), reverse=True)[:limit]
    def items_get_no_supply(self, limit, max_age):
        return self._cache.query(lambda items: self._query__items_get_no_supply(limit, max_age, items))
        
    def _query__items_get_stale(self, limit, min_age, max_age, items):
        candidates = [i for i in items if max_age < i.item_state.price.timestamp < min_age]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp))[:limit]
    def items_get_stale(self, limit, min_age, max_age):
        return self._cache.query(lambda items: self._query__items_get_stale(limit, min_age, max_age, items))
        
    def items_create_item(self, item_name):
        _logger.info("Creating item {name}...".format(
            name=item_name,
        ))
        with self._pool.get_cursor() as cursor:
            try:
                cursor.execute("""INSERT
                    INTO items(name)
                    VALUES(%(item_name)s)
                    RETURNING id""", {
                    'item_name': item_name,
                })
                return cursor.fetchone()[0]
            except Exception: #Item already exists: extremely unlikely race-condition
                cursor.execute("""SELECT items.id
                    FROM items
                    WHERE items.name = %(item_name)s""", {
                    'item_name': item_name,
                })
                return cursor.fetchone()[0]
                
    def _items_compute_average(self, item_id):
        #Computes the average price from -12h to -36h
        
        current_time = int(time.time())
        end_time = current_time - _TWELVE_HOURS
        start_time = end_time - _TWELVE_HOURS
        timeslices = collections.defaultdict(list)
        
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT ts, value
                FROM prices
                WHERE item_id = %(item_id)s
                  AND ts < %(end_ts)s
                  AND ts > %(start_ts)s
                LIMIT 128""", {
                'item_id': item_id,
                'end_ts': _epoch_to_datetime(end_time),
                'start_ts': _epoch_to_datetime(start_time),
            })
            for (ts, value) in self._iterate_results(cursor):
                if value:
                    timestamp = end_time - _datetime_to_epoch(ts)
                    timeslices[int(timestamp / _THREE_HOURS)].append(value)
        if len(timeslices) == 0:
            return None
        return int(sum(((max(v) + min(v)) / 2.0) for v in timeslices.values()) / len(timeslices))
        
    def items_add_price(self, item_id, value, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""INSERT
                INTO prices(item_id, value, submitting_user)
                VALUES(%(item_id)s, %(value)s, %(user_id)s)
                RETURNING ts""", {
                'item_id': item_id,
                'value': value,
                'user_id': user_id,
            })
            price = ItemPrice(_datetime_to_epoch(cursor.fetchone()[0]), value, None, False)
            
            #Update the cache
            item_name = self.items_id_to_name(item_id)
            if item_name is None: #First price: cache doesn't know about it
                cursor.execute("""SELECT name
                    FROM items
                    WHERE items.id = %(item_id)s
                    LIMIT 1""", {
                        'item_id': item_id,
                    })
                item_name = cursor.fetchone()[0]
            self._cache.update(ItemRef(
                ItemState(item_name, item_id, price),
                self._items_compute_average(item_id),
            ))
            
    def items_delete_price(self, item_id, timestamp, user_id=None):
        item_name = self.items_id_to_name(item_id)
        if item_name is None: #Unknown item
            return
            
        statement = [
            "DELETE "
            "FROM prices "
            "WHERE prices.item_id = %(item_id)s "
              "AND prices.ts = %(timestamp)s",
        ]
        if user_id is not None:
            statement.append("AND prices.submitting_user = %(user_id)s")
            
        with self._pool.get_cursor() as cursor:
            latest_deleted = self._cache.delete(item_id, timestamp)
            cursor.execute('\n'.join(statement), {
                'item_id': item_id,
                'timestamp': _epoch_to_datetime(timestamp),
                'user_id': user_id,
            })
            
            if latest_deleted:
                cursor.execute("""SELECT prices.ts, prices.value
                    FROM prices
                    WHERE prices.item_id = %(item_id)s
                    ORDER BY prices.ts DESC
                    LIMIT 1""", {
                    'item_id': item_id,
                })
                result = cursor.fetchone()
                if result is None: #All data is gone
                    cursor.execute("""DELETE
                        FROM items
                        WHERE items.id = %(item_id)s""", {
                        'item_id': item_id,
                    })
                else:
                    (timestamp, value) = result
                    self._cache.update(ItemRef(
                        ItemState(item_name, item_id, ItemPrice(
                            _datetime_to_epoch(timestamp),
                            value, None, False,
                        )),
                        self._items_compute_average(item_id),
                    ))
                    
    def items_get_prices(self, item_id, limit=None, max_age=None):
        query = [
            "SELECT prices.ts, prices.value, users.id, users.name, users.anonymous, flags.price_ts "
            "FROM users, "
                "prices LEFT OUTER JOIN flags ON (flags.price_item_id = prices.item_id AND flags.price_ts = prices.ts) "
            "WHERE prices.item_id = %(item_id)s "
              "AND prices.submitting_user = users.id "
        ]
        if max_age is not None:
            query.append("AND prices.ts > %(max_age)s")
        query.append("ORDER BY prices.ts DESC")
        if limit is not None:
            query.append("LIMIT %(limit)s")
            
        with self._pool.get_cursor() as cursor:
            cursor.execute('\n'.join(query), {
                'item_id': item_id,
                'max_age': _epoch_to_datetime(max_age),
                'limit': limit,
            })
            return [
                ItemPrice(_datetime_to_epoch(timestamp), value, UserRef(username, user_id, user_anonymous), bool(flagged))
                for (timestamp, value, user_id, username, user_anonymous, flagged)
                in self._iterate_results(cursor, buffer_size=512)
            ]
            
    def flags_create(self, item_id, timestamp, reporter):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""INSERT
                INTO flags (price_item_id, price_ts, reported_by)
                VALUES (%(item_id)s, %(timestamp)s, %(reporter)s)""", {
                'item_id': item_id,
                'timestamp': _epoch_to_datetime(timestamp),
                'reporter': reporter,
            })
            
    def flags_list(self):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT flags.price_item_id, items.name,
                    flags.price_ts, prices.value,
                    reporter.id, reporter.name, reporter.anonymous,
                    reportee.id, reportee.name, reportee.anonymous
                FROM flags, users AS reportee, users as reporter, prices, items
                WHERE flags.price_item_id = prices.item_id
                  AND flags.price_item_id = items.id
                  AND flags.price_ts = prices.ts
                  AND flags.reported_by = reporter.id
                  AND prices.submitting_user = reportee.id
                ORDER BY flags.price_ts ASC""")
            return map(
                lambda (
                    item_id, item_name,
                    price_ts, item_value,
                    reporter_id, reporter_name, reporter_anonymous,
                    reportee_id, reportee_name, reportee_anonymous,
                ): Flag(
                    ItemState(
                        item_name,
                        item_id,
                        ItemPrice(
                            _datetime_to_epoch(price_ts),
                            item_value,
                            UserRef(reportee_name, reportee_id, reportee_anonymous),
                            True,
                        ),
                    ),
                    UserRef(reporter_name, reporter_id, reporter_anonymous),
                ),
                self._iterate_results(cursor, 512)
            )
                        
    def flags_count(self):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT COUNT(flags.price_ts)
                FROM flags""")
            return cursor.fetchone()[0]
            
    def flags_resolve(self, item_id, timestamp, delete):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT flags.reported_by, prices.submitting_user
                FROM flags, prices
                WHERE flags.price_item_id = prices.item_id
                  AND flags.price_ts = prices.ts
                LIMIT 1""")
            (reported_by, submitting_user) = cursor.fetchone()
            
            if delete: #Cascade will clean up the flag
                cursor.execute("""DELETE
                    FROM prices
                    WHERE prices.item_id = %(item_id)s
                      AND prices.ts = %(timestamp)s""", {
                    'item_id': item_id,
                    'timestamp': _epoch_to_datetime(timestamp),
                })
            else:
                cursor.execute("""DELETE
                    FROM flags
                    WHERE flags.price_item_id = %(item_id)s
                      AND flags.price_ts = %(timestamp)s""", {
                    'item_id': item_id,
                    'timestamp': _epoch_to_datetime(timestamp),
                })
                
            cursor.execute("""INSERT
                INTO flags_history (item_id, price_ts, submitting_user, reported_by, deleted)
                VALUES (%(item_id)s, %(timestamp)s, %(submitter)s, %(reporter)s, %(deleted)s)""", {
                'item_id': item_id,
                'timestamp': _epoch_to_datetime(timestamp),
                'submitter': submitting_user,
                'reporter': reported_by,
                'deleted': delete,
            })
            
    def watchlist_add(self, user_id, item_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""INSERT
                INTO watchlist (user_id, item_id)
                VALUES (%(user_id)s, %(item_id)s)""", {
                'user_id': user_id,
                'item_id': item_id,
            })
            
    def watchlist_remove(self, user_id, item_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""DELETE
                FROM watchlist
                WHERE watchlist.user_id = %(user_id)s
                  AND watchlist.item_id = %(item_id)s""", {
                'user_id': user_id,
                'item_id': item_id,
            })
            
    def watchlist_list(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT watchlist.item_id
                FROM watchlist
                WHERE watchlist.user_id = %(user_id)s""", {
                'user_id': user_id,
            })
            return [self._cache.get_item_by_id(i[0]) for i in self._iterate_results(cursor)]
            
    def watchlist_is_watching(self, user_id, item_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT 0
                FROM watchlist
                WHERE watchlist.user_id = %(user_id)s
                  AND watchlist.item_id = %(item_id)s
                LIMIT 1""", {
                'user_id': user_id,
                'item_id': item_id,
            })
            return bool(cursor.fetchone())
            
    def watchlist_count(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT COUNT(watchlist.item_id)
                FROM watchlist
                WHERE watchlist.user_id = %(user_id)s""", {
                'user_id': user_id,
            })
            return cursor.fetchone()[0]
            
    def watchlist_get_most_watched(self, limit=None):
        query = [
            "SELECT COUNT(watchlist.item_id) as item_count, watchlist.item_id "
            "FROM watchlist "
            "GROUP BY watchlist.item_id "
            "ORDER BY item_count DESC ",
        ]
        if limit is not None:
            query.append("LIMIT %(limit)s")
            
        with self._pool.get_cursor() as cursor:
            cursor.execute('\n'.join(query), {
                'limit': limit,
            })
            return [self._cache.get_item_by_id(i[1]) for i in self._iterate_results(cursor)]
            
    def related_set(self, item_id, crafted_from, crafts_into):
        with self._related_lock:
            with self._pool.get_cursor() as cursor:
                cursor.execute("""DELETE
                    FROM related_crafted_from
                    WHERE item_id = %(item_id)s""", {
                    'item_id': item_id,
                })
                cursor.executemany("""INSERT
                    INTO related_crafted_from (item_id, related_item_id)
                    VALUES (%(item_id)s, %(related_item_id)s)""", ({
                    'item_id': item_id,
                    'related_item_id': v,
                } for v in crafted_from))
                
                cursor.execute("""DELETE
                    FROM related_crafts_into
                    WHERE item_id = %(item_id)s""", {
                    'item_id': item_id,
                })
                cursor.executemany("""INSERT
                    INTO related_crafts_into (item_id, related_item_id)
                    VALUES (%(item_id)s, %(related_item_id)s)""", ({
                    'item_id': item_id,
                    'related_item_id': v,
                } for v in crafts_into))
                
    def related_get(self, item_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT related_item_id
                FROM related_crafted_from
                WHERE item_id = %(item_id)s""", {
                'item_id': item_id,
            })
            crafted_from = [self._cache.get_item_by_id(i[0]) for i in self._iterate_results(cursor)]
            cursor.execute("""SELECT related_item_id
                FROM related_crafts_into
                WHERE item_id = %(item_id)s""", {
                'item_id': item_id,
            })
            return (crafted_from, [self._cache.get_item_by_id(i[0]) for i in self._iterate_results(cursor)])
DATABASE = _Database()
