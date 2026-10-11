"""Уведомление Windows о готовой или неудавшейся загрузке."""
import os
import subprocess
import threading
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .config import NO_WINDOW, log

# Скрипт — константа: в его текст не подставляется ничего. Содержимое уведомления приходит отдельно, переменной
# окружения. Раньше название видео вставлялось прямо в текст скрипта, и название с переводом строки и «'@»
# (его задаёт сайт) выполнялось как команда PowerShell.
# Своего «приложения» у качалки в Windows нет, поэтому уведомление показываем от имени PowerShell.
SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
    "$x.LoadXml($env:KACHALKA_TOAST)\n"
    "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show((New-Object Windows.UI.Notifications.ToastNotification $x))"
)


def toast_xml(title, text, file=None):
    """Содержимое уведомления. Для готового файла — с кнопками «Открыть файл» и «Показать в папке»."""
    actions = launch = ""
    if file:
        uri = Path(file).as_uri()
        launch = f" activationType=\"protocol\" launch={quoteattr(uri)}"
        actions = (f"<actions><action content=\"Открыть файл\" activationType=\"protocol\" arguments={quoteattr(uri)}/>"
                   f"<action content=\"Показать в папке\" activationType=\"protocol\" arguments={quoteattr(Path(file).parent.as_uri())}/></actions>")
    one_line = " ".join(str(text).split())[:200]  # название приходит с сайта: переводы строк и хвосты не нужны
    return (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(' '.join(str(title).split()))}</text>"
            f"<text>{escape(one_line)}</text></binding></visual>{actions}</toast>")


def toast(title, text, file=None):
    """Всплывающее уведомление в углу экрана."""
    xml = toast_xml(title, text, file)

    def show():
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", SCRIPT], capture_output=True, text=True, timeout=30,
                               creationflags=NO_WINDOW, env={**os.environ, "KACHALKA_TOAST": xml})
            if r.returncode:
                log.warning(f"уведомление Windows не показалось: {r.stderr.strip()[:300]}")
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"уведомление Windows не показалось: {e!r}")

    threading.Thread(target=show, daemon=True).start()
