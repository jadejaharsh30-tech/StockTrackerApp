/* ========================================
   PMS Review Dashboard V2 — fund_review.js
   ======================================== */

// --- STATE ---
let currentFund = 'gm';
let currentPeriod = 'weekly';
let dashboardStates = { wm: null, gm: null };
let entryRows = [];
let historyFilter = 'all';
let excelParsedData = null;
let trendCharts = {};
let mtdSortCol = 'rsStart'; // default sort column
let mtdSortDir = -1;        // -1 = descending

const WKS = ['Start', 'W1', 'W2', 'W3', 'W4', 'W5'];
const DEFAULT_STOCKS = {
    gm: ['RELIANCE', 'HDFCBANK', 'INFY', 'TCS', 'ICICIBANK', 'KOTAKBANK', 'AXISBANK', 'LT', 'BHARTIARTL', 'HCLTECH'],
    wm: ['RELIANCE', 'HDFCBANK', 'ITC', 'SUNPHARMA', 'MARUTI', 'BAJFINANCE', 'ASIANPAINT', 'TITAN', 'NESTLEIND', 'WIPRO']
};

// --- HELPERS ---
const today = () => new Date().toISOString().split('T')[0];
const fPct = v => (v === null || v === undefined || isNaN(v)) ? '—' : (v * 100).toFixed(2) + '%';
const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
const pctChg = (a, b) => { const fa = parseFloat(a), fb = parseFloat(b); if (isNaN(fa) || isNaN(fb) || fa === 0) return null; return (fb - fa) / Math.abs(fa); };
const closeModal = id => document.getElementById(id).classList.remove('visible');

function emptyRow(stock = '') {
    return { stock, alloc: '', rsStart: '', rsW1: '', rsW2: '', rsW3: '', rsW4: '', rsW5: '', pxStart: '', pxW1: '', pxW2: '', pxW3: '', pxW4: '', pxW5: '', st: 'above' };
}

// --- NIFTY FIELDS ---
const NIFTY_FIELDS = ['niftyRsStart', 'niftyRsW1', 'niftyRsW2', 'niftyRsW3', 'niftyRsW4', 'niftyRsW5',
    'niftyPxStart', 'niftyPxW1', 'niftyPxW2', 'niftyPxW3', 'niftyPxW4', 'niftyPxW5'];

function getNiftyData() {
    const d = {};
    NIFTY_FIELDS.forEach(f => d[f] = document.getElementById(f)?.value || '');
    return d;
}
function setNiftyData(d) {
    NIFTY_FIELDS.forEach(f => { const el = document.getElementById(f); if (el) el.value = d[f] || ''; });
}

// --- NAV ---
function switchFund(fund) {
    if (currentFund && fund !== currentFund) saveDraft(currentFund, collectEntryData());
    currentFund = fund;
    document.body.className = `fund-${fund}`;
    document.getElementById('btn-wm').className = 'fund-btn' + (fund === 'wm' ? ' active-wm' : '');
    document.getElementById('btn-gm').className = 'fund-btn' + (fund === 'gm' ? ' active-gm' : '');
    document.getElementById('entry-fund-label').textContent = fund === 'gm' ? 'Growth Mantra' : 'Wealth Mantra';
    document.getElementById('manage-fund-name').textContent = fund === 'gm' ? 'Growth Mantra' : 'Wealth Mantra';

    const draftStr = localStorage.getItem(`pms_draft_v2_${fund}`);
    if (draftStr) {
        const draft = JSON.parse(draftStr);
        entryRows = draft.rows || [];
        document.getElementById('review-date').value = draft.date || today();
        setNiftyData(draft);
    } else {
        const stocks = JSON.parse(localStorage.getItem(`pms_stocks_${fund}`)) || DEFAULT_STOCKS[fund];
        entryRows = stocks.map(s => emptyRow(s));
    }
    rebuildTableDOM();
    if (document.getElementById('dashboard-screen').classList.contains('visible')) renderDashboard();
}

function showScreen(name) {
    ['entry', 'dashboard', 'history', 'trends'].forEach(s => {
        document.getElementById(`${s}-screen`).classList.toggle('visible', s === name);
        document.getElementById(`tab-${s}`).classList.toggle('active', s === name);
    });
    if (name === 'history') fetchDbSnapshots();
    if (name === 'dashboard') renderDashboard();
    if (name === 'trends') loadTrends();
}

function saveDraft(fund, data) { localStorage.setItem(`pms_draft_v2_${fund}`, JSON.stringify(data)); }
function saveEntryDraft() { saveDraft(currentFund, collectEntryData()); alert('Draft saved locally.'); }
function togglePasteArea() { document.getElementById('paste-area').style.display = document.getElementById('paste-area').style.display === 'none' ? 'block' : 'none'; }

// --- TABLE DOM ---
function rebuildTableDOM() {
    const rsFields = ['rsStart', 'rsW1', 'rsW2', 'rsW3', 'rsW4', 'rsW5'];
    const pxFields = ['pxStart', 'pxW1', 'pxW2', 'pxW3', 'pxW4', 'pxW5'];
    document.getElementById('stock-tbody').innerHTML = entryRows.map((r, i) => `
        <tr>
            <td style="font-weight:600;color:#666;text-align:center">${i + 1}</td>
            <td><input type="text" value="${esc(r.stock)}" style="min-width:100px"/></td>
            <td><input type="number" value="${esc(r.alloc)}" step="0.01" placeholder="—" style="min-width:60px"/></td>
            ${rsFields.map((f, j) => `<td${j === 0 ? ' class="col-group-sep"' : ''}><input type="number" value="${esc(r[f])}" step="0.1" placeholder="—" style="min-width:55px"/></td>`).join('')}
            ${pxFields.map((f, j) => `<td${j === 0 ? ' class="col-group-sep"' : ''}><input type="number" value="${esc(r[f])}" step="0.01" placeholder="—" style="min-width:70px"/></td>`).join('')}
            <td><div class="st-toggle">
                <button class="st-toggle-btn above ${r.st !== 'below' ? 'active' : ''}" onclick="setST(this)">▲</button>
                <button class="st-toggle-btn below ${r.st === 'below' ? 'active' : ''}" onclick="setST(this)">▼</button>
            </div></td>
            <td><button class="btn-ghost" style="color:var(--danger-color);border:none;padding:4px" onclick="removeRow(${i})">✕</button></td>
        </tr>
    `).join('');
}

function setST(btn) { const p = btn.closest('.st-toggle'); p.querySelectorAll('.st-toggle-btn').forEach(b => b.classList.remove('active')); btn.classList.add('active'); }
function removeRow(i) { entryRows.splice(i, 1); rebuildTableDOM(); }
function addStockRow() { entryRows.push(emptyRow('')); rebuildTableDOM(); const tbody = document.getElementById('stock-tbody'); const lastTr = tbody.lastElementChild; if (lastTr) lastTr.querySelector('input').focus(); }
function clearAllRows() { if (confirm('Clear all rows?')) { entryRows = []; rebuildTableDOM(); } }

// --- EXPORT EXCEL ---
function exportExcel() {
    const data = collectEntryData();
    const headers = ['Stock', 'Alloc %', 'RS Start', 'RS W1', 'RS W2', 'RS W3', 'RS W4', 'RS W5', 'Px Start', 'Px W1', 'Px W2', 'Px W3', 'Px W4', 'Px W5', 'Supertrend'];
    const rows = data.rows.map(r => [r.stock, r.alloc, r.rsStart, r.rsW1, r.rsW2, r.rsW3, r.rsW4, r.rsW5, r.pxStart, r.pxW1, r.pxW2, r.pxW3, r.pxW4, r.pxW5, r.st]);
    const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, currentFund === 'gm' ? 'Growth Mantra' : 'Wealth Mantra');
    XLSX.writeFile(wb, `PMS_${currentFund.toUpperCase()}_${data.date || 'export'}.xlsx`);
}

function collectEntryData() {
    const rsF = ['rsStart', 'rsW1', 'rsW2', 'rsW3', 'rsW4', 'rsW5'];
    const pxF = ['pxStart', 'pxW1', 'pxW2', 'pxW3', 'pxW4', 'pxW5'];
    const rows = [];
    document.querySelectorAll('#stock-tbody tr').forEach(tr => {
        const inp = tr.querySelectorAll('input');
        const row = { stock: inp[0].value.trim(), alloc: inp[1].value, st: tr.querySelector('.st-toggle-btn.above.active') ? 'above' : 'below' };
        rsF.forEach((f, j) => row[f] = inp[2 + j].value);
        pxF.forEach((f, j) => row[f] = inp[8 + j].value);
        rows.push(row);
    });
    const d = { fund: currentFund, date: document.getElementById('review-date').value, rows, ...getNiftyData() };
    return d;
}

// --- PASTE ---
function parsePaste() {
    const lines = document.getElementById('paste-input').value.trim().split('\n');
    const parsed = [];
    lines.forEach(l => {
        const c = l.split('\t'); if (c.length < 2) return;
        let alloc = parseFloat(c[1]?.replace('%', '')); if (!isNaN(alloc) && alloc <= 1) alloc *= 100;
        const r = emptyRow(c[0]?.trim() || '');
        r.alloc = isNaN(alloc) ? '' : alloc;
        ['rsStart', 'rsW1', 'rsW2', 'rsW3', 'rsW4', 'rsW5'].forEach((f, j) => r[f] = c[2 + j]?.trim() || '');
        ['pxStart', 'pxW1', 'pxW2', 'pxW3', 'pxW4', 'pxW5'].forEach((f, j) => r[f] = c[8 + j]?.trim() || '');
        r.st = (c[14] || '').toLowerCase().includes('below') ? 'below' : 'above';
        parsed.push(r);
    });
    if (!parsed.length) return alert('No valid data found.');
    entryRows = parsed; rebuildTableDOM();
    document.getElementById('paste-area').style.display = 'none';
}

// --- EXCEL UPLOAD ---
function handleExcelUpload(e) {
    const file = e.target.files[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = function (ev) {
        try {
            const wb = XLSX.read(ev.target.result, { type: 'binary' });
            const ws = wb.Sheets[wb.SheetNames[0]];
            const json = XLSX.utils.sheet_to_json(ws, { header: 1 });
            if (json.length < 2) return alert('File appears empty.');
            const headers = json[0].map(String);
            excelParsedData = { headers, dataRows: json.slice(1) };
            showExcelColumnPicker(headers);
        } catch (err) { alert('Error reading file: ' + err.message); }
    };
    reader.readAsBinaryString(file);
    e.target.value = ''; // reset
}

function showExcelColumnPicker(headers) {
    const FIELD_MAP = ['stock', 'alloc', 'rsStart', 'rsW1', 'rsW2', 'rsW3', 'rsW4', 'rsW5', 'pxStart', 'pxW1', 'pxW2', 'pxW3', 'pxW4', 'pxW5', 'st'];
    const labels = ['Stock', 'Alloc %', 'RS Start', 'RS W1', 'RS W2', 'RS W3', 'RS W4', 'RS W5', 'Px Start', 'Px W1', 'Px W2', 'Px W3', 'Px W4', 'Px W5', 'Supertrend'];
    let html = '<table class="stock-table" style="font-size:13px"><thead><tr><th>Excel Column</th><th>Import?</th><th>Map To</th></tr></thead><tbody>';
    headers.forEach((h, i) => {
        const autoMap = i < FIELD_MAP.length ? FIELD_MAP[i] : '';
        const isStock = autoMap === 'stock';
        html += `<tr><td style="font-weight:600">${esc(h)}</td>
            <td><input type="checkbox" class="exc-check" data-idx="${i}" ${isStock ? 'checked disabled' : i < FIELD_MAP.length ? 'checked' : ''}></td>
            <td><select class="pms-input exc-map" data-idx="${i}" style="padding:4px;font-size:12px">
                <option value="">— Skip —</option>
                ${FIELD_MAP.map((f, j) => `<option value="${f}" ${f === autoMap ? 'selected' : ''}>${labels[j]}</option>`).join('')}
            </select></td></tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('excel-col-list').innerHTML = html;
    document.getElementById('excel-modal').classList.add('visible');
}

function importSelectedColumns() {
    if (!excelParsedData) return;
    const checks = document.querySelectorAll('.exc-check');
    const maps = document.querySelectorAll('.exc-map');
    const mapping = {}; // colIdx -> fieldName
    checks.forEach((cb, i) => { if (cb.checked) { const field = maps[i].value; if (field) mapping[parseInt(cb.dataset.idx)] = field; } });
    if (!mapping[0] && !Object.values(mapping).includes('stock')) { alert('Stock column must be mapped.'); return; }

    const rows = excelParsedData.dataRows.map(dr => {
        const r = emptyRow();
        Object.entries(mapping).forEach(([ci, field]) => {
            let val = dr[parseInt(ci)];
            if (val === undefined || val === null) val = '';
            if (field === 'alloc') { let v = parseFloat(String(val).replace('%', '')); if (!isNaN(v) && v <= 1) v *= 100; r.alloc = isNaN(v) ? '' : v; }
            else if (field === 'st') { r.st = String(val).toLowerCase().includes('below') ? 'below' : 'above'; }
            else { r[field] = String(val).trim(); }
        });
        return r;
    }).filter(r => r.stock);

    if (!rows.length) { alert('No valid rows found.'); return; }
    entryRows = rows; rebuildTableDOM();
    closeModal('excel-modal');
}

// --- PRICE FETCH ---
async function fetchPrices(field, dateInputId) {
    const dateVal = document.getElementById(dateInputId).value;
    if (!dateVal) return alert('Select a date first.');
    const symbols = [];
    document.querySelectorAll('#stock-tbody tr').forEach(tr => {
        const s = tr.querySelector('input').value.trim();
        if (s) symbols.push(s);
    });
    if (!symbols.length) return alert('No stocks in the table.');

    const fieldMap = { pxStart: 8, pxW1: 9, pxW2: 10, pxW3: 11, pxW4: 12, pxW5: 13 };
    const colIdx = fieldMap[field];

    try {
        const res = await fetch('/api/fund-review/fetch-prices', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbols, date: dateVal })
        });
        const data = await res.json();
        if (data.error) return alert('Error: ' + data.error);

        document.querySelectorAll('#stock-tbody tr').forEach(tr => {
            const sym = tr.querySelector('input').value.trim();
            const inputs = tr.querySelectorAll('input');
            if (data.prices[sym] !== undefined && inputs[colIdx]) {
                inputs[colIdx].value = data.prices[sym];
            }
        });
        if (data.errors?.length) alert('Could not fetch prices for: ' + data.errors.join(', '));
        else alert('Prices fetched successfully!');
    } catch (e) { alert('Network error while fetching prices.'); }
}

// --- MANAGE MODAL ---
function openManageModal() {
    // Use current entry table stocks, not localStorage defaults
    const stocks = entryRows.map(r => r.stock).filter(Boolean);
    if (!stocks.length) {
        const fallback = JSON.parse(localStorage.getItem(`pms_stocks_${currentFund}`)) || DEFAULT_STOCKS[currentFund];
        stocks.push(...fallback);
    }
    document.getElementById('manage-stock-list').innerHTML = stocks.map(s =>
        `<div style="display:flex;gap:8px;margin-bottom:8px"><input class="pms-input" value="${esc(s)}"><button class="btn-ghost" style="color:var(--danger-color);border:none" onclick="this.parentElement.remove()">✕</button></div>`
    ).join('');
    document.getElementById('manage-modal').classList.add('visible');
}
function addManagedStock() {
    document.getElementById('manage-stock-list').insertAdjacentHTML('beforeend',
        '<div style="display:flex;gap:8px;margin-bottom:8px"><input class="pms-input" placeholder="TICKER"><button class="btn-ghost" style="color:var(--danger-color);border:none" onclick="this.parentElement.remove()">✕</button></div>');
}
function saveManageModal() {
    const inputs = Array.from(document.querySelectorAll('#manage-stock-list input')).map(i => i.value.trim().toUpperCase()).filter(Boolean);
    localStorage.setItem(`pms_stocks_${currentFund}`, JSON.stringify(inputs));
    const existing = Object.fromEntries(entryRows.map(r => [r.stock.toUpperCase(), r]));
    entryRows = inputs.map(s => existing[s] || emptyRow(s));
    rebuildTableDOM();
    closeModal('manage-modal');
}

// --- GENERATE DASHBOARD ---
function generateDashboard() {
    const data = collectEntryData();
    if (!data.rows.filter(r => r.stock && r.alloc).length) return alert('Enter at least one stock with allocation.');
    dashboardStates[currentFund] = JSON.parse(JSON.stringify(data));
    saveDraft(currentFund, data);
    showScreen('dashboard');
}

function setPeriod(p) {
    currentPeriod = p;
    document.getElementById('btn-mtd').classList.toggle('active', p === 'mtd');
    document.getElementById('btn-weekly').classList.toggle('active', p === 'weekly');
    renderDashboard();
}

function showSubTab(id) {
    document.querySelectorAll('.sub-panel').forEach(el => el.classList.remove('visible'));
    document.getElementById(id).classList.add('visible');
    document.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
}

// --- DASHBOARD RENDERING ---
function getLatestWeek(row, prefix) {
    for (let i = 5; i >= 1; i--) { const v = row[`${prefix}W${i}`]; if (v !== '' && v !== undefined && v !== null && !isNaN(parseFloat(v))) return { week: i, val: parseFloat(v) }; }
    return null;
}

function renderDashboard() {
    const snap = dashboardStates[currentFund];
    if (!snap) { document.getElementById('dash-empty').style.display = 'block'; document.getElementById('dash-content').style.display = 'none'; return; }
    document.getElementById('dash-empty').style.display = 'none';
    document.getElementById('dash-content').style.display = 'block';

    const fund = snap.fund || currentFund;
    const isGM = fund === 'gm';
    document.getElementById('dash-fund-badge').textContent = isGM ? 'Growth Mantra' : 'Wealth Mantra';
    document.getElementById('dash-fund-badge').style.background = isGM ? 'var(--gm)' : 'var(--wm)';
    const d = snap.date ? snap.date.split('-') : [];
    document.getElementById('dash-date-label').textContent = d.length === 3 ? `Review: ${d[2]}-${d[1]}-${d[0]}` : 'Review: —';
    document.getElementById('dash-period-sub').textContent = currentPeriod === 'mtd' ? 'Month-to-Date' : 'Weekly';

    document.getElementById('panel-mtd').style.display = currentPeriod === 'mtd' ? 'block' : 'none';
    document.getElementById('panel-weekly').style.display = currentPeriod === 'weekly' ? 'block' : 'none';

    const rows = snap.rows.filter(r => r.stock && r.alloc);
    if (currentPeriod === 'mtd') renderMTD(rows, snap);
    else renderWeekly(rows, snap);
}

// --- MTD SORTING ---
function mtdSort(col) {
    if (mtdSortCol === col) mtdSortDir *= -1;
    else { mtdSortCol = col; mtdSortDir = -1; }
    renderDashboard();
}

// --- MTD RENDERING ---
function renderMTD(rows, snap) {
    const activeWeeks = [];
    for (let w = 1; w <= 5; w++) { if (rows.some(r => r[`rsW${w}`] !== '' && r[`rsW${w}`] !== undefined && !isNaN(parseFloat(r[`rsW${w}`])))) activeWeeks.push(w); }

    // Pre-calculate for sorting
    const computed = rows.map(r => {
        const alloc = parseFloat(r.alloc) / 100;
        const rsStart = parseFloat(r.rsStart);
        const latestPx = getLatestWeek(r, 'px');
        const pxStart = parseFloat(r.pxStart);
        const mtdRet = (latestPx && !isNaN(pxStart) && pxStart !== 0) ? (latestPx.val - pxStart) / Math.abs(pxStart) : null;
        const portRet = (mtdRet !== null && !isNaN(alloc)) ? mtdRet * alloc : null;
        return { ...r, rsStart, alloc, mtdRet, portRet };
    });

    // Sort
    computed.sort((a, b) => {
        let vA, vB;
        if (mtdSortCol === 'rsStart') { vA = a.rsStart; vB = b.rsStart; }
        else if (mtdSortCol === 'mtdRet') { vA = a.mtdRet || 0; vB = b.mtdRet || 0; }
        else if (mtdSortCol === 'portRet') { vA = a.portRet || 0; vB = b.portRet || 0; }
        else { const w = mtdSortCol.replace('rsW', ''); vA = parseFloat(a[`rsW${w}`]) || 0; vB = parseFloat(b[`rsW${w}`]) || 0; }
        if (isNaN(vA)) vA = 0; if (isNaN(vB)) vB = 0;
        return (vA - vB) * mtdSortDir;
    });

    const sortIcon = col => col === mtdSortCol ? (mtdSortDir > 0 ? ' ↑' : ' ↓') : '';
    const thStyle = 'text-align:center;cursor:pointer;user-select:none';

    let html = `<thead><tr><th style="text-align:left">Stock</th><th style="${thStyle}" onclick="mtdSort('rsStart')">RS Start${sortIcon('rsStart')}</th>`;
    activeWeeks.forEach(w => html += `<th style="${thStyle}" onclick="mtdSort('rsW${w}')">RS W${w}${sortIcon('rsW' + w)}</th>`);
    html += `<th style="${thStyle}" onclick="mtdSort('mtdRet')">MTD Return${sortIcon('mtdRet')}</th>`;
    html += `<th style="${thStyle}" onclick="mtdSort('portRet')">Portfolio Return${sortIcon('portRet')}</th></tr></thead><tbody>`;

    let totalPortRet = 0;
    computed.forEach(r => {
        if (r.portRet !== null) totalPortRet += r.portRet;
        html += `<tr><td style="font-weight:600">${esc(r.stock)}</td>`;
        html += `<td style="text-align:center">${isNaN(r.rsStart) ? '—' : r.rsStart.toFixed(1)}</td>`;
        activeWeeks.forEach(w => {
            const v = parseFloat(r[`rsW${w}`]);
            if (isNaN(v)) { html += '<td style="text-align:center">—</td>'; return; }
            const cls = (!isNaN(r.rsStart) && v >= r.rsStart) ? 'cell-green' : (!isNaN(r.rsStart) ? 'cell-red' : '');
            html += `<td class="${cls}" style="text-align:center">${v.toFixed(1)}</td>`;
        });
        html += `<td class="${r.mtdRet !== null ? (r.mtdRet >= 0 ? 'val-pos' : 'val-neg') : 'val-mono'}" style="text-align:center">${fPct(r.mtdRet)}</td>`;
        html += `<td class="${r.portRet !== null ? (r.portRet >= 0 ? 'val-pos' : 'val-neg') : 'val-mono'}" style="text-align:center">${fPct(r.portRet)}</td>`;
        html += '</tr>';
    });

    html += `<tr class="total-row"><td colspan="${2 + activeWeeks.length}" style="text-align:left">Total Portfolio Return</td>`;
    html += `<td></td><td class="${totalPortRet >= 0 ? 'val-pos' : 'val-neg'}" style="text-align:center">${fPct(totalPortRet)}</td></tr>`;

    const nPxStart = parseFloat(snap.niftyPxStart);
    let nLatest = null;
    for (let w = 5; w >= 1; w--) { const v = parseFloat(snap[`niftyPxW${w}`]); if (!isNaN(v)) { nLatest = v; break; } }
    const niftyRet = (nLatest !== null && !isNaN(nPxStart) && nPxStart !== 0) ? (nLatest - nPxStart) / Math.abs(nPxStart) : null;
    if (niftyRet !== null) {
        html += `<tr class="total-row benchmark-row"><td colspan="${2 + activeWeeks.length}" style="text-align:left">Nifty 500 Benchmark</td>`;
        html += `<td class="${niftyRet >= 0 ? 'val-pos' : 'val-neg'}" style="text-align:center">${fPct(niftyRet)}</td><td></td></tr>`;
    }
    html += '</tbody>';
    document.getElementById('mtd-table').innerHTML = html;
}

// --- WEEKLY RENDERING ---
function renderWeekly(rows, snap) {
    let niftyLatestRs = null;
    for (let w = 5; w >= 1; w--) { const v = parseFloat(snap[`niftyRsW${w}`]); if (!isNaN(v)) { niftyLatestRs = v; break; } }
    if (niftyLatestRs === null) niftyLatestRs = parseFloat(snap.niftyRsStart);

    const classified = rows.map(r => {
        const latest = getLatestWeek(r, 'rs');
        const latestRs = latest ? latest.val : parseFloat(r.rsStart);
        const isStrong = (niftyLatestRs !== null && !isNaN(latestRs)) ? latestRs > niftyLatestRs : null;
        const isAbove = r.st === 'above';
        return { ...r, latestRs, isStrong, isAbove };
    });

    const strong = classified.filter(c => c.isStrong === true);
    const weak = classified.filter(c => c.isStrong === false);
    const above = classified.filter(c => c.isAbove);
    const below = classified.filter(c => !c.isAbove);

    // --- Tab 1: Single table RS Review + Single table Supertrend ---
    const buildSplitTable = (titleL, dotL, itemsL, titleR, dotR, itemsR, showRs) => {
        const maxLen = Math.max(itemsL.length, itemsR.length, 1);
        const totalL = itemsL.reduce((s, r) => s + (parseFloat(r.alloc) || 0) / 100, 0);
        const totalR = itemsR.reduce((s, r) => s + (parseFloat(r.alloc) || 0) / 100, 0);
        let html = '<div class="stock-table-wrap" style="margin-bottom:16px"><table class="stock-table"><thead><tr>';
        html += `<th colspan="${showRs ? 3 : 2}" style="text-align:center;background:var(--green-dim);color:var(--green)"><span class="bif-dot ${dotL}" style="display:inline-block;margin-right:6px"></span>${titleL}</th>`;
        html += `<th colspan="${showRs ? 3 : 2}" style="text-align:center;background:var(--red-dim);color:var(--red);border-left:2px solid var(--border-light)"><span class="bif-dot ${dotR}" style="display:inline-block;margin-right:6px"></span>${titleR}</th>`;
        html += '</tr><tr>';
        html += '<th>Stock</th>';
        if (showRs) html += '<th style="text-align:center">RS</th>';
        html += '<th style="text-align:center">Alloc</th>';
        html += `<th style="border-left:2px solid var(--border-light)">Stock</th>`;
        if (showRs) html += '<th style="text-align:center">RS</th>';
        html += '<th style="text-align:center">Alloc</th>';
        html += '</tr></thead><tbody>';
        for (let i = 0; i < maxLen; i++) {
            const l = itemsL[i], r = itemsR[i];
            html += '<tr>';
            html += l ? `<td style="font-weight:500">${esc(l.stock)}</td>` : '<td></td>';
            if (showRs) html += l ? `<td style="text-align:center">${l.latestRs?.toFixed(1) || '—'}</td>` : '<td></td>';
            html += l ? `<td style="text-align:center">${fPct(parseFloat(l.alloc) / 100)}</td>` : '<td></td>';
            html += r ? `<td style="font-weight:500;border-left:2px solid var(--border-light)">${esc(r.stock)}</td>` : '<td style="border-left:2px solid var(--border-light)"></td>';
            if (showRs) html += r ? `<td style="text-align:center">${r.latestRs?.toFixed(1) || '—'}</td>` : '<td></td>';
            html += r ? `<td style="text-align:center">${fPct(parseFloat(r.alloc) / 100)}</td>` : '<td></td>';
            html += '</tr>';
        }
        html += `<tr class="total-row"><td>${itemsL.length} stocks</td>`;
        if (showRs) html += '<td></td>';
        html += `<td style="text-align:center">${fPct(totalL)}</td>`;
        html += `<td style="border-left:2px solid var(--border-light)">${itemsR.length} stocks</td>`;
        if (showRs) html += '<td></td>';
        html += `<td style="text-align:center">${fPct(totalR)}</td></tr>`;
        html += '</tbody></table></div>';
        return html;
    };

    document.getElementById('wk-rs-review').innerHTML =
        `<div class="card-title" style="margin-bottom:12px">RS Review <span style="font-weight:400;font-size:12px;color:var(--secondary-color)">(vs Nifty RS: ${niftyLatestRs?.toFixed(1) || 'N/A'})</span></div>` +
        buildSplitTable('Strong (RS > Nifty)', 'green', strong, 'Weak (RS ≤ Nifty)', 'red', weak, true);

    document.getElementById('wk-st-status').innerHTML =
        '<div class="card-title" style="margin-bottom:12px">Supertrend Status</div>' +
        buildSplitTable('Above Supertrend', 'green', above, 'Below Supertrend', 'red', below, false);

    // --- Tab 2: Outperformers / Underperformers / Neutral ---
    const outperf = classified.filter(c => c.isStrong === true && c.isAbove);
    const underperf = classified.filter(c => c.isStrong === false && !c.isAbove);
    const neutral = classified.filter(c => !(c.isStrong === true && c.isAbove) && !(c.isStrong === false && !c.isAbove));

    const bifCard = (title, dotCls, items) => {
        const totalAlloc = items.reduce((s, r) => s + (parseFloat(r.alloc) || 0) / 100, 0);
        return `<div class="bifurcation"><div class="bif-header"><div class="bif-dot ${dotCls}"></div>${title}</div><div class="bif-body">
            ${items.length ? items.map(r => `<div class="bif-item"><span>${esc(r.stock)}</span><span style="color:var(--secondary-color)">${fPct(parseFloat(r.alloc) / 100)}</span></div>`).join('') : '<div style="color:#999;font-size:12px;padding:8px 0">—</div>'}
            <div class="bif-total"><span>${items.length} stocks</span><span>${fPct(totalAlloc)}</span></div></div></div>`;
    };

    document.getElementById('wk-performers').innerHTML =
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">' +
        bifCard('Outperformers', 'green', outperf) +
        bifCard('Neutral', 'neutral', neutral) +
        bifCard('Underperformers', 'red', underperf) +
        '</div>';
}

// --- SAVE / LOAD SNAPSHOTS ---
async function saveSnapshotToDB() {
    const snap = dashboardStates[currentFund];
    if (!snap) return alert('Generate dashboard first.');
    try {
        const res = await fetch('/api/fund-review/snapshots', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fund: snap.fund, review_date: snap.date, period: currentPeriod, data_json: JSON.stringify(snap) })
        });
        const r = await res.json();
        if (res.ok) alert('Snapshot saved!'); else alert('Error: ' + r.error);
    } catch (e) { alert('Network error.'); }
}

function setHistoryFilter(f) {
    historyFilter = f;
    ['all', 'gm', 'wm'].forEach(id => document.getElementById(`hf-${id}`).classList.toggle('active', id === f));
    fetchDbSnapshots();
}

async function fetchDbSnapshots() {
    document.getElementById('db-history-list').innerHTML = '<div style="text-align:center;padding:40px"><div class="loading-spinner"></div> Loading...</div>';
    try {
        const res = await fetch(`/api/fund-review/snapshots?fund=${historyFilter}`);
        const data = await res.json();
        if (!data.length) { document.getElementById('db-history-list').innerHTML = '<div style="text-align:center;padding:40px;color:#666">No snapshots found.</div>'; return; }
        document.getElementById('db-history-list').innerHTML = data.map(s => {
            const isGM = s.fund === 'gm'; const color = isGM ? 'var(--gm)' : 'var(--wm)';
            const d = s.review_date.split('-'); const dd = d.length === 3 ? `${d[2]}-${d[1]}-${d[0]}` : s.review_date;
            return `<div class="history-item">
                <div class="history-date">${dd}</div>
                <div class="history-fund-badge" style="background:${color};color:#fff">${isGM ? 'GM' : 'WM'}</div>
                <div class="history-meta">Period: ${s.period} • ${new Date(s.created_at + 'Z').toLocaleString()}</div>
                <div class="history-actions">
                    <button class="btn-ghost" onclick='loadSnapshot(${JSON.stringify(s.data_json)})'>Load</button>
                    <button class="btn-ghost" style="color:var(--danger-color);border-color:var(--danger-color)" onclick="deleteSnapshot(${s.id})">Delete</button>
                </div></div>`;
        }).join('');
    } catch (e) { document.getElementById('db-history-list').innerHTML = '<div style="text-align:center;padding:40px;color:var(--danger-color)">Failed to load.</div>'; }
}

function loadSnapshot(jsonStr) {
    const d = JSON.parse(jsonStr); const fund = d.fund || 'gm';
    currentFund = fund; document.body.className = `fund-${fund}`;
    dashboardStates[fund] = d; entryRows = d.rows || [];
    document.getElementById('review-date').value = d.date || '';
    setNiftyData(d); rebuildTableDOM(); showScreen('dashboard');
}

async function deleteSnapshot(id) {
    if (!confirm('Delete this snapshot permanently?')) return;
    try { const r = await fetch(`/api/fund-review/snapshots/${id}`, { method: 'DELETE' }); if (r.ok) fetchDbSnapshots(); else alert('Error.'); }
    catch (e) { alert('Network error.'); }
}

// --- HISTORICAL TRENDS ---
async function loadTrends() {
    const fund = document.getElementById('trend-fund').value;
    try {
        const res = await fetch(`/api/fund-review/snapshots?fund=${fund}`);
        const snapshots = await res.json();
        if (snapshots.length < 2) {
            ['chart-return', 'chart-vs-nifty', 'chart-rs-strength', 'chart-st-health', 'chart-spotlight'].forEach(id => {
                const ctx = document.getElementById(id); if (ctx) { const c = Chart.getChart(ctx); if (c) c.destroy(); }
            });
            return;
        }
        // Parse and sort by date
        const parsed = snapshots.map(s => ({ date: s.review_date, ...JSON.parse(s.data_json) })).sort((a, b) => a.date.localeCompare(b.date));
        const labels = parsed.map(p => { const d = p.date.split('-'); return d.length === 3 ? `${d[2]}/${d[1]}` : `${p.date}`; });

        // 1. Fund Return Over Time
        const fundReturns = parsed.map(p => {
            const rows = (p.rows || []).filter(r => r.stock && r.alloc);
            let total = 0;
            rows.forEach(r => {
                const lp = getLatestWeek(r, 'px'); const ps = parseFloat(r.pxStart); const alloc = parseFloat(r.alloc) / 100;
                if (lp && !isNaN(ps) && ps !== 0 && !isNaN(alloc)) total += (lp.val - ps) / Math.abs(ps) * alloc;
            });
            return total * 100;
        });
        mkChart('chart-return', 'line', labels, [{ label: 'Fund MTD Return %', data: fundReturns, borderColor: 'var(--accent)', tension: .3, fill: false }]);

        // 2. Fund vs Nifty
        const niftyReturns = parsed.map(p => {
            const ps = parseFloat(p.niftyPxStart); let latest = null;
            for (let w = 5; w >= 1; w--) { const v = parseFloat(p[`niftyPxW${w}`]); if (!isNaN(v)) { latest = v; break; } }
            return (latest !== null && !isNaN(ps) && ps !== 0) ? (latest - ps) / Math.abs(ps) * 100 : null;
        });
        mkChart('chart-vs-nifty', 'line', labels, [
            { label: 'Fund %', data: fundReturns, borderColor: 'var(--accent)', tension: .3, fill: false },
            { label: 'Nifty 500 %', data: niftyReturns, borderColor: '#888', borderDash: [5, 5], tension: .3, fill: false }
        ]);

        // 3. RS Strength Trend
        const strongPct = [], weakPct = [];
        parsed.forEach(p => {
            const rows = (p.rows || []).filter(r => r.stock && r.alloc);
            let nRs = null; for (let w = 5; w >= 1; w--) { const v = parseFloat(p[`niftyRsW${w}`]); if (!isNaN(v)) { nRs = v; break; } }
            if (nRs === null) nRs = parseFloat(p.niftyRsStart);
            let sA = 0, wA = 0;
            rows.forEach(r => {
                const lr = getLatestWeek(r, 'rs'); const v = lr ? lr.val : parseFloat(r.rsStart); const a = (parseFloat(r.alloc) || 0) / 100;
                if (!isNaN(v) && nRs !== null) { if (v > nRs) sA += a; else wA += a; }
            });
            strongPct.push(sA * 100); weakPct.push(wA * 100);
        });
        mkChart('chart-rs-strength', 'bar', labels, [
            { label: 'Strong %', data: strongPct, backgroundColor: 'rgba(0,200,150,.5)' },
            { label: 'Weak %', data: weakPct, backgroundColor: 'rgba(255,77,109,.5)' }
        ], { scales: { x: { stacked: true }, y: { stacked: true, max: 100 } } });

        // 4. Supertrend Health
        const abvPct = [], blwPct = [];
        parsed.forEach(p => {
            const rows = (p.rows || []).filter(r => r.stock && r.alloc); let aA = 0, bA = 0;
            rows.forEach(r => { const a = (parseFloat(r.alloc) || 0) / 100; if (r.st === 'above') aA += a; else bA += a; });
            abvPct.push(aA * 100); blwPct.push(bA * 100);
        });
        mkChart('chart-st-health', 'bar', labels, [
            { label: 'Above %', data: abvPct, backgroundColor: 'rgba(0,200,150,.5)' },
            { label: 'Below %', data: blwPct, backgroundColor: 'rgba(255,77,109,.5)' }
        ], { scales: { x: { stacked: true }, y: { stacked: true, max: 100 } } });

        // 5. Stock Spotlight
        const allStocks = [...new Set(parsed.flatMap(p => (p.rows || []).map(r => r.stock).filter(Boolean)))];
        const sel = document.getElementById('spotlight-stock');
        sel.innerHTML = allStocks.map(s => `<option value="${s}">${s}</option>`).join('');
        renderSpotlight(parsed, labels);

    } catch (e) { console.error('Trends load error', e); }
}

function renderSpotlight(parsed, labels) {
    if (!parsed) return; // will be called from loadTrends
    window._trendParsed = parsed; window._trendLabels = labels;
    const stock = document.getElementById('spotlight-stock').value;
    if (!stock) return;
    const rsData = parsed.map(p => {
        const r = (p.rows || []).find(r => r.stock === stock); if (!r) return null;
        const lr = getLatestWeek(r, 'rs'); return lr ? lr.val : parseFloat(r.rsStart) || null;
    });
    mkChart('chart-spotlight', 'line', labels, [{ label: `${stock} RS Rating`, data: rsData, borderColor: 'var(--accent)', tension: .3, fill: false }]);
}
// Re-render spotlight when dropdown changes
document.getElementById('spotlight-stock')?.addEventListener('change', () => renderSpotlight(window._trendParsed, window._trendLabels));

function mkChart(id, type, labels, datasets, extraOpts) {
    const ctx = document.getElementById(id);
    if (!ctx) return;
    const existing = Chart.getChart(ctx);
    if (existing) existing.destroy();
    new Chart(ctx, { type, data: { labels, datasets }, options: { responsive: true, plugins: { legend: { position: 'bottom' } }, ...extraOpts } });
}

// --- INIT ---
document.getElementById('review-date').value = today();
switchFund('gm');
