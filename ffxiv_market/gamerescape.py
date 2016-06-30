import urllib2

import bs4

def build_url(common_item_name):
    return "http://ffxiv.gamerescape.com/wiki/{item}".format(
        item=common_item_name.replace(' ', '_'),
    )
    
def parse_related(common_item_name):
    data = urllib2.urlopen(build_url(common_item_name)).read()
    soup = bs4.BeautifulSoup(data)
    del data
    
    crafted_from = []
    item_box = soup.find('div', attrs={
        'class': 'itembox',
    })
    if item_box:
        for i in item_box.find_all('table', attrs={'style': 'width: 100%;'}):
            crafted_from.append(i.find_all('a')[1].text.strip())
    del item_box
    
    crafts_into = []
    item_list = soup.find_all('table', attrs={'class': 'datatable-GEtable sortable'})
    if item_list:
        skipped_first = False
        for i in item_list[-1].find_all('tr'):
            if skipped_first:
                crafts_into.append(i.find_all('a')[1].text.strip())
            else:
                skipped_first = True
                
    return (crafted_from, crafts_into)
    