/* GSTR-2B vs Tally - front end */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toLocaleString('en-IN');
  const rupee = (n) =>
    '₹' + Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });

  let RUN = null, TAB = null, PAGE = 1, TOTAL = 0;
  const SIZE = 100;

  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ------------------------------------------------------------- theme */
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('themeBtn').onclick = () => {
    const cur = document.documentElement.dataset.theme;
    const dark = cur ? cur === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('theme', next);
  };

  /* -------------------------------------------------- the two file slots */
  const SIDES = [
    { key: 'gst', card: 'dropGst', input: 'gstUpload',
      name: 'gstName', pick: 'gstExisting',
      empty: 'Drop the GSTR-2B file here' },
    { key: 'tally', card: 'dropTally', input: 'tallyUpload',
      name: 'tallyName', pick: 'tallyExisting',
      empty: 'Drop the Tally file here' },
  ];

  function chosen(s) {
    const f = $(s.input).files[0];
    if (f) return f.name;
    const sel = $(s.pick);
    return sel.value ? sel.options[sel.selectedIndex].text : '';
  }

  function refresh() {
    let ready = true;
    SIDES.forEach((s) => {
      const label = chosen(s);
      $(s.name).textContent = label || s.empty;
      $(s.card).classList.toggle('filled', !!label);
      if (!label) ready = false;
    });
    $('runBtn').disabled = !ready;
  }

  /* The dropdowns offer sheets uploaded during this session, so the same
     file can be re-used on the other side or re-run without uploading it
     twice. Nothing on the server's disk is ever listed. */
  async function refreshUploads() {
    let list = [];
    try {
      list = (await (await fetch('/api/uploads')).json()).uploads || [];
    } catch (e) { /* offer nothing rather than break the page */ }

    SIDES.forEach((s) => {
      const sel = $(s.pick), keep = sel.value;
      sel.innerHTML = '<option value="">'
        + (list.length ? '— or re-use an uploaded sheet —'
                       : '— no uploads yet —') + '</option>'
        + list.map((u) =>
            `<option value="${u.id}">${esc(u.name)} · ${kb(u.size)}</option>`)
          .join('');
      if (keep && list.some((u) => u.id === keep)) sel.value = keep;
    });
  }

  const kb = (n) => n > 1048576
    ? (n / 1048576).toFixed(1) + ' MB'
    : Math.max(1, Math.round(n / 1024)) + ' KB';

  SIDES.forEach((s) => {
    const card = $(s.card);

    $(s.input).onchange = () => {
      if ($(s.input).files[0]) $(s.pick).value = '';
      refresh();
    };
    $(s.pick).onchange = () => {
      if ($(s.pick).value) {
        $(s.input).value = '';
      }
      refresh();
    };

    ['dragenter', 'dragover'].forEach((e) =>
      card.addEventListener(e, (ev) => {
        ev.preventDefault();
        card.classList.add('over');
      }));
    ['dragleave', 'drop'].forEach((e) =>
      card.addEventListener(e, (ev) => {
        ev.preventDefault();
        card.classList.remove('over');
      }));
    card.addEventListener('drop', (ev) => {
      const f = ev.dataTransfer.files[0];
      if (!f) return;
      const dt = new DataTransfer();
      dt.items.add(f);
      $(s.input).files = dt.files;
      $(s.pick).value = '';
      refresh();
    });
  });
  refresh();
  refreshUploads();

  /* ---------------------------------------------------------------- run */
  $('runBtn').onclick = async () => {
    const fd = new FormData();
    fd.append('gst_sheet', $('gstSheet').value.trim());
    fd.append('tally_sheet', $('tallySheet').value.trim());

    SIDES.forEach((s) => {
      const f = $(s.input).files[0];
      if (f) fd.append(s.key + '_file', f);
      else fd.append(s.key + '_uploaded', $(s.pick).value);
    });

    $('errorBox').classList.add('hidden');
    $('results').classList.add('hidden');
    $('progress').classList.remove('hidden');
    $('runBtn').disabled = true;

    try {
      const res = await fetch('/api/run', { method: 'POST', body: fd });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      poll(j.job);
    } catch (err) {
      fail(err.message);
    }
  };

  function fail(msg) {
    $('progress').classList.add('hidden');
    $('errorBox').textContent = msg;
    $('errorBox').classList.remove('hidden');
    refresh();
  }

  async function poll(job) {
    try {
      const j = await (await fetch('/api/job/' + job)).json();
      if (j.state === 'running') {
        $('progStep').textContent = j.step || 'Working…';
        return setTimeout(() => poll(job), 400);
      }
      if (j.state === 'error') return fail(j.error);
      $('progress').classList.add('hidden');
      await refreshUploads();
      refresh();
      RUN = j.run;
      $('fileTag').textContent = j.gst_label + '  ↔  ' + j.tally_label;
      $('fileTag').classList.remove('hidden');
      render(j.summary);
    } catch (e) { fail(e.message); }
  }

  /* ------------------------------------------------------------- render */
  function render(s) {
    $('kMatched').textContent = fmt(s.matched);
    $('kDiffers').textContent = fmt(s.differs);
    $('kOnly2b').textContent = fmt(s.only_2b);
    $('kOnly2bTax').textContent = rupee(s.tax_only_2b) + ' ITC not booked';
    $('kOnlyTally').textContent = fmt(s.only_tally);
    $('kOnlyTallyTax').textContent = rupee(s.tax_only_tally) + ' at risk';
    $('kRows').textContent = fmt(s.gst_rows + s.tally_rows);
    $('kRowsSub').textContent =
      fmt(s.gst_rows) + ' GSTR-2B · ' + fmt(s.tally_rows) + ' Tally';

    const chips = [];
    s.gstin_notes.forEach(([k, v]) => {
      const cls = k.includes('DIFFERENT PAN') ? 'bad' : 'warn';
      chips.push([cls, k, fmt(v)]);
    });
    s.flags.slice(0, 6).forEach(([k, v]) => chips.push(['', k, fmt(v)]));
    $('chips').innerHTML = chips.map(([c, k, v]) =>
      `<span class="chip ${c}">${k} <b>${v}</b></span>`).join('');

    $('xlsxBtn').href = `/api/download/${RUN}/report.xlsx`;

    $('tabs').innerHTML = s.tables.map((t, i) =>
      `<button data-id="${t.id}" class="${i === 0 ? 'on' : ''}">${t.label}` +
      `<span class="n">${fmt(t.rows)}</span></button>`).join('');
    [...$('tabs').children].forEach((b) => {
      b.onclick = () => {
        [...$('tabs').children].forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        TAB = b.dataset.id; PAGE = 1; $('q').value = ''; load();
      };
    });

    TAB = s.tables[0].id; PAGE = 1;
    $('results').classList.remove('hidden');
    load();
  }

  /* -------------------------------------------------------------- table */
  let timer;
  $('q').oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { PAGE = 1; load(); }, 250);
  };
  $('prev').onclick = () => { if (PAGE > 1) { PAGE--; load(); } };
  $('next').onclick = () => {
    if (PAGE * SIZE < TOTAL) { PAGE++; load(); }
  };

  const NUMERIC =
    /(value|taxable|tax|amount|igst|cgst|sgst|cess|diff|rows|count|days|row)/i;

  function cellHtml(col, v) {
    const t = v === null || v === undefined ? '' : String(v);
    if (!t) return '<td></td>';

    if (col === 'Status' || col === 'Priority' || col === 'Confidence'
        || col === 'Issue') {
      let cls = 'mute';
      if (/^MATCHED$/.test(t)) cls = 'ok';
      else if (/MISMATCH|^P1$|^LOW/.test(t)) cls = 'bad';
      else if (/^P2$|DIFF|MISSING/.test(t)) cls = 'warn';
      else if (/IGNORE|^P3$/.test(t)) cls = 'mute';
      else if (/^HIGH$/.test(t)) cls = 'ok';
      else if (/^MEDIUM$/.test(t)) cls = 'warn';
      return `<td><span class="pill ${cls}">${esc(t)}</span></td>`;
    }

    if (NUMERIC.test(col) && !isNaN(Number(t.replace(/,/g, '')))) {
      const n = Number(t.replace(/,/g, ''));
      const show = Number.isInteger(n) ? fmt(n)
        : n.toLocaleString('en-IN', { minimumFractionDigits: 2,
                                      maximumFractionDigits: 2 });
      return `<td class="num${n < 0 ? ' neg' : ''}">${show}</td>`;
    }
    return `<td title="${esc(t)}">${esc(t)}</td>`;
  }

  async function load() {
    if (!RUN || !TAB) return;
    const q = encodeURIComponent($('q').value.trim());
    const url = `/api/table/${RUN}/${TAB}?q=${q}&page=${PAGE}&size=${SIZE}`;
    const d = await (await fetch(url)).json();
    TOTAL = d.total;

    $('csvBtn').href = `/api/download/${RUN}/${TAB}.csv?q=${q}`;
    $('count').textContent = fmt(d.total) + ' rows';

    const head = $('grid').tHead, body = $('grid').tBodies[0];
    head.innerHTML = '<tr>' + d.columns.map((c) =>
      `<th>${esc(c)}</th>`).join('') + '</tr>';

    if (!d.total) {
      body.innerHTML = `<tr><td class="empty" colspan="${d.columns.length || 1}">`
        + 'Nothing here — which is good news.</td></tr>';
    } else {
      body.innerHTML = d.rows.map((r) => '<tr>' +
        r.map((v, i) => cellHtml(d.columns[i], v)).join('') + '</tr>').join('');
    }

    const pages = Math.max(1, Math.ceil(d.total / SIZE));
    $('pageInfo').textContent = `Page ${d.page} of ${fmt(pages)}`;
    $('prev').disabled = d.page <= 1;
    $('next').disabled = d.page >= pages;
    $('grid').parentElement.scrollTop = 0;
  }
})();
