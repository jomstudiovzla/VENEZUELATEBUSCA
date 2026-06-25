/**
 * Cliente mínimo WebSocket — Venezuela te Busca
 * Conecta a /ws/missing_updates y pinta tarjetas en tiempo real.
 */
(function () {
  const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/missing_updates`;
  const grid = document.getElementById("missing-grid");
  const statusEl = document.getElementById("ws-status");

  function setStatus(text, ok) {
    if (statusEl) {
      statusEl.textContent = text;
      statusEl.className = ok ? "status ok" : "status err";
    }
  }

  function renderCard(person) {
    const card = document.createElement("article");
    card.className = "missing-card";
    card.dataset.externalId = person.external_id || person.id;

    const photo = person.photo_url
      ? `<img src="${person.photo_url}" alt="${person.full_name}" loading="lazy" />`
      : `<div class="no-photo">Sin foto</div>`;

    card.innerHTML = `
      ${photo}
      <div class="body">
        <h3>${person.full_name}</h3>
        <p>${person.age ? person.age + " años" : "Edad no reportada"} · ${person.last_known_location || "Ubicación desconocida"}</p>
        <p class="marks">${person.distinguishing_marks || "Sin descripción física"}</p>
        <span class="badge ${person.source_estado || person.status}">${person.source_estado || person.status}</span>
      </div>
    `;
    return card;
  }

  function upsertCard(person, eventType) {
    if (!grid) return;
    const key = person.external_id || String(person.id);
    const existing = grid.querySelector(`[data-external-id="${key}"]`);

    if (existing) {
      existing.replaceWith(renderCard(person));
      existing.classList?.add("flash-update");
    } else {
      const card = renderCard(person);
      card.classList.add("flash-new");
      grid.prepend(card);
    }

    document.title = `(${grid.children.length}) Venezuela te Busca — ${eventType}`;
  }

  function connect() {
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => setStatus("Conectado — escuchando nuevos reportes", true);

    ws.onmessage = (msg) => {
      const payload = JSON.parse(msg.data);
      if (payload.event === "new_missing" || payload.event === "updated_missing") {
        upsertCard(payload.data, payload.event);
      }
    };

    ws.onclose = () => {
      setStatus("Desconectado — reintentando en 3s…", false);
      setTimeout(connect, 3000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
})();