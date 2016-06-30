var month_names = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
];
var day_names = [
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"
];

function format_timestamp(timestamp){
    var date = new Date(timestamp * 1000);
    return day_names[date.getDay()] + ", " + month_names[date.getMonth()] + " " + date.getDate() + ", " + date.toLocaleTimeString();
}

$(document).ready(function(){
    $("[ffxivm_ts]").each(function(index, element){
        element.setAttribute("title", format_timestamp(parseInt(element.getAttribute("ffxivm_ts"))));
    });
    $("[ffxivm_ts_t]").each(function(index, element){
        element.textContent = format_timestamp(parseInt(element.getAttribute("ffxivm_ts_t")));
    });
})
