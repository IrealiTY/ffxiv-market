import httplib
import logging
import os
import threading
import time
import urllib

import mako.template
import mako.lookup
import tornado.web

from ..common import (
    CONFIG,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
    USER_STATUS_NAMES,
)
from ..db import DATABASE

_logger = logging.getLogger('handlers._common')

class _MakoEngine(object):
    def __init__(self):
        if not os.path.isdir(CONFIG['server']['mako_modules_path']):
            os.makedirs(CONFIG['server']['mako_modules_path'])
        self._lookup = mako.lookup.TemplateLookup(
            directories=[CONFIG['server']['mako_templates_path']],
            module_directory=CONFIG['server']['mako_modules_path'],
        )
        
    def render_page(self, template, **kwargs):
        template = self._lookup.get_template(template)
        return template.render(DATABASE=DATABASE, **kwargs)
_MAKO_ENGINE = _MakoEngine()

_BAN_LOCK = threading.Lock()
_BAN_LIST = set()
def ADD_BAN(user_id): #Call any time a user is banned
    with _BAN_LOCK:
        _BAN_LIST.add(user_id)
def CLEAR_BAN(user_id): #Call any time a user's status changes to anything but banned, regardless of previous state
    with _BAN_LOCK:
        _BAN_LIST.discard(user_id)
def CHECK_BAN(user_id):
    with _BAN_LOCK:
        return user_id in _BAN_LIST
        
        
def restrict_active(context):
    if not context['role']['active']:
        raise tornado.web.HTTPError(403, reason="Access is restricted to active users")
        
def restrict_moderator(context):
    if not context['role']['moderator']:
        raise tornado.web.HTTPError(403, reason="Access is restricted to moderators")
        
def restrict_administrator(context):
    if not context['role']['administrator']:
        raise tornado.web.HTTPError(403, reason="Access is restricted to administrators")
        
class Handler(tornado.web.RequestHandler):
    def get_current_user(self):
        user_id = self.get_secure_cookie(CONFIG['cookie']['auth_identifier'])
        if user_id:
            user_id = int(user_id)
            if not CHECK_BAN(user_id):
                return user_id
        return None
        
    def _get_current_user_identity(self, user_id):
        identity = DATABASE.users_get_identity(self.get_current_user())
        if identity is None:
            return {
                'user_id': user_id,
                'user_name': 'guest',
                'status': USER_STATUS_GUEST,
                'anonymous': True,
            }
        return {
            'user_id': user_id,
            'user_name': identity[0],
            'status': identity[1],
            'anonymous': identity[2],
        }
        
    def _build_common_context(self, page_title=None, header_extra=None):
        identity = self._get_current_user_identity(self.get_current_user())
        server = CONFIG['server']
        
        moderator = identity['status'] in (USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,)
        
        return {
            'CONFIG': CONFIG,
            'page': {
                'title': page_title,
                'time_current': int(time.time()),
                'all_item_names': (),
                'header_extra': header_extra or [],
            },
            'identity': identity,
            'role': {
                'active': identity['status'] in (USER_STATUS_ACTIVE, USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,),
                'moderator': moderator,
                'administrator': identity['status'] in (USER_STATUS_ADMINISTRATOR,),
            },
            'notifications': {
                'flags': moderator and DATABASE.flags_count() or 0,
            },
        }
        
    def _refresh_auth_cookie(self, context):
        if context['identity']['user_id'] is not None:
            self.set_secure_cookie(
                CONFIG['cookie']['auth_identifier'], str(context['identity']['user_id']),
                expires_days=CONFIG['cookie']['longevity_days']
            )
            
    def _common_setup(self, page_title=None, header_extra=None, restrict=None):
        context = self._build_common_context(
            page_title=page_title,
            header_extra=header_extra,
        )
        
        self._refresh_auth_cookie(context)
        
        if restrict:
            restrict(context)
            
        if context['identity']['user_id'] is not None:
            context['page']['all_item_names'] = DATABASE.items_get_names()
            
        return context
        
    def _render(self, template, context):
        self.set_header('Content-Type', 'text/html')
        self.write(_MAKO_ENGINE.render_page(template, **context))
        
    def write_error(self, status_code, **kwargs):
        context = self._build_common_context(page_title='Error {code}'.format(code=status_code))
        reason = httplib.responses.get(status_code)
        exc = kwargs.get('exc_info')
        if exc:
            exc = exc[1]
            if isinstance(exc, tornado.web.HTTPError):
                reason = exc.reason
        context.update({
            'error_code': status_code,
            'reason': reason,
        })
        self._render('error.html', context)
        