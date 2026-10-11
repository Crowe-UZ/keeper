"""Уведомление Windows о готовой или неудавшейся загрузке."""
import subprocess
import threading
from pathlib import Path

from .config import (NO_WINDOW, log)


def toast(title, text, file=None):
    """Всплывающее уведомление в углу экрана. Для готового видео — с кнопками «Открыть» и «Папка»."""
    from xml.sax.saxutils import escape, quoteattr
    actions = launch = ""
    if file:
        uri = Path(file).as_uri()
        launch = f" activationType=\"protocol\" launch={quoteattr(uri)}"
        actions = (f"<actions><action content=\"Открыть\" activationType=\"protocol\" arguments={quoteattr(uri)}/>"
                   f"<action content=\"Папка\" activationType=\"protocol\" arguments={quoteattr(Path(file).parent.as_uri())}/></actions>")
    xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
           f"<text>{escape(text[:200])}</text></binding></visual>{actions}</toast>")
    # своего «приложения» у качалки в Windows нет, поэтому уведомление показываем от имени PowerShell
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
          "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
          "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
          f"$x.LoadXml(@'\n{xml}\n'@)\n"
          "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show((New-Object Windows.UI.Notifications.ToastNotification $x))")

    def show():
        import base64
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(ps.encode("utf-16-le")).decode()],
                               capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            if r.returncode:
                log.warning(f"уведомление Windows не показалось: {r.stderr.strip()[:300]}")
        except Exception as e:
            log.warning(f"уведомление Windows не показалось: {e!r}")

    threading.Thread(target=show, daemon=True).start()
