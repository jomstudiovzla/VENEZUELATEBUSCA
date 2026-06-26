/**
 * Walkie-talkie por Bluetooth BLE (sin datos móviles).
 * Requiere dispositivo con Web Bluetooth y radio BLE compatible.
 * Fallback: mensajes por WebSocket cuando hay conexión.
 */
export class BlePTT {
  constructor({ onMessage, onStatus }) {
    this.onMessage = onMessage || (() => {});
    this.onStatus = onStatus || (() => {});
    this.device = null;
    this.server = null;
    this._holding = false;
  }

  async connect() {
    if (!navigator.bluetooth) {
      this.onStatus("Web Bluetooth no disponible en este navegador");
      return false;
    }
    try {
      this.device = await navigator.bluetooth.requestDevice({
        acceptAllDevices: true,
        optionalServices: ["battery_service", "device_information"],
      });
      this.device.addEventListener("gattserverdisconnected", () => {
        this.onStatus("BLE desconectado");
      });
      this.server = await this.device.gatt.connect();
      this.onStatus(`Conectado: ${this.device.name || "dispositivo BLE"}`);
      return true;
    } catch (e) {
      this.onStatus(e.message || "Conexión BLE cancelada");
      return false;
    }
  }

  startTalk() {
    this._holding = true;
    this.onStatus("🎙️ Transmitiendo (mantén presionado)");
    this.onMessage({ type: "ptt_start", ts: Date.now() });
  }

  stopTalk() {
    if (!this._holding) return;
    this._holding = false;
    this.onStatus("Escuchando…");
    this.onMessage({ type: "ptt_end", ts: Date.now() });
  }

  disconnect() {
    if (this.device?.gatt?.connected) this.device.gatt.disconnect();
    this.device = null;
    this.server = null;
    this.onStatus("Desconectado");
  }
}