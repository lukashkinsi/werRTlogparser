const form = document.querySelector('#upload-form');
const input = document.querySelector('#log');
const dropzone = document.querySelector('#dropzone');
const message = document.querySelector('#message');

['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, event => {
  event.preventDefault();
  dropzone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(name => dropzone.addEventListener(name, event => {
  event.preventDefault();
  dropzone.classList.remove('drag');
}));
dropzone.addEventListener('drop', event => {
  input.files = event.dataTransfer.files;
  showSelectedFile();
});
input.addEventListener('change', showSelectedFile);

function showSelectedFile() {
  if (input.files[0]) dropzone.querySelector('strong').textContent = input.files[0].name;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  message.textContent = '';
  button.disabled = true;
  button.textContent = 'Анализируем…';
  try {
    const response = await fetch('/api/analyze', { method: 'POST', body: new FormData(form) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось обработать файл.');
    renderResult(data);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Проверить связь';
  }
});

function renderResult(data) {
  const summary = data.summary;
  document.querySelector('#results').hidden = false;
  document.querySelector('#result-title').textContent = summary.call_ids[0] ? `Звонок ${summary.call_ids[0]}` : data.filename;
  document.querySelector('#result-meta').textContent = `${formatDate(data.rows[0]?.timestamp)} · ${summary.samples} RTP-замеров · ${mediaNames(summary.media)}`;
  renderQuality(summary);
  renderMedia('audio', data.rows);
  renderMedia('video', data.rows);
  renderChart(data.rows.filter(row => row.media_kind === 'video'));
  renderDiagnostics(data.rows);
  document.querySelector('#sample-count').textContent = `${summary.samples} записей`;
  document.querySelector('#results').scrollIntoView({ behavior: 'smooth' });
}

function renderQuality(summary) {
  const badge = document.querySelector('#quality');
  const recommendation = document.querySelector('#recommendation');
  badge.className = `badge ${summary.score <= 2 ? 'bad' : summary.score === 3 ? 'warn' : ''}`;
  badge.textContent = `${summary.quality} · ${summary.score}/5`;
  recommendation.className = `rec ${summary.score <= 2 ? 'bad' : summary.score === 3 ? 'warn' : ''}`;
  const advice = summary.score <= 2
    ? 'Связь заметно деградирует. Проверьте канал, Wi-Fi и загрузку сети перед важным звонком.'
    : summary.score === 3
      ? 'Есть отклонения по сетевым метрикам. Снизьте движение в кадре и контролируйте качество в ходе звонка.'
      : 'Критических отклонений не найдено. Канал подходит для аудио- и видеосвязи.';
  recommendation.innerHTML = `<strong>${escapeHtml(summary.quality)} качество.</strong> ${advice}`;
}

function renderMedia(kind, rows) {
  const mediaRows = rows.filter(row => row.media_kind === kind);
  const card = document.querySelector(`#${kind}-card`);
  card.hidden = mediaRows.length === 0;
  if (!mediaRows.length) return;
  const inbound = mediaRows.filter(row => row.direction === 'inbound');
  const outbound = mediaRows.filter(row => row.direction === 'outbound');
  const definitions = [
    ['Потери пакетов', 'packet_loss_pct', '%', 2, 5],
    ['Джиттер', 'jitter_ms', 'мс', 30, 50],
    ['RTT', 'rtt_ms', 'мс', 250, 400],
    ['Битрейт', 'bitrate_kbps', 'кбит/с', null, null],
  ];
  document.querySelector(`#${kind}-metrics`).innerHTML = definitions.map(([label, key, unit, warn, bad]) => {
    return `<tr><td>${label}</td>${metricCell(average(outbound, key), unit, warn, bad)}${metricCell(average(inbound, key), unit, warn, bad)}</tr>`;
  }).join('');
}

function metricCell(value, unit, warn, bad) {
  const severity = value != null && bad != null && value >= bad ? 'flag-bad' : value != null && warn != null && value >= warn ? 'flag-warn' : '';
  return `<td class="${severity}">${value == null ? '—' : `${number(value)} ${unit}`}</td>`;
}

function renderChart(rows) {
  const svg = document.querySelector('#traffic-chart');
  svg.querySelectorAll('.chart-marker').forEach(marker => marker.remove());
  if (!rows.length) {
    document.querySelector('#traffic-line').setAttribute('points', '');
    return;
  }
  const values = rows.map(row => row.bitrate_kbps || 0);
  const max = Math.max(...values, 1);
  const points = values.map((value, index) => {
    const x = rows.length === 1 ? 300 : index * 600 / (rows.length - 1);
    return `${x.toFixed(1)},${(60 - value / max * 48).toFixed(1)}`;
  });
  document.querySelector('#traffic-line').setAttribute('points', points.join(' '));
  rows.forEach((row, index) => {
    if ((row.packet_loss_pct || 0) < 2) return;
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    const x = rows.length === 1 ? 300 : index * 600 / (rows.length - 1);
    marker.setAttribute('x', String(x - 1.5));
    marker.setAttribute('y', '54');
    marker.setAttribute('width', '3');
    marker.setAttribute('height', '13');
    marker.setAttribute('rx', '1.5');
    marker.setAttribute('class', 'chart-marker');
    svg.appendChild(marker);
  });
}

function renderDiagnostics(rows) {
  const diagnostics = [];
  const worst = (key) => rows.reduce((best, row) => row[key] != null && (best == null || row[key] > best) ? row[key] : best, null);
  const loss = worst('packet_loss_pct');
  const jitter = worst('jitter_ms');
  const rtt = worst('rtt_ms');
  diagnostics.push(['Потери', loss == null ? 'Нет данных о потерях пакетов' : `Максимальное значение — ${number(loss)} %`, loss >= 5 ? 'flag-bad' : loss >= 2 ? 'flag-warn' : '']);
  diagnostics.push(['Джиттер', jitter == null ? 'Нет данных о джиттере' : `Максимальное значение — ${number(jitter)} мс`, jitter >= 50 ? 'flag-bad' : jitter >= 30 ? 'flag-warn' : '']);
  diagnostics.push(['RTT', rtt == null ? 'Нет данных о задержке' : `Максимальное значение — ${number(rtt)} мс`, rtt >= 400 ? 'flag-bad' : rtt >= 250 ? 'flag-warn' : '']);
  const codecs = [...new Set(rows.map(row => row.codec).filter(Boolean))];
  diagnostics.push(['Кодеки', codecs.length ? codecs.join(', ') : 'Не указаны в логе', '']);
  document.querySelector('#diagnostics').innerHTML = diagnostics.map(([label, text, flag]) => `<tr><td>${label}</td><td class="${flag}">${escapeHtml(text)}</td></tr>`).join('');
}

function average(rows, key) {
  const values = rows.map(row => row[key]).filter(value => value != null);
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}
function number(value) { return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value); }
function formatDate(value) { if (!value) return 'Дата не указана'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU'); }
function mediaNames(media) { return Object.keys(media).map(name => name === 'audio' ? 'аудио' : name === 'video' ? 'видео' : name).join(' + '); }
function escapeHtml(value) { const element = document.createElement('div'); element.textContent = String(value); return element.innerHTML; }
