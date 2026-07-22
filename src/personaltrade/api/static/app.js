// Overview page only: live account snapshot over /ws/live, and the two
// kill-switch mutating actions. No framework, no build step (ADR-026).

function applySnapshot(snapshot) {
  document.getElementById("cash").textContent = snapshot.funds.cash;
  document.getElementById("equity").textContent = snapshot.funds.equity;

  const body = document.getElementById("positions-body");
  body.innerHTML = "";
  if (snapshot.positions.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="3">no open positions</td></tr>';
  } else {
    for (const p of snapshot.positions) {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${p.symbol}</td><td>${p.qty}</td><td>₹${p.avg_price}</td>`;
      body.appendChild(row);
    }
  }

  const status = document.getElementById("ks-status");
  status.textContent = snapshot.kill_switch.tripped ? "TRIPPED" : "clear";
  status.className = snapshot.kill_switch.tripped ? "danger-text" : "ok-text";
  document.getElementById("ks-reason").textContent = snapshot.kill_switch.reason || "";
  document.getElementById("ks-errors").textContent = snapshot.kill_switch.consecutive_errors;
}

function connectLiveFeed() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`);
  ws.onmessage = (event) => applySnapshot(JSON.parse(event.data));
  ws.onclose = () => setTimeout(connectLiveFeed, 3000);
}

async function submitForm(form, endpoint, confirmMessage) {
  if (!window.confirm(confirmMessage)) return;
  const response = await fetch(endpoint, { method: "POST", body: new FormData(form) });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    window.alert(`Failed: ${body.detail || response.statusText}`);
    return;
  }
  window.location.reload();
}

document.addEventListener("DOMContentLoaded", () => {
  connectLiveFeed();

  const tripForm = document.getElementById("trip-form");
  tripForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(tripForm, "/api/kill-switch/trip", "Trip the kill switch and halt trading?");
  });

  const resetForm = document.getElementById("reset-form");
  resetForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(resetForm, "/api/kill-switch/reset", "Reset the kill switch and resume trading?");
  });
});
