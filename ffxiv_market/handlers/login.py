import logging
import re
import time
import uuid

import tornado.web

from _common import (
    CONFIG, DATABASE,
    Handler,
    restrict_active, restrict_moderator, restrict_administrator,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
)

_CHARACTER_NAME_MAX_LENGTH = 21
_CHARACTER_NAME_RE = re.compile(r"[A-Za-z][a-z']* [A-Za-z][a-z']*")
_PASSWORD_MIN_LENGTH = 4

_logger = logging.getLogger('handlers.login')

def _normalise_name(user_name):
    (first_name, last_name) = user_name.lower().split()
    return "{c1}{n1} {c2}{n2}".format(
        c1=first_name[0].upper(),
        n1=first_name[1:],
        c2=last_name[0].upper(),
        n2=last_name[1:],
    )
    
def _validate_credentials(handler):
    username = handler.get_argument("username", default='').strip()
    password = handler.get_argument("password", default='')
    if len(username) > _CHARACTER_NAME_MAX_LENGTH or not _CHARACTER_NAME_RE.match(username):
        raise tornado.web.HTTPError(422, reason="Character-name is invalid")
    if len(password) < _PASSWORD_MIN_LENGTH:
        raise tornado.web.HTTPError(422, reason="Password is too short")
    return (_normalise_name(username).encode('utf-8'), password.encode('utf-8'))

class LoginHandler(Handler):
    def get(self):
        context = self._build_common_context(page_title="Login")
        context.update({
            'next': self.get_argument("next", default="/"),
            'throwaway_password': uuid.uuid4().hex,
        })
        self._render('login.html', context)
        
    def post(self):
        (username, password) = _validate_credentials(self)
        
        user_id = DATABASE.users_login(username, password)
        if user_id is not None:
            if user_id == -1:
                raise tornado.web.HTTPError(403, reason="Account is banned")
                
            self.set_secure_cookie(
                CONFIG['cookie']['auth_identifier'], str(user_id),
                expires_days=CONFIG['cookie']['longevity_days'],
            )
            self.redirect(self.get_argument("next", default="/"))
            return
        time.sleep(1)
        raise tornado.web.HTTPError(403, reason="Unrecognised character-name/password")
        
class LogoutHandler(Handler):
    def get(self):
        self.clear_cookie(CONFIG['cookie']['auth_identifier'])
        self.redirect('/login')
        
class RegisterHandler(Handler):
    def get(self):
        self._render('register.html', self._build_common_context(page_title="Register account"))
        
    def post(self):
        (username, password) = _validate_credentials(self)
        
        try:
            DATABASE.users_create(username, password)
        except Exception, e:
            _logger.error(str(e))
            raise tornado.web.HTTPError(409, reason="Character-name already exists")
            
        self.redirect("/login/register")
        
class RecoverHandler(Handler):
    def get(self):
        self._render('recover.html', self._build_common_context(page_title="Recover password"))
        
    def post(self):
        (username, password) = _validate_credentials(self)
        
        if not DATABASE.users_set_recovery_password(username, password):
            raise tornado.web.HTTPError(403, reason="Unable to set recovery password: character is not registered")
            
        self.redirect("/login/recover")
        
class AboutHandler(Handler):
    def get(self):
        context = self._build_common_context(page_title="About")
        self._render('about.html', context)
        
