/**
 * Centro de Comando — Google Maps GIS + edificaciones en tiempo real
 * Red de Esperanza Venezuela
 */
(function (global) {
  const buildingMarkers = new Map();
  let gmap = null;
  let mapsApiKey = "";
  let infoWindow = null;

  const MARKER_SVGS = {
    red: "http://maps.google.com/mapfiles/ms/icons/red-dot.png",
    green: "http://maps.google.com/mapfiles/ms/icons/green-dot.png",
    blue: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png",
    gray: "http://maps.google.com/mapfiles/ms/icons/grey-dot.png",
  };

  function $(id) {
    return document.getElementById(id);
  }

  async function loadMapsKey() {
    try {
      const r = await fetch("/api/config/public");
      const d = await r.json();
      mapsApiKey = d.google_maps_api_key || "";
      return mapsApiKey;
    } catch {
      return "";
    }
  }

  function loadGoogleMaps(apiKey) {
    return new Promise((resolve, reject) => {
      if (global.google?.maps) return resolve();
      const s = document.createElement("script");
      s.src = `https://maps.googleapis.com/maps/api/js?key=${apiKey}&libraries=marker&v=weekly`;
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("Google Maps no cargó"));
      document.head.appendChild(s);
    });
  }

  function markerIcon(color, pulse) {
    return {
      url: MARKER_SVGS[color] || MARKER_SVGS.gray,
      scaledSize: new google.maps.Size(pulse ? 44 : 36, pulse ? 44 : 36),
      animation: pulse ? google.maps.Animation.BOUNCE : null,
    };
  }

  function popupHtml(p) {
    const tipo = (p.tipo_estructura || "").replace(/_/g, " ");
    const needs = p.necesidades_urgentes
      ? `<p><strong>Necesidades:</strong> ${p.necesidades_urgentes}</p>`
      : "";
    const ver = p.estado_verificacion
      ? "<span style='color:#16a34a'>✓ Verificado</span>"
      : "<span style='color:#d97706'>Pendiente verificación</span>";
    return `<div style="max-width:240px;font-family:system-ui">
      <h3 style="margin:0 0 6px;color:#00247D">${p.nombre_edificio}</h3>
      <p style="margin:0 0 4px;font-size:12px"><strong>Tipo:</strong> ${tipo}</p>
      <p style="margin:0 0 4px;font-size:12px"><strong>Dirección:</strong> ${p.direccion_texto || ""}</p>
      ${needs}
      <p style="margin:8px 0 0;font-size:11px">${ver}</p>
      <p style="margin:6px 0 0;font-size:11px;color:#CF142B;font-weight:700">Fuerza Venezuela 🇻🇪</p>
    </div>`;
  }

  function placeBuilding(item, { pulse } = {}) {
    if (!gmap || !item?.id) return;
    const pos = { lat: item.latitud, lng: item.longitud };
    let m = buildingMarkers.get(item.id);
    if (m) {
      m.setPosition(pos);
      m.setIcon(markerIcon(item.marker_color || "gray", pulse));
    } else {
      m = new google.maps.Marker({
        map: gmap,
        position: pos,
        title: item.nombre_edificio,
        icon: markerIcon(item.marker_color || "gray", pulse),
      });
      m.addListener("click", () => {
        infoWindow.setContent(popupHtml(item));
        infoWindow.open({ anchor: m, map: gmap });
      });
      buildingMarkers.set(item.id, m);
    }
    if (pulse) {
      setTimeout(() => m.setAnimation(null), 4000);
      global.showMatchBanner?.("¡Nuevo reporte en el mapa! Juntos Salvaremos Vidas");
    }
  }

  async function fetchBuildings() {
    const r = await fetch("/api/edificaciones/mapa?format=list");
    const d = await r.json();
    (d.items || []).forEach((it) => placeBuilding(it));
  }

  async function reportBuildingFromGps() {
    if (!navigator.geolocation) {
      alert("Activa GPS en tu dispositivo");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const nombre = prompt("Nombre del edificio o punto:", "Edificio reportado");
        if (!nombre) return;
        const tipo = prompt(
          "Tipo: colapsado | refugio | hospital | centro_acopio",
          "colapsado",
        );
        const direccion = prompt("Dirección o referencia:", "Venezuela");
        const necesidades = prompt("Necesidades urgentes (opcional):", "");
        try {
          const r = await fetch("/api/edificaciones/reportar", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              nombre_edificio: nombre,
              tipo_estructura: tipo || "colapsado",
              direccion_texto: direccion || "Sin dirección",
              latitud: pos.coords.latitude,
              longitud: pos.coords.longitude,
              necesidades_urgentes: necesidades || null,
              reportado_por: "Ciudadano web",
            }),
          });
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail || "Error");
          placeBuilding(d.item, { pulse: true });
          gmap.panTo({ lat: d.item.latitud, lng: d.item.longitud });
        } catch (e) {
          alert(e.message || "No se pudo reportar");
        }
      },
      () => alert("Permiso de ubicación requerido"),
      { enableHighAccuracy: true },
    );
  }

  function onWsBuilding(event, data) {
    if (event === "edificacion_reportada" || event === "edificacion_verificada") {
      placeBuilding(data, { pulse: event === "edificacion_reportada" });
    }
  }

  async function initCommandCenter() {
    const el = $("google-command-map");
    if (!el) return;

    const key = await loadMapsKey();
    if (!key) {
      el.innerHTML = `<div style="padding:2rem;text-align:center;color:#00247D">
        <h3>Configura GOOGLE_MAPS_API_KEY en .env</h3>
        <p>Mientras tanto usa <a href="/mobile/">app móvil</a> o mapa Leaflet abajo.</p>
      </div>`;
      return;
    }

    try {
      await loadGoogleMaps(key);
    } catch {
      el.innerHTML = "<p style='padding:2rem'>Error cargando Google Maps</p>";
      return;
    }

    gmap = new google.maps.Map(el, {
      center: { lat: 10.49, lng: -66.85 },
      zoom: 11,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: true,
      styles: [
        { elementType: "geometry", stylers: [{ color: "#f5f5f5" }] },
        { featureType: "water", elementType: "geometry", stylers: [{ color: "#c9e6ff" }] },
      ],
    });
    infoWindow = new google.maps.InfoWindow();
    await fetchBuildings();

    $("btn-report-building")?.addEventListener("click", reportBuildingFromGps);
    $("btn-center-venezuela")?.addEventListener("click", () => {
      gmap.setCenter({ lat: 10.49, lng: -66.85 });
      gmap.setZoom(7);
    });
  }

  global.CommandCenter = {
    init: initCommandCenter,
    onWsEvent: onWsBuilding,
    placeBuilding,
    getMap: () => gmap,
  };
})(window);