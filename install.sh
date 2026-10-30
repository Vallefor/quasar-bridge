#!/bin/sh
# Download on the speaker and run with sh; stdin stays available for the wizard.
set -eu
umask 077
test "$(id -u)" = 0 || { echo 'Запустите установку от root.' >&2; exit 1; }
case "$(uname -m)" in
 aarch64|arm64) arch=arm64 ;;
 armv7l|armv8l) arch=armv7 ;;
 *) echo 'Поддерживаются только Linux ARM64 и ARMv7.' >&2; exit 1 ;;
esac
for command in wget sha256sum tar mktemp; do
 command -v "$command" >/dev/null || { echo "Не найдена утилита: $command" >&2; exit 1; }
done
release=${QUASAR_RELEASE_URL:-https://github.com/skrashevich/quasar-bridge/releases/latest/download}
archive=quasar-bridge-linux-$arch.tar.gz
stage=$(mktemp -d /tmp/quasar-bridge-download.XXXXXX)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM
echo "Загрузка Quasar Bridge для $arch…"
if ! wget -q -O "$stage/$archive" "$release/$archive" ||
   ! wget -q -O "$stage/$archive.sha256" "$release/$archive.sha256"; then
 echo 'Не удалось скачать выпуск. Проверьте интернет и наличие установочных архивов в GitHub Releases.' >&2
 exit 1
fi
cd "$stage"
sha256sum -c "$archive.sha256" || { echo 'Контрольная сумма не совпала. Установка отменена.' >&2; exit 1; }
mkdir package
tar -xzf "$archive" -C package
sh "$stage/package/setup.sh"
