#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINK="/tmp/ojo-de-dios-build"
ln -sfn "$ROOT" "$LINK"

if [ ! -d /tmp/jdk21 ]; then
  echo "Descarga JDK 21 en /tmp/jdk21 (ver scripts/iniciar-prototipo.sh o documentación)."
  exit 1
fi
if [ ! -d /tmp/android-sdk-build ]; then
  echo "Falta Android SDK en /tmp/android-sdk-build. Ejecuta empaquetar una vez desde este Mac."
  exit 1
fi

export JAVA_HOME="/tmp/jdk21/Contents/Home"
export ANDROID_HOME="/tmp/android-sdk-build"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

cd "$LINK/mobile-native/android"
./gradlew assembleDebug
mkdir -p "$ROOT/dist"
cp app/build/outputs/apk/debug/app-debug.apk "$ROOT/dist/RedEsperanza-debug.apk"
echo "APK → dist/RedEsperanza-debug.apk"