#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DIST="$ROOT/dist"
STAMP="$(date +%Y%m%d)"
ZIP="$DIST/RedEsperanza-Prototipo-Movil-${STAMP}.zip"

mkdir -p "$DIST"

echo "→ Sincronizando Capacitor…"
if [ -d "$ROOT/mobile-native" ]; then
  (cd "$ROOT/mobile-native" && npx cap sync 2>/dev/null) || true
fi

echo "→ Creando ${ZIP}…"
zip -r "$ZIP" . \
  -x ".git/*" \
  -x "node_modules/*" \
  -x ".tools/*" \
  -x ".venv/*" \
  -x "*__pycache__*" \
  -x "*.pyc" \
  -x "dist/*" \
  -x "ojo_de_dios.db*" \
  -x "red_esperanza.db*" \
  -x "building_photos/*" \
  -x "reference_photos/*" \
  -x "camera_snapshots/*" \
  -x "cloudflared/*" \
  -x "cloudflared" \
  -x "logs/*" \
  -x "mobile-native/android/build/*" \
  -x "mobile-native/android/.gradle/*" \
  -x ".DS_Store" \
  > /dev/null

if [ -f "$ROOT/mobile-native/android/app/build/outputs/apk/debug/app-debug.apk" ]; then
  cp "$ROOT/mobile-native/android/app/build/outputs/apk/debug/app-debug.apk" \
    "$DIST/RedEsperanza-debug.apk"
  echo "→ APK: dist/RedEsperanza-debug.apk"
fi

if [ -d "$ROOT/mobile-native/ios" ]; then
  (cd "$ROOT/mobile-native/ios" && zip -r "$DIST/RedEsperanza-iOS-Xcode-${STAMP}.zip" App -x "*.DS_Store" > /dev/null) 2>/dev/null || true
  echo "→ iOS: dist/RedEsperanza-iOS-Xcode-${STAMP}.zip (abrir en Xcode)"
fi

echo ""
echo "Listo:"
echo "  $ZIP"
ls -lh "$DIST"/*.zip "$DIST"/*.apk 2>/dev/null || true