import collections
import json
import logging
import re

import tornado.web

from _common import (
    CONFIG, DATABASE,
    Handler,
    restrict_active, restrict_moderator, restrict_administrator,
    USER_STATUS_GUEST,
    USER_STATUS_PENDING, USER_STATUS_ACTIVE, USER_STATUS_BANNED,
    USER_STATUS_MODERATOR, USER_STATUS_ADMINISTRATOR,
)

import ffxiv_market.gamerescape as gamerescape

_ONE_MINUTE = 60
_ONE_HOUR = _ONE_MINUTE * 60
_ONE_DAY = _ONE_HOUR * 24
_ONE_WEEK = _ONE_DAY * 7
_ONE_MONTH = _ONE_WEEK * 4

_RE_ROMAN_NUMERALS = re.compile(r'^[ivxlc]+$')

_CRYSTAL_LIST = (
    #'Fire Shard', 'Ice Shard', 'Wind Shard', 'Earth Shard', 'Lightning Shard', 'Water Shard',
    2, 3, 4, 5, 6, 7,
    #'Fire Crystal', 'Ice Crystal', 'Wind Crystal', 'Earth Crystal', 'Lightning Crystal', 'Water Crystal',
    8, 9, 10, 11, 12, 13,
    #'Fire Cluster', 'Ice Cluster', 'Wind Cluster', 'Earth Cluster', 'Lightning Cluster', 'Water Cluster',
    14, 15, 16, 17, 18, 19,
)

_logger = logging.getLogger('handlers.items')

def _normalise_item_name(item_name, include_hq=True):
    item_tokens = item_name.strip().lower().split()
    
    hq = False
    if item_tokens[-1] == 'hq':
        hq = True
        item_tokens.pop()
        
    item_name = []
    for i in item_tokens:
        if _RE_ROMAN_NUMERALS.match(i):
            item_name.append(i.upper())
        else:
            item_name.append(i.title())
            
    if hq and include_hq:
        item_name.append('HQ')
        
    return ' '.join(item_name)

def _get_nq_name(item_name):
    if item_name.endswith(' HQ'):
        return item_name[:-3]
    return item_name

class ItemsHandler(Handler):
    @tornado.web.authenticated
    def get(self):
        context = self._common_setup(page_title="Items")
        
        context['rendering']['header_extra'] = [
            '<script src="/static/ajax.js"></script>',
        ]
        context.update({
            'crystal_list': _CRYSTAL_LIST,
        })
        self._render('items.html', context)

class ItemHandler(Handler):
    def _get_quality_counterpart(self, item_name):
        #Determine if there's an HQ/NQ counterpart
        nq_name = _get_nq_name(item_name)
        if nq_name == item_name:
            return DATABASE.items_get_latest_by_name(item_name + ' HQ')
        return DATABASE.items_get_latest_by_name(nq_name)
        
    def _normalise_data(self, price_data, current_time):
        data_points = CONFIG['graphing']['data_points']
        timescale = int(_ONE_DAY * CONFIG['graphing']['days'] / float(data_points))
        
        ages = collections.defaultdict(list)
        for price in price_data:
            age = int((current_time - price.timestamp) / timescale)
            if age >= data_points: #Data is too old to be relevant
                break
            ages[age].append(price.value)
            
        prices = []
        for (age, pricing) in sorted(ages.items()):
            prices.append((age, int((max(pricing) + min(pricing)) / 2)))
        return (prices, timescale)
        
    def _compute_maxmin(self, price_data, current_time):
        low_24h = low_week = low_month = None
        low_24h_value = low_week_value = low_month_value = 999999999
        high_24h = high_week = high_month = None
        high_24h_value = high_week_value = high_month_value = 0
        
        timestamp_cutoff_month = current_time - _ONE_MONTH
        timestamp_cutoff_week = current_time - _ONE_WEEK
        timestamp_cutoff_24h = current_time - _ONE_DAY
        
        for datum in price_data:
            datum_value = datum.value
            datum_timestamp = datum.timestamp
            if datum_timestamp >= timestamp_cutoff_month:
                if datum_timestamp >= timestamp_cutoff_week:
                    if datum_timestamp >= timestamp_cutoff_24h:
                        if datum_value < low_24h_value:
                            low_24h = datum
                            low_24h_value = datum_value
                        elif datum_value > high_24h_value:
                            high_24h = datum
                            high_24h_value = datum_value
                    if datum_value < low_week_value:
                        low_week = datum
                        low_week_value = datum_value
                    elif datum_value > high_week_value:
                        high_week = datum
                        high_week_value = datum_value
                if datum_value < low_month_value:
                    low_month = datum
                    low_month_value = datum_value
                elif datum_value > high_month_value:
                    high_month = datum
                    high_month_value = datum_value
            else:
                break
            
        return (
            low_24h, low_week, low_month,
            high_24h, high_week, high_month,
        )
        
    def _compute_timeblock_averages(self, normalised_data, normalised_data_timescale):
        slices_per_day = int((3600.0 * 24) / normalised_data_timescale)
        
        days = collections.defaultdict(list)
        for datum in normalised_data:
            days[int(datum[0] / slices_per_day)].append(datum[1])
        weeks = collections.defaultdict(list)
        for (day, prices) in days.iteritems():
            weeks[int(day / 7)].append(int(sum(prices) / len(prices)))
            
        return (days, weeks)
        
    def _compute_averages(self, timeblock_days, timeblock_weeks):
        return (
            0 in timeblock_days and int(sum(timeblock_days[0]) / len(timeblock_days[0])) or None,
            0 in timeblock_weeks and int(sum(timeblock_weeks[0]) / len(timeblock_weeks[0])) or None,
            int(sum(int(sum(prices) / len(prices)) for prices in timeblock_weeks.values()) / len(timeblock_weeks)),
        )
        
    def _compute_trends(self, normalised_data, timeblock_days, timeblock_weeks):
        if 0 in timeblock_weeks and 1 in timeblock_weeks:
            current_weekly_average = sum(timeblock_weeks[0]) / len(timeblock_weeks[0])
            previous_weekly_average = sum(timeblock_weeks[1]) / len(timeblock_weeks[1])
            trend_weekly = (current_weekly_average / float(previous_weekly_average)) - 1
        else:
            trend_weekly = None
            
        if 0 in timeblock_days and 1 in timeblock_days:
            current_daily_average = sum(timeblock_days[0]) / len(timeblock_days[0])
            previous_daily_average = sum(timeblock_days[1]) / len(timeblock_days[1])
            trend_daily = (current_daily_average / float(previous_daily_average)) - 1
        else:
            trend_daily = None
            
        if len(normalised_data) > 1 and normalised_data[0][0] == 0 and normalised_data[1][0] == 1:
            trend_current = (normalised_data[0][1] / float(normalised_data[1][1])) - 1        
        else:
            trend_current = None
            
        return (trend_current, trend_daily, trend_weekly)
        
    @tornado.web.authenticated
    def get(self, item_id):
        item_id = int(item_id)
        item_name = DATABASE.items_id_to_name(item_id)
        if item_name is None:
            raise tornado.web.HTTPError(42, reason='"{item_id}" is not a known item; submit a price to create it'.format(
                item_id=item_id,
            ))
            
        quality_counterpart = self._get_quality_counterpart(item_name)
        (crafted_from, crafts_into) = DATABASE.related_get(item_id)
        
        context = self._common_setup(
            page_title=item_name,
            header_extra=[
                '<script src="/static/ajax.js"></script>',
                '<script src="https://www.gstatic.com/charts/loader.js"></script>',
            ],
        )
        
        price_data = DATABASE.items_get_prices(item_id, max_age=(context['rendering']['time_current'] - (CONFIG['graphing']['days'] * _ONE_DAY)))
        
        #Defaults
        low_month = low_week = low_24h = None
        high_month = high_week = high_24h = None
        average_month = average_week = average_24h = None
        trend_weekly = trend_daily = trend_current = None
        
        (normalised_data, normalised_data_timescale) = self._normalise_data(price_data, context['rendering']['time_current'])
        if normalised_data:
            (   low_24h, low_week, low_month,
                high_24h, high_week, high_month,
            ) = self._compute_maxmin(price_data, context['rendering']['time_current'])
            
            (timeblock_days, timeblock_weeks) = self._compute_timeblock_averages(normalised_data, normalised_data_timescale)
            (average_24h, average_week, average_month) = self._compute_averages(timeblock_days, timeblock_weeks)
            (trend_current, trend_daily, trend_weekly) = self._compute_trends(normalised_data, timeblock_days, timeblock_weeks)
            
        if len(normalised_data) > 1:
            #Reverse the data and pad holes
            next_data_slice = normalised_data[-1][0]
            last_value = None
            new_normalised_data = []
            for i in xrange(167, -1, -1):
                if next_data_slice == i:
                    last_value = normalised_data.pop()[1]
                    if last_value == 0:
                        last_value = None
                    new_normalised_data.append(last_value)
                    if normalised_data:
                        next_data_slice = normalised_data[-1][0]
                    else:
                        new_normalised_data.extend(last_value for n in xrange(i))
                        break
                else:
                    new_normalised_data.append(last_value)
        else: #Not enough data to do time-based analysis
            new_normalised_data = None
        del normalised_data
        
        context.update({
            'item_name': item_name,
            'item_id': item_id,
            'item_db_url': gamerescape.build_url(item_name),
            'quality_counterpart': quality_counterpart,
            'crafted_from': crafted_from,
            'crafts_into': crafts_into,
            'price_data': price_data,
            'normalised_data': new_normalised_data,
            'normalised_data_timescale': normalised_data_timescale,
            'average_month': average_month,
            'average_week': average_week,
            'average_24h': average_24h,
            'low_month': low_month,
            'low_week': low_week,
            'low_24h': low_24h,
            'high_month': high_month,
            'high_week': high_week,
            'high_24h': high_24h,
            'trend_weekly': trend_weekly,
            'trend_daily': trend_daily,
            'trend_current': trend_current,
            'delete_lockout_time': 0, #Assume it's a moderator by default, to avoid resizing the table
            'watch_count': DATABASE.watchlist_count(context['identity']['user_id']),
            'watch_limit': CONFIG['lists']['item_watch']['limit'],
            'watching': DATABASE.watchlist_is_watching(context['identity']['user_id'], item_id),
        })
        if not context['role']['moderator']:
            context['delete_lockout_time'] = context['rendering']['time_current'] - CONFIG['data']['prices']['delete_window']
        self._render('item.html', context)
        
class PriceUpdateHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = self.get_argument("item_id", default=None)
        
        value = self.get_argument("value", default=None)
        if value:
            try:
                value = int(value.strip())
            except ValueError:
                raise tornado.web.HTTPError(422, reason="Invalid value: {value}".format(
                    value=value,
                ))
        else:
            value = None
            
        if item_id is None:
            name = _normalise_item_name(self.get_argument("name"))
            item_id = DATABASE.items_name_to_id(name)
            if item_id is None:
                item_id = DATABASE.items_create_item(name)
        else:
            item_id = int(item_id)
            
        context = self._build_common_context()
        if value is not None:
            DATABASE.items_add_price(item_id, value, context['identity']['user_id'])
            
        self.redirect("/items/{item_id}".format(
            item_id=item_id,
        ))
        
class PriceDeleteHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        timestamp = int(self.get_argument("timestamp"))
        
        context = self._build_common_context()
        if context['role']['moderator']:
            DATABASE.items_delete_price(item_id, timestamp)
        else:
            if context['rendering']['time_current'] - timestamp > CONFIG['data']['prices']['delete_window']:
                DATABASE.flags_create(item_id, timestamp, context['identity']['user_id'])
            else:
                DATABASE.items_delete_price(item_id, timestamp, context['identity']['user_id'])
                
        self.redirect("/items/{item_id}".format(
            item_id=item_id,
        ))
        
class RelatedItemsUpdateHandler(Handler):
    def _get_item_ids(self, items):
        for item in items:
            item_id = DATABASE.items_name_to_id(item)
            if item_id:
                yield item_id
            item_id = DATABASE.items_name_to_id(item + ' HQ')
            if item_id:
                yield item_id
                
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        item_name = DATABASE.items_id_to_name(item_id)
        if item_name is None:
            raise tornado.web.HTTPError(422, reason='"{item_id}" is not a known item; submit a price to create it'.format(
                item_id=item_id,
            ))
        
        context = self._build_common_context()
        restrict_moderator(context)
        
        (crafted_from, crafts_into) = gamerescape.parse_related(_get_nq_name(item_name))
        DATABASE.related_set(item_id,
            self._get_item_ids(crafted_from),
            self._get_item_ids(crafts_into),
        )
        self.redirect("/items/{item_id}".format(
            item_id=item_id,
        ))
        
class AjaxPriceUpdateHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        value = int(self.get_argument("value"))
        
        context = self._build_common_context()
        DATABASE.items_add_price(item_id, value, context['identity']['user_id'])
        self.write({})
        
class AjaxPriceDeleteHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        timestamp = int(self.get_argument("timestamp"))
        
        context = self._build_common_context()
        deleted = True
        if context['role']['moderator']:
            DATABASE.items_delete_price(item_id, timestamp)
        else:
            if context['rendering']['time_current'] - timestamp > CONFIG['data']['prices']['delete_window']:
                DATABASE.flags_create(item_id, timestamp, context['identity']['user_id'])
                deleted = False
            else:
                DATABASE.items_delete_price(item_id, timestamp, context['identity']['user_id'])
        self.write({'deleted': deleted})
        
class AjaxWatchHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        
        context = self._build_common_context()
        user_id = context['identity']['user_id']
        
        if DATABASE.watchlist_count(user_id) >= CONFIG['lists']['item_watch']['limit']:
            raise tornado.web.HTTPError(409, reason='You cannot watch any more items')
            
        DATABASE.watchlist_add(user_id, item_id)
        self.write({})
        
class AjaxUnwatchHandler(Handler):
    @tornado.web.authenticated
    def post(self):
        item_id = int(self.get_argument("item_id"))
        
        context = self._build_common_context()
        
        DATABASE.watchlist_remove(context['identity']['user_id'], item_id)
        self.write({})
        
class AjaxQueryNames(Handler):
    @tornado.web.authenticated
    def get(self):
        item_name = self.get_argument("term")
        self.write(json.dumps([
            {'label': item_ref.item_state.name, 'value': item_ref.item_state.id,}
            for item_ref in DATABASE.items_get_names(filter=item_name)
        ]))
        