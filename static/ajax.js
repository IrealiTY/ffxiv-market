function ffxivm_price_update(item_id, callback_id){
    var price = $.trim($('#pin-' + callback_id).val());
    $.ajax({
        url: '/items/ajax-price-update',
        type: 'POST',
        timeout: 5000,
        data: {
            item_id: item_id,
            value: price
        },
    })
    .done(function(result){
        $('#ts-' + callback_id).remove();
        $('#prc-' + callback_id).text(price);
    })
    .fail(function(result){
        alert("Unable to update " + item_id + "@" + price);
    })
    ;
    return false;
}

function ffxivm_price_delete(item_id, timestamp, callback_id){
    $.ajax({
        url: '/items/ajax-price-delete',
        type: 'POST',
        timeout: 5000,
        data: {
            item_id: item_id,
            timestamp: timestamp
        },
    })
    .done(function(result){
        if(result.deleted){
            $('#' + callback_id).remove();
        }else{
            var a_element = $('#' + callback_id + '-a');
            a_element.prop('onclick', null).off('click');
            a_element.on('click', function(){return false;});
            
            var img_element = $('#' + callback_id + '-img');
            img_element.prop('src', '/static/flagged.png');
            img_element.prop('title', 'flagged for review');
        }
    })
    .fail(function(result){
        alert("Unable to delete " + item_id + "@" + timestamp);
    })
    ;
    return false;
}

function ffxivm_watch(item_id){
    $.ajax({
        url: '/items/ajax-watch',
        type: 'POST',
        timeout: 5000,
        data: {
            item_id: item_id
        },
    })
    .done(function(result){
        var watch_element = $('#watch');
        watch_element.prop('onclick', null).off('click');
        watch_element.on('click', function(){return ffxivm_unwatch(item_id);});
        watch_element.prop('value', 'stop watching this item');
    })
    .fail(function(result){
        alert("Unable to watch " + item_id);
    })
    ;
    return false;
}

function ffxivm_unwatch(item_id){
    $.ajax({
        url: '/items/ajax-unwatch',
        type: 'POST',
        timeout: 5000,
        data: {
            item_id: item_id
        },
    })
    .done(function(result){
        var watch_element = $('#watch');
        watch_element.prop('onclick', null).off('click');
        watch_element.on('click', function(){return ffxivm_watch(item_id);});
        watch_element.prop('value', 'watch this item');
    })
    .fail(function(result){
        alert("Unable to unwatch " + item_id);
    })
    ;
    return false;
}

function ffxivm_flag_resolve(item_id, timestamp, remove, callback_id){
    $.ajax({
        url: '/flags/ajax-resolve',
        type: 'POST',
        timeout: 5000,
        data: {
            item_id: item_id,
            timestamp: timestamp,
            remove: remove
        },
    })
    .done(function(result){
        $('#' + callback_id).remove();
    })
    .fail(function(result){
        alert("Unable to resolve " + item_id + "@" + timestamp);
    })
    ;
    return false;
}
