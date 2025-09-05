async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function cell(v) {
  return `<td>${v === null || v === undefined ? "" : v}</td>`;
}

async function loadCurrent() {
  const data = await getJSON("/api/current");
  const cc = document.getElementById("currentContent");
  cc.innerHTML = `
    <div><span class="label">Temperature (dry bulb)</span><span class="val">${data.dry_bulb_F ?? ""} °F</span></div>
    <div><span class="label">Wet bulb</span><span class="val">${data.wet_bulb_F ?? ""} °F</span></div>
    <div><span class="label">Humidity</span><span class="val">${data.humidity_percent ?? ""} %</span></div>
    <div><span class="label">Pressure</span><span class="val">${data.pressure_inHg ?? ""} inHg</span></div>
  `;
  document.getElementById("stamp").textContent =
    data.timestamp_local ? `As of ${data.timestamp_local} (${data.station})` : "";
}

async function loadHistory() {
  const rows = await getJSON("/api/history");
  const tbody = document.getElementById("histBody");
  tbody.innerHTML = rows.map(r => `
    <tr>
      ${cell(r.timestamp_local)}
      ${cell(r.temperature_F)}
      ${cell(r.dry_bulb_F)}
      ${cell(r.wet_bulb_F)}
      ${cell(r.humidity_percent)}
      ${cell(r.pressure_inHg)}
    </tr>
  `).join("");
}

async function copyCSV() {
  const r = await fetch("/api/history.csv", { cache: "no-store" });
  const txt = await r.text();
  await navigator.clipboard.writeText(txt);
  const btn = document.getElementById("copyBtn");
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy 48h CSV to Clipboard"), 1200);
}

async function refreshAll() {
  await Promise.all([loadCurrent(), loadHistory()]);
}

document.getElementById("copyBtn").addEventListener("click", copyCSV);
document.getElementById("refreshBtn").addEventListener("click", refreshAll);

// Initial load + auto-refresh every 5 minutes
refreshAll();
setInterval(refreshAll, 5 * 60 * 1000);
