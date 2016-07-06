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
ItemState = collections.namedtuple('ItemState', ['name', 'id', 'hq', 'price'])
ItemName = collections.namedtuple('ItemName', ['en', 'ja', 'fr', 'de'])
ItemRef = collections.namedtuple('ItemRef', ['item_state', 'average'])
UserRef = collections.namedtuple('UserRef', ['name', 'id', 'anonymous'])
Flag = collections.namedtuple('Flag', ['item', 'user'])

_logger = logging.getLogger('db')

_EPOCH = datetime.datetime.utcfromtimestamp(0)
def _datetime_to_epoch(timestamp):
    if timestamp is None:
        return None
    return int((timestamp - _EPOCH).total_seconds())
    
_epoch_to_datetime = datetime.datetime.utcfromtimestamp

class WriteWaitLock(object):
    def __init__(self):
        self._reader_lock = threading.Condition(threading.Lock())
        self._readers = 0

    def read_start(self):
        with self._reader_lock:
            self._readers += 1
            
    def read_stop(self):
        with self._reader_lock:
            self._readers -= 1
            if not self._readers:
                self._reader_lock.notify_all()
                
    def write_start(self):
        self._reader_lock.acquire()
        while self._readers:
            self._read_ready.wait()
            
    def write_stop(self):
        self._reader_lock.release()
        
class _Cache(object):
    """
    This should be replaced by a Materialized View when Postgres >= 9.3 is
    available.
    """
    _lock = None
    _item_refs = None
    
    def __init__(self, item_data):
        self._lock = WriteWaitLock()
        
        self._item_refs = list(item_data)
        
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
        self._lock.write_start()
        try:
            self._item_refs[self._find_index(item_ref.item_state.id)] = item_ref
        finally:
            self._lock.write_stop()
            
    def delete(self, item_id, timestamp):
        self._lock.write_start()
        try:
            record_index = self._find_index(item_id)
            if self._item_refs[record_index].item_state.price and self._item_refs[record_index].item_state.price.timestamp == timestamp:
                item_ref = self._item_refs[record_index]
                self._item_refs[record_index] = ItemRef(
                    ItemState(
                        item_ref.item_state.name, item_ref.item_state.id, item_ref.item_state.hq, None,
                    ),
                    None,
                )
                return True
        finally:
            self._lock.write_stop()
        return False
        
    def get_item_by_id(self, item_id):
        self._lock.read_start()
        try:
            record_index = self._find_index(item_id)
            if record_index is None:
                return None
            return self._item_refs[record_index]
        finally:
            self._lock.read_stop()
            
    def query(self, query_func):
        self._lock.read_start()
        try:
            return query_func(self._item_refs)
        finally:
            self._lock.read_stop()
            
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
        _logger.info("Cache initialised")
        
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
            cursor.execute("""SELECT DISTINCT ON (items.id) items.id, items.hq, prices.ts, prices.value,
                     base_items.name_en, base_items.name_ja, base_items.name_fr, base_items.name_de
                FROM items, prices, base_items
                WHERE prices.item_id = items.id
                  AND base_items.id = items.base_item_id
                ORDER BY items.id ASC, prices.ts DESC""")
            for (item_id, hq, ts, value, name_en, name_ja, name_fr, name_de) in self._iterate_results(cursor, buffer_size=512):
                yield ItemRef(
                    ItemState(
                        ItemName(name_en, name_ja, name_fr, name_de), item_id, hq, ItemPrice(
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
            cursor.execute("""SELECT users.name, users.language, users.anonymous, users.status, users.last_seen_ts, users.password_hash_candidate_ts
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
            
            (p_name, p_language, p_visible, p_status, p_last_seen_ts, p_password_hash_candidate_ts) = profile
            return (
                (p_name, p_language, p_visible, p_status,
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
            
    def users_set_language(self, user_id, language):
        _logger.info("Changing user {id}'s language={language}...".format(
            id=user_id,
            language=language,
        ))
        with self._pool.get_cursor() as cursor:
            cursor.execute("""UPDATE users
                SET
                    language = %(language)s
                WHERE users.id = %(user_id)s""", {
                'language': language,
                'user_id': user_id,
            })
            
    def users_get_identity(self, user_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT users.name, users.language, users.status, users.anonymous
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
            
    def items_search(self, language, filter, limit):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT base_items.name_{language}, items.id, items.hq
                FROM items, base_items
                WHERE LOWER(base_items.name) LIKE '%%%(filter)s%%'
                  AND base_items.id = items.base_item_id
                ORDER BY base_items.name ASC, items.hq ASC
                LIMIT %(limit)s""".format(language=language), {
                'filter': filter,
                'limit': limit,
            })
            return list(self._iterate_results(cursor))
            
    def items_get_properties(self, language, item_id):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT base_items.name_{language}, base_items.id, base_items.lodestone_id, items.hq
                FROM items, base_items
                WHERE items.id = %(item_id)s
                  AND base_items.id = items.base_item_id
                LIMIT 1""".format(language=language), {
                'item_id': item_id,
            })
            return cursor.fetchone()
            
    def items_get_hq_variant_id(self, xivdb_item_id, hq):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT items.id
                FROM items
                WHERE items.base_item_id = %(xivdb_item_id)s
                  AND items.hq = %(hq)s
                LIMIT 1""", {
                'xivdb_item_id': xivdb_item_id,
                'hq': hq,
            })
            return cursor.fetchone()
            
    def items_get_latest_by_id(self, item_id):
        return self._cache.get_item_by_id(item_id)
        
    def _query__items_get_recently_updated(self, limit, max_age, items):
        candidates = [i for i in items if i.item_state.price and i.item_state.price.timestamp > max_age]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp), reverse=True)[:limit]
    def items_get_recently_updated(self, limit, max_age):
        return self._cache.query(lambda items: self._query__items_get_recently_updated(limit, max_age, items))
        
    def _query__items_get_most_valuable(self, limit, max_age, min_value, max_value, items):
        candidates = [i for i in items if i.item_state.price and i.item_state.price.timestamp > max_age and min_value <= i.item_state.price.value <= max_value]
        return sorted(candidates, key=(lambda i: i.item_state.price.value), reverse=True)[:limit]
    def items_get_most_valuable(self, limit, max_age, min_value, max_value):
        return self._cache.query(lambda items: self._query__items_get_most_valuable(limit, max_age, min_value, max_value, items))
        
    def _query__items_get_no_supply(self, limit, max_age, items):
        candidates = [i for i in items if i.item_state.price and i.item_state.price.timestamp > max_age and i.item_state.price.value == 0]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp), reverse=True)[:limit]
    def items_get_no_supply(self, limit, max_age):
        return self._cache.query(lambda items: self._query__items_get_no_supply(limit, max_age, items))
        
    def _query__items_get_stale(self, limit, min_age, max_age, items):
        candidates = [i for i in items if i.item_state.price and max_age < i.item_state.price.timestamp < min_age]
        return sorted(candidates, key=(lambda i: i.item_state.price.timestamp))[:limit]
    def items_get_stale(self, limit, min_age, max_age):
        return self._cache.query(lambda items: self._query__items_get_stale(limit, min_age, max_age, items))
        
    def _items_compute_average(self, item_id):
        #Computes the average price from -12h to -36h
        current_time = int(time.time())
        end_time = current_time - _TWELVE_HOURS
        start_time = end_time - _TWELVE_HOURS
        timeslices = collections.defaultdict(list)
        
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT prices.ts, prices.value
                FROM prices
                WHERE prices.item_id = %(item_id)s
                  AND prices.ts < %(end_ts)s
                  AND prices.ts > %(start_ts)s""", {
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
            self._cache.update(ItemRef(
                ItemState(self._cache.get_item_by_id(item_id), item_id, price),
                self._items_compute_average(item_id),
            ))
            
    def items_delete_price(self, item_id, timestamp, user_id=None):
        statement = [
            "DELETE "
            "FROM prices "
            "WHERE prices.item_id = %(item_id)s "
              "AND prices.ts = %(timestamp)s "
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
                if result is not None: #There's still data
                    (timestamp, value) = result
                    self._cache.update(ItemRef(
                        ItemState(self._cache.get_item_by_id(item_id), item_id, hq, ItemPrice(
                            _datetime_to_epoch(timestamp),
                            value, None, False,
                        )),
                        self._items_compute_average(item_id, hq),
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
            
    def flags_list(self, language):
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT flags.price_item_id, base_items.name_{language}, items.hq,
                    flags.price_ts, prices.value,
                    reporter.id, reporter.name, reporter.anonymous,
                    reportee.id, reportee.name, reportee.anonymous
                FROM flags, users AS reportee, users as reporter, prices, items, base_items
                WHERE flags.price_item_id = prices.item_id
                  AND flags.price_item_id = items.id
                  AND flags.price_ts = prices.ts
                  AND flags.reported_by = reporter.id
                  AND prices.submitting_user = reportee.id
                  AND base_items.id = items.base_item_id
                ORDER BY flags.price_ts ASC""".format(language=language))
            return map(
                lambda (
                    item_id, item_name, hq,
                    price_ts, item_value,
                    reporter_id, reporter_name, reporter_anonymous,
                    reportee_id, reportee_name, reportee_anonymous,
                ): Flag(
                    ItemState(
                        item_name,
                        item_id, hq,
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
        timestamp = _epoch_to_datetime(timestamp)
        
        with self._pool.get_cursor() as cursor:
            cursor.execute("""SELECT flags.reported_by, prices.submitting_user
                FROM flags, prices
                WHERE flags.price_item_id = %(item_id)s
                  AND flags.price_ts = %(timestamp)s
                LIMIT 1""", {
                    'item_id': item_id,
                    'timestamp': timestamp,
                })
            (reported_by, submitting_user) = cursor.fetchone()
            
            if delete: #Cascade will clean up the flag
                cursor.execute("""DELETE
                    FROM prices
                    WHERE prices.item_id = %(item_id)s
                      AND prices.ts = %(timestamp)s""", {
                    'item_id': item_id,
                    'timestamp': timestamp,
                })
            else:
                cursor.execute("""DELETE
                    FROM flags
                    WHERE flags.price_item_id = %(item_id)s
                      AND flags.price_ts = %(timestamp)s""", {
                    'item_id': item_id,
                    'timestamp': timestamp,
                })
                
            cursor.execute("""INSERT
                INTO flags_history (item_id, price_ts, submitting_user, reported_by, deleted)
                VALUES (%(item_id)s, %(timestamp)s, %(submitter)s, %(reporter)s, %(deleted)s)""", {
                'item_id': item_id,
                'timestamp': timestamp,
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
