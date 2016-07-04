$(document).ready(function() {
    var items = [${','.join('"{item}"'.format(item=i) for i in page['all_item_names'])}];
    $("#itemselect").autocomplete({
        source: items
    });
});