; Установщик Keeper. Собирается скриптом installer\build.py: он готовит папку stage и запускает этот сценарий.
; Ставится в профиль пользователя, прав администратора не просит.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{6C1B5E0A-52C1-4C0B-9D6E-5B0F3A7C2E41}
AppName=Keeper
AppVersion={#AppVersion}
AppPublisher=Keeper
DefaultDirName={localappdata}\Keeper
; первая страница объясняет, что будет установлено; папку человек выбирает сам (при обновлении — прежняя)
DisableWelcomePage=no
DisableDirPage=no
UsePreviousAppDir=yes
; картинки делает build.py из значка расширения
SetupIconFile=art\keeper.ico
WizardSmallImageFile=art\small.bmp
WizardImageFile=art\side.bmp
UninstallDisplayIcon={app}\keeper.ico
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=out
OutputBaseFilename=Keeper-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Keeper
; сами закрываем программу перед заменой файлов (см. StopKeeper) — стандартный поиск занятых файлов не нужен
CloseApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel1=Установка Keeper
ru.WelcomeLabel2=Keeper скачивает видео с открытой вкладки Chrome, а ещё звук отдельно и текст того, что в видео говорят.%n%nЧто будет установлено (около 410 МБ на диске):%n%n  •  программа Keeper и расширение для Chrome;%n  •  Python и ffmpeg — свои, внутри папки программы. Если они у тебя уже стоят, их это не затронет;%n  •  по желанию — распознавание речи (выберешь на следующих шагах).%n%nВсё ставится в одну папку, которую ты выберешь дальше. Права администратора не нужны. Удалить можно в «Параметры → Приложения».%n%nПоследний шаг — добавить расширение в Chrome — делается вручную: после установки откроется страница с подсказкой.
ru.SelectDirLabel3=Keeper будет установлен в эту папку. Сюда же позже скачаются модели распознавания речи (до 660 МБ). Видео сохраняются отдельно — в «Загрузки\Videos», это меняется в окне расширения.
ru.SelectDirBrowseLabel=Нажми «Далее», чтобы оставить эту папку, или «Обзор», чтобы выбрать другую. Выбирай папку, куда можно записывать без прав администратора (не Program Files).

[Tasks]
Name: "model_ru"; Description: "Текст на русском — скачать сейчас (170 МБ)"; GroupDescription: "Текст из видео (распознавание речи). Не отметишь — скачается само при первом запросе текста:"
Name: "model_multi"; Description: "Текст на английском и других европейских языках — скачать сейчас (490 МБ)"; GroupDescription: "Текст из видео (распознавание речи). Не отметишь — скачается само при первом запросе текста:"; Flags: unchecked

[InstallDelete]
; при обновлении старые файлы программы убираем целиком: иначе остались бы удалённые модули и две версии yt-dlp.
; Модели, настройки, история и журналы лежат в других папках и остаются.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\kachalka"
Type: filesandordirs; Name: "{app}\extension"

[Files]
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "art\keeper.ico"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; по этой записи Chrome находит программу; само значение пишет launcher.py --install, здесь — чтобы запись
; исчезла при удалении
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.kachalka.host"; ValueType: string; ValueData: "{app}\native_host.json"; Flags: uninsdeletekey

[Run]
Filename: "{app}\python\python.exe"; Parameters: """{app}\launcher.py"" --install"; WorkingDir: "{app}"; StatusMsg: "Подключаю программу к Chrome…"; Flags: runhidden
; окно с процентами: загрузка идёт минуты, без него установщик выглядел бы зависшим
Filename: "{app}\python\python.exe"; Parameters: "-m kachalka.fetch_models ru"; WorkingDir: "{app}"; StatusMsg: "Скачиваю распознавание русской речи (170 МБ)…"; Tasks: model_ru
Filename: "{app}\python\python.exe"; Parameters: "-m kachalka.fetch_models multi"; WorkingDir: "{app}"; StatusMsg: "Скачиваю распознавание других языков (490 МБ)…"; Tasks: model_multi
Filename: "{app}\next-steps.html"; Description: "Показать, как добавить Keeper в Chrome (последний шаг)"; Flags: postinstall shellexec skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Работающая программа держит свои файлы — перед установкой и удалением закрываем её. Только процессы из
// нашей папки: чужой Python на этом компьютере не трогаем.
procedure StopKeeper;
var
  Code: Integer;
begin
  Exec('powershell.exe',
    '-NoProfile -Command "Get-Process python,pythonw,ffmpeg,ffprobe -ErrorAction SilentlyContinue | Where-Object { $_.Path -like ''' +
    ExpandConstant('{app}') + '\*'' } | Stop-Process -Force"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopKeeper;
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopKeeper;
  Result := True;
end;
