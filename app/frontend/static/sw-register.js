/** PWA offline-first: cola de sobrevivientes + Service Worker */
const QUEUE_KEY = "red_esperanza_offline_queue";

const OfflineQueue = {
  async _get() {
    try {
      return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
    } catch {
      return [];
    }
  },
  async _set(items) {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(items));
  },
  async enqueue(payload) {
    const q = await this._get();
    q.push(payload);
    await this._set(q);
  },
  async flush() {
    const q = await this._get();
    if (!q.length || !navigator.onLine) return;
    try {
      const res = await fetch("/api/sync/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: q }),
      });
      if (res.ok) {
        await this._set([]);
        console.log("[Red de Esperanza] Cola offline sincronizada");
      }
    } catch (e) {
      console.warn("[Red de Esperanza] Sync pendiente", e);
    }
  },
};

window.OfflineQueue = OfflineQueue;

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

window.addEventListener("load", () => OfflineQueue.flush());