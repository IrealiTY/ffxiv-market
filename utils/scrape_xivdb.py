import collections
import json
import sys
import time
import urllib2

NAME_COLUMN = 'name_en' #Change this to build databases for other languages

item_list = json.loads(
    urllib2.urlopen('https://api.xivdb.com/item?columns=id,{name_col},lodestone_id'.format(
        name_col=NAME_COLUMN,
    )).read(),
)

item_ids = set()
crafted_from = collections.defaultdict(list)
crafts_into = collections.defaultdict(list)

print("""CREATE RULE items_on_duplicate_ignore AS ON INSERT TO items
    WHERE EXISTS(SELECT 1 FROM items
                    WHERE (id)=(NEW.id)
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
    
    try:
        item_details = json.loads(
            urllib2.urlopen('https://api.xivdb.com/item/{item_id}'.format(
                item_id=item_id,
            )).read(),
        )
    except Exception, e:
        sys.stderr.write(str(e) + '\n')
    else:
        if item_details['is_untradable'] or item_details['special_shops_currency'] or (item_details['price_sell'] == 0 and item_details['item_search_category'] != 58):
            continue
            
        print("""INSERT INTO items VALUES ({item_id}, {can_be_hq}, $${name}$$, $${lodestone_id}$$);""".format(
            item_id=item_id,
            can_be_hq=item_details['can_be_hq'] and 'true' or 'false',
            name=item[NAME_COLUMN],
            lodestone_id=item['lodestone_id'],
        ))
        item_ids.add(item_id)
        
        if item_details['craftable']:
            for craftable in item_details['craftable']:
                crafted_from[item_id].append(craftable['id'])
                
        if item_details['recipes']:
            for recipe in item_details['recipes']:
                crafts_into[item_id].append(recipe['id'])
    finally:
        time.sleep(0.25)
        
print("DROP RULE items_on_duplicate_ignore ON items;")

print("DELETE FROM related_crafted_from;")
for (item_id, related) in crafted_from.iteritems():
    for i in item_ids.intersection(related):
        print("INSERT INTO related_crafted_from VALUES ({item_id}, {related_id});".format(
            item_id=item_id,
            related_id=i,
        ))
        
print("DELETE FROM related_crafts_into;")
for (item_id, related) in crafts_into.iteritems():
    for i in item_ids.intersection(related):
        print("INSERT INTO related_crafts_into VALUES ({item_id}, {related_id});".format(
            item_id=item_id,
            related_id=i,
        ))
        