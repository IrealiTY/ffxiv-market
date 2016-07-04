import logging

import tornado.web

from _common import (
    CONFIG, DATABASE,
    Handler,
    restrict_active, restrict_moderator, restrict_administrator,
    ADD_BAN, CLEAR_BAN, CHECK_BAN,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
    USER_STATUS_NAMES,
)

_logger = logging.getLogger('handlers.users')

def _can_set_active(user_id, user_status, actor_id, actor_role):
    if user_status == USER_STATUS_ADMINISTRATOR:
        return False
        
    if actor_role['moderator']:
        if user_status in (USER_STATUS_PENDING, USER_STATUS_BANNED,):
            return True
        if user_id == actor_id:
            return True
        if actor_role['administrator']:
            return True
    return False

def _can_set_moderator(user_id, user_status, actor_id, actor_role):
    return actor_role['administrator']

def _can_set_banned(user_id, user_status, actor_id, actor_role):
    if actor_role['administrator']:
        return True
    if actor_role['moderator']:
        if user_status not in (USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,):
            return True
    return False
    
class ListHandler(Handler):
    @tornado.web.authenticated
    def get(self):
        context = self._common_setup(
            page_title="Users",
            restrict=restrict_moderator,
        )
        
        pending = []
        active = []
        moderators = []
        administrators = []
        banned = []
        for (user_id, user_name, user_status) in DATABASE.users_list():
            if user_status == USER_STATUS_PENDING:
                pending.append((user_id, user_name))
            elif user_status == USER_STATUS_ACTIVE:
                active.append((user_id, user_name))
            elif user_status == USER_STATUS_MODERATOR:
                moderators.append((user_id, user_name))
            elif user_status == USER_STATUS_ADMINISTRATOR:
                administrators.append((user_id, user_name))
            elif user_status == USER_STATUS_BANNED:
                banned.append((user_id, user_name))
                
        context.update({
            'users_pending': pending,
            'users_active': active,
            'users_moderator': moderators,
            'users_administrator': administrators,
            'users_banned': banned,
        })
        self._render('users.html', context)
        
class ModeratorsHandler(Handler):
    def get(self):
        context = self._common_setup(page_title="Moderators")
        
        moderators = [
            (user_id, user_name)
            for (user_id, user_name, _)
            in DATABASE.users_list(status=USER_STATUS_MODERATOR, order_most_recent=True)
        ]
        moderators.extend(
            (user_id, user_name)
            for (user_id, user_name, _)
            in DATABASE.users_list(status=USER_STATUS_ADMINISTRATOR, order_most_recent=True)
        )
        
        context['moderators'] = moderators
        self._render('moderators.html', context)
        
class ProfileHandler(Handler):
    @tornado.web.authenticated
    def get(self, user_id):
        user_id = int(user_id)
        
        context = self._common_setup()
        moderator = context['role']['moderator']
        if not moderator and context['identity']['user_id'] != user_id:
            raise tornado.web.HTTPError(403, reason="You do not have access to user profiles")
            
        profile = DATABASE.users_get_profile(user_id)
        if profile is None:
            raise tornado.web.HTTPError(404, reason="No user exists with id {id}".format(
                id=user_id,
            ))
            
        (profile,
         actions_received, actions_performed,
         prices_submitted, invalid_prices_submitted,
         unresolved_flags, valid_flags_reported, invalid_flags_reported,
        ) = profile
        
        context['rendering']['title'] = profile[0]
        context.update({
            'user_id': user_id,
            'user_name': profile[0],
            'user_anonymous': profile[1],
            'user_status': USER_STATUS_NAMES[profile[2]],
            'user_last_seen': profile[3],
            'user_prices_submitted': prices_submitted,
        })
        if moderator:
            context.update({
                'user_actions_received': actions_received,
                'user_actions_performed': actions_performed,
                'user_invalid_prices_submitted': invalid_prices_submitted,
                'user_unresolved_flags': unresolved_flags,
                'user_valid_flags_reported': valid_flags_reported,
                'user_invalid_flags_reported': invalid_flags_reported,
                'user_set_active': _can_set_active(user_id, profile[2], context['identity']['user_id'], context['role']),
                'user_set_moderator': _can_set_moderator(user_id, profile[2], context['identity']['user_id'], context['role']),
                'user_set_banned': _can_set_banned(user_id, profile[2], context['identity']['user_id'], context['role']),
                'user_candidate_password_timestamp': profile[4],
            })
        self._render('user.html', context)
        
class SetStatusHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        action = self.get_argument("action")
        reason = self.get_argument("reason").strip()
        user_id = int(self.get_argument("user_id"))
        if not reason:
            raise tornado.web.HTTPError(422, reason="No explanation provided for this action".format(
                offset=offset,
            ))
            
        context = self._build_common_context()
        
        subject_identity = DATABASE.users_get_identity(user_id)
        if subject_identity is None:
            raise tornado.web.HTTPError(404, reason="No user exists with id {id}".format(
                id=user_id,
            ))
            
        if action =='activated' and _can_set_active(user_id, subject_identity[1], context['identity']['user_id'], context['role']):
            status = USER_STATUS_ACTIVE
            CLEAR_BAN(user_id)
        elif action == 'promoted' and _can_set_moderator(user_id, subject_identity[1], context['identity']['user_id'], context['role']):
            status = USER_STATUS_MODERATOR
            CLEAR_BAN(user_id)
        elif action == 'banned' and _can_set_banned(user_id, subject_identity[1], context['identity']['user_id'], context['role']):
            status = USER_STATUS_BANNED
            ADD_BAN(user_id)
        else:
            raise tornado.web.HTTPError(422, reason="Unsupported action: {action}".format(
                action=action,
            ))
            
        DATABASE.users_set_status(user_id, status)
        DATABASE.interactions_record(user_id, context['identity']['user_id'], action, reason)
        self.redirect('/users/{user_id}'.format(
            user_id=user_id,
        ))
        
class AcceptRecoveryPasswordHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        reason = self.get_argument("reason").strip()
        user_id = int(self.get_argument("user_id"))
        if not reason:
            raise tornado.web.HTTPError(422, reason="No explanation provided for this action".format(
                offset=offset,
            ))
            
        context = self._build_common_context()
        restrict_moderator(context)
        DATABASE.users_accept_recovery_password(user_id)
        DATABASE.interactions_record(user_id, context['identity']['user_id'], 'recovered', reason)
        self.redirect('/users/{user_id}'.format(
            user_id=user_id,
        ))
        
class AnonymityUpdateHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        anonymous = self.get_argument("anonymity") == 'hide'
        
        context = self._build_common_context()
        user_id = context['identity']['user_id']
        DATABASE.users_set_anonymous(user_id, anonymous)
        self.redirect('/users/{user_id}'.format(
            user_id=user_id,
        ))
        