#!/usr/bin/env python

import collections
import json
import sys
import time
import urllib2

NAME_COLUMN = 'name_en' #Change this to build databases for other languages

if len(sys.argv) >= 2:
    items_filter = [int(i.strip()) for i in open(sys.argv[1])]
    items_filter.sort()
    items_filter.reverse()
else:
    items_filter = None
    
item_list = json.loads(
    urllib2.urlopen('https://api.xivdb.com/item?columns=id,{name_col},lodestone_id'.format(
        name_col=NAME_COLUMN,
    )).read(),
)

item_ids = set()
crafted_from = collections.defaultdict(list)
crafts_into = collections.defaultdict(list)

print("""CREATE RULE base_items_on_duplicate_ignore AS ON INSERT TO base_items
    WHERE EXISTS(SELECT 1 FROM base_items
                    WHERE (id)=(NEW.id)
                )
    DO INSTEAD NOTHING;""")
print("""CREATE RULE items_on_duplicate_ignore AS ON INSERT TO items
    WHERE EXISTS(SELECT 1 FROM items
                    WHERE (base_item_id)=(NEW.base_item_id)
                      AND (hq)=(NEW.hq)
                )
    DO INSTEAD NOTHING;""")

item_count = len(item_list)
item_count_f = float(item_count)
for (i, item) in enumerate(sorted(item_list, key=(lambda i: i['id']))):
    sys.stderr.write("{i}/{item_count} ({progress:%}): {item}\n".format(
        i=(i + 1),
        item_count=item_count,
        progress=(i / item_count_f),
        item=item,
    ))
    item_id = item['id']
    
    if items_filter is not None:
        if item_id == items_filter[-1]:
            items_filter.pop()
        else:
            continue
            
    while True:
        try:
            item_details = json.loads(
                urllib2.urlopen('https://api.xivdb.com/item/{item_id}'.format(
                    item_id=item_id,
                ), timeout=2).read(),
            )
        except Exception, e:
            sys.stderr.write(str(e) + '\n')
        else:
            if item_details['is_untradable'] or item_details['special_shops_currency'] or (item_details['price_sell'] == 0 and item_details['item_search_category'] != 58):
                break
            
            print("INSERT INTO base_items (id, name, lodestone_id) VALUES ({item_id}, $${name}$$, $${lodestone_id}$$);".format(
                item_id=item_id,
                name=item[NAME_COLUMN],
                lodestone_id=item['lodestone_id'],
            ))
            print("INSERT INTO items (base_item_id, hq) VALUES ({item_id}, false);".format(
                item_id=item_id,
            ))
            if item_details['can_be_hq']:
                print("INSERT INTO items (base_item_id, hq) VALUES ({item_id}, true);".format(
                    item_id=item_id,
                ))
            item_ids.add(item_id)
            
            if item_details['craftable']:
                for craftable in item_details['craftable']:
                    for component in craftable['tree']:
                        crafted_from[item_id].append(component['id'])
                        
            if item_details['recipes']:
                for recipe in item_details['recipes']:
                    crafts_into[item_id].append(recipe['id'])
                    
            break
        finally:
            time.sleep(0.05)
            
print("DROP RULE base_items_on_duplicate_ignore ON base_items;")
print("DROP RULE items_on_duplicate_ignore ON items;")

print("DELETE FROM related_crafted_from;")
for (item_id, related) in sorted(crafted_from.iteritems()):
    for i in sorted(item_ids.intersection(related)):
        print("INSERT INTO related_crafted_from (item_id, related_item_id) VALUES ({item_id}, {related_id});".format(
            item_id=item_id,
            related_id=i,
        ))
        
print("DELETE FROM related_crafts_into;")
for (item_id, related) in sorted(crafts_into.items()):
    for i in sorted(item_ids.intersection(related)):
        print("INSERT INTO related_crafts_into (item_id, related_item_id) VALUES ({item_id}, {related_id});".format(
            item_id=item_id,
            related_id=i,
        ))
        
