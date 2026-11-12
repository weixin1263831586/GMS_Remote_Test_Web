/* Local inventory filters, selection and pagination; writes remain explicit group saves. */
window.GmsDeviceInventory = (() => {
    const selected = new Set();
    let page = 1, size = 20, rows = [], filtered = [], filterSignature = '';
    const field = name => document.getElementById(`inventory-${name}`);
    const key = device => JSON.stringify([device.worker_id || workspaceLocalWorkerId(), mgmtDeviceKey(device)]);
    const groupsFor = device => (state.deviceGroups || []).filter(group =>
        (group.device_ids || []).map(String).includes(mgmtDeviceKey(device)));
    const filters = () => Object.fromEntries(['system', 'connection', 'runtime', 'occupancy', 'search']
        .map(name => [name, field(name)?.value || '']));

    function matches(device, query) {
        const presentation = GmsDevicePresentation;
        if (query.system && presentation.systemLabel(device) !== query.system) return false;
        if (query.connection && presentation.connectionType(device) !== query.connection) return false;
        if (query.runtime && presentation.runtimeState(device) !== query.runtime) return false;
        if (query.occupancy && presentation.occupancyState(device) !== query.occupancy) return false;
        const search = query.search.trim().toLocaleLowerCase();
        return !search || [device.name, device.model, device.serial_no, device.device_id,
            device.source_host, device.worker_id, device.host_display_name, device.soc_model,
            ...groupsFor(device).map(group => group.name)]
            .some(value => String(value || '').toLocaleLowerCase().includes(search));
    }

    function prepare(devices) {
        const query = filters();
        const signature = JSON.stringify([devicesManagementClusterMode, workspaceWorkerId(), state.groupFilter, query]);
        if (signature !== filterSignature) {
            page = 1;
            selected.clear();
            filterSignature = signature;
        }
        const known = new Set(allDevices.map(key));
        for (const value of selected) if (!known.has(value)) selected.delete(value);
        const systems = [...new Set(allDevices.map(GmsDevicePresentation.systemLabel))].sort();
        if (query.system && !systems.includes(query.system)) systems.push(query.system);
        const system = field('system');
        if (system) {
            system.replaceChildren(new Option('全部系统', ''), ...systems.map(value => new Option(value, value)));
            system.value = query.system;
        }
        filtered = devices.filter(device => matches(device, query));
        page = Math.min(page, Math.max(1, Math.ceil(filtered.length / size)));
        rows = filtered.slice((page - 1) * size, page * size);
        renderFooter();
        return rows;
    }

    function renderFooter() {
        const total = field('total');
        if (total) total.textContent = `共 ${allDevices.length} 台，已筛选 ${filtered.length} 台`;
        const pages = Math.max(1, Math.ceil(filtered.length / size));
        const pager = field('pages');
        if (!pager) return;
        const button = (number, label, disabled = false) =>
            `<button type="button" class="btn-xxs${number === page && /^\d+$/.test(label) ? ' active' : ''}"`
            + ` data-click="changeInventoryPage" data-a0="${number}"${disabled ? ' disabled' : ''}`
            + `${number === page && /^\d+$/.test(label) ? ' aria-current="page"' : ''}>${label}</button>`;
        const numbers = [...new Set([1, ...Array.from({length: 5}, (_, i) => page + i - 2), pages])]
            .filter(number => number >= 1 && number <= pages).sort((a, b) => a - b);
        const items = [];
        numbers.forEach((number, index) => {
            if (index && number - numbers[index - 1] > 1) items.push('<span aria-hidden="true">…</span>');
            items.push(button(number, String(number)));
        });
        pager.innerHTML = button(page - 1, '上一页', page <= 1) + items.join('')
            + button(page + 1, '下一页', page >= pages);
    }

    function syncSelection() {
        const checkboxes = [...document.querySelectorAll('#devices-table-body .inventory-device-select')];
        checkboxes.forEach(checkbox => {
            checkbox.checked = selected.has(checkbox.dataset.deviceKey);
            checkbox.closest('tr').classList.toggle('is-selected', checkbox.checked);
        });
        const keys = new Set(checkboxes.map(checkbox => checkbox.dataset.deviceKey));
        const count = [...keys].filter(value => selected.has(value)).length;
        const selectAll = field('select-page');
        if (selectAll) {
            selectAll.disabled = !keys.size;
            selectAll.checked = keys.size > 0 && count === keys.size;
            selectAll.indeterminate = count > 0 && count < keys.size;
        }
        if (field('selected-count')) field('selected-count').textContent = String(selected.size);
        for (const name of ['create-group', 'clear-selection']) {
            if (field(name)) field(name).disabled = !selected.size;
        }
    }

    function selectionCell(device) {
        return `<td class="inventory-select-cell"><input type="checkbox" class="inventory-device-select"`
            + ` data-device-key="${GmsUi.escape(key(device))}" aria-label="选择设备 ${GmsUi.escape(device.serial_no || device.device_id)}"`
            + ' data-change="selectInventoryDevice" data-r0="dataset.deviceKey" data-r1="checked"></td>';
    }

    function refresh() { displayDevicesManagement(filteredDevicesForView()); }
    window.filterDeviceInventory = () => { page = 1; selected.clear(); refresh(); };
    window.resetDeviceInventoryFilters = () => {
        for (const name of Object.keys(filters())) if (field(name)) field(name).value = '';
        filterDeviceInventory();
    };
    window.changeInventoryPage = number => {
        if (!Number.isInteger(number) || number < 1 || number > Math.max(1, Math.ceil(filtered.length / size))) return;
        page = number; refresh();
    };
    window.changeInventoryPageSize = value => {
        if (![20, 50, 100].includes(Number(value))) return;
        size = Number(value); page = 1; refresh();
    };
    window.selectInventoryDevice = (value, checked) => {
        if (!allDevices.some(device => key(device) === value)) return;
        if (checked) selected.add(value); else selected.delete(value);
        syncSelection();
    };
    window.selectInventoryPage = checked => {
        for (const checkbox of document.querySelectorAll('#devices-table-body .inventory-device-select')) {
            if (checked) selected.add(checkbox.dataset.deviceKey); else selected.delete(checkbox.dataset.deviceKey);
        }
        syncSelection();
    };
    window.clearInventorySelection = () => { selected.clear(); syncSelection(); };
    window.createInventoryGroup = () => {
        const ids = [...new Set(allDevices.filter(device => selected.has(key(device))).map(mgmtDeviceKey))];
        if (!ids.length) return;
        openCreateGroupModal();
        renderGroupDevicePicker(ids);
    };
    return Object.freeze({prepare, syncSelection, selectionCell, groupsFor});
})();
