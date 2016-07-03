import logging

CONFIG = None #installed on startup

USER_STATUS_GUEST = -1 #People who aren't logged in
USER_STATUS_PENDING = 0
USER_STATUS_ACTIVE = 1
USER_STATUS_BANNED = 2
USER_STATUS_MODERATOR = 3
USER_STATUS_ADMINISTRATOR = 4
USER_STATUS_NAMES = {
    USER_STATUS_GUEST: 'guest',
    USER_STATUS_PENDING: 'pending approval',
    USER_STATUS_ACTIVE: 'active',
    USER_STATUS_BANNED: 'banned',
    USER_STATUS_MODERATOR: 'moderator',
    USER_STATUS_ADMINISTRATOR: 'administrator',
}

_logger = logging.getLogger('common')



