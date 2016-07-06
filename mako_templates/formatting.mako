<%!
    import time
%>

<%def name="format_timestamp_friendly(timestamp)"><%
age = int(rendering['time_current'] - timestamp)
qualifier = "ago"
if age < 0:
    qualifier = "from now"
    age *= -1

if age < 60:
    unit = "seconds"
elif age < 3600:
    age /= 60
    unit = "minutes"
elif age < 86400:
    age /= 3600
    unit = "hours"
else:
    age /= 86400
    unit = "days"

if age == 1:
    unit = unit[:-1]
%>${age} ${unit} ${qualifier}
</%def>

<%def name="render_timestamp(timestamp)">
<span class="timestamp" ffxivm_ts="${timestamp}">${format_timestamp_friendly(timestamp)}</span>
</%def>

<%def name="_render_item(item_ref, callback_id)">
    <a href="/items/${item_ref.item_state.id}">${getattr(item_ref.item_state.name, identity['language']) | h}${item_ref.item_state.hq and ' HQ' or ''}</a>
    %if item_ref.item_state.price:
        @ <span id="prc-${callback_id}">${item_ref.item_state.price.value and '{p:,}'.format(p=item_ref.item_state.price.value) or 'none'}</span>
        %if item_ref.item_state.price.value and item_ref.average and item_ref.item_state.price.timestamp > (rendering['time_current'] - 43200):
            <%
                price_delta = item_ref.item_state.price.value - item_ref.average
            %>
            %if price_delta < 0:
                <img src="/static/loss.png"/> ${price_delta * -1}</img>
            %elif price_delta > 0:
                <img src="/static/gain.png"/> ${price_delta}</img>
            %endif
        %endif
    %else:
        <span id="prc-${callback_id}"/>
    %endif
    <div ${"id=\"ts-{callback_id}\" onclick=\"$('#frm-{callback_id}').show('blind');\"".format(callback_id=callback_id)} style="display: inline;">
        %if item_ref.item_state.price:
            ${render_timestamp(item_ref.item_state.price.timestamp)}
        %else:
            <span class="nodata">no history</span>
        %endif
        <div style="display: none;" id="frm-${callback_id}">
            <form class="inl-up">
                <input type="number" id="pin-${callback_id}" min="0" max="999999999" size="9" autocomplete="off" required class="inl-up"/>
                gil
                <input type="submit" value="update" onclick="return ffxivm_price_update(${item_ref.item_state.id}, '${callback_id}');" class="inl-up"/>
            </form>
        </div>
    </div>
</%def>

<%def name="render_item_list(item_refs, callback_id_prefix)">
    <%
        callback_id = 0
    %>
    %if item_refs:
        <ul class="ffxiv-list">
            %for item_ref in item_refs:
                %if not item_ref:
                    <%continue%>
                %endif
                <%
                    callback_id += 1
                %>
                <li>${_render_item(item_ref, "{prefix}-{id}".format(prefix=callback_id_prefix, id=callback_id))}</li>
            %endfor
        </ul>
    %endif
    %if not callback_id:
        <span class="nodata">Nothing</span>
    %endif
</%def>
