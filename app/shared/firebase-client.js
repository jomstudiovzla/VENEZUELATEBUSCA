import { initializeApp } from "firebase/app";
import { getDatabase, onValue, ref } from "firebase/database";

function normalizeFirebaseConfig(config = {}) {
  const projectId = config.projectId || config.project_id;
  const databaseURL = config.databaseURL || config.database_url;
  const appId = config.appId || config.app_id;
  const messagingSenderId =
    config.messagingSenderId ||
    config.messaging_sender_id ||
    (appId ? appId.split(":")[1] : undefined);

  return {
    projectId,
    databaseURL,
    apiKey: config.apiKey || config.api_key,
    authDomain: config.authDomain || config.auth_domain,
    storageBucket: config.storageBucket || config.storage_bucket,
    appId,
    messagingSenderId,
  };
}

/**
 * Cliente Firebase Realtime Database — proyecto Esperanzavzla.
 * Expuesto como window.RedEsperanzaFirebase en el bundle IIFE.
 */
export function createFirebaseClient(config, handlers = {}) {
  const cfg = normalizeFirebaseConfig(config);
  if (!cfg.projectId || !cfg.databaseURL) {
    throw new Error("Firebase: falta projectId o databaseURL");
  }

  const firebaseConfig = {
    apiKey: cfg.apiKey || "AIzaSy-placeholder-reemplazar-en-env",
    authDomain: cfg.authDomain || `${cfg.projectId.toLowerCase()}.firebaseapp.com`,
    databaseURL: cfg.databaseURL,
    projectId: cfg.projectId,
    storageBucket: cfg.storageBucket || `${cfg.projectId.toLowerCase()}.appspot.com`,
  };
  if (cfg.appId) firebaseConfig.appId = cfg.appId;
  if (cfg.messagingSenderId) firebaseConfig.messagingSenderId = cfg.messagingSenderId;

  const app = initializeApp(firebaseConfig);
  const db = getDatabase(app);
  const unsubs = [];

  const watch = (path, cb) => {
    if (!cb) return;
    const r = ref(db, path);
    const unsub = onValue(
      r,
      (snap) => cb(snap.val()),
      (err) => handlers.onError?.(path, err),
    );
    unsubs.push(unsub);
  };

  watch("victimas", handlers.onVictims);
  watch("testimonios", handlers.onTestimonies);
  watch("busquedas_familiares", handlers.onSearches);

  return {
    app,
    db,
    destroy() {
      unsubs.forEach((u) => u());
    },
  };
}

if (typeof window !== "undefined") {
  window.RedEsperanzaFirebase = { createFirebaseClient };
}