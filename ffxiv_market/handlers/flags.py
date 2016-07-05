import collections
import logging

import tornado.web

from _common import (
    CONFIG, DATABASE,
    Handler,
    restrict_active, restrict_moderator, restrict_administrator,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
)

_logger = logging.getLogger('handlers.items')

class FlagsHandler(Handler):
    @tornado.web.authenticated
    def get(self):
        context = self._common_setup(
            page_title="Flags",
            restrict=restrict_moderator,
        )
        
        context['flags'] = DATABASE.flags_list()
        self._render('flags.html', context, html_headers=(
            '<script src="/static/ajax.js"></script>',
        ))
        
class AjaxResolveHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        timestamp = int(self.get_argument("timestamp"))
        remove = self.get_argument("remove") == 'true'
        
        context = self._build_common_context()
        restrict_moderator(context)
        
        DATABASE.flags_resolve(item_id, timestamp, remove)
        self.write({})
        
