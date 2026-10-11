; Установщик VideoTranskeeper. Собирается скриптом installer\build.py: он готовит папку stage и запускает этот сценарий.
; Ставится в профиль пользователя, прав администратора не просит.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{6C1B5E0A-52C1-4C0B-9D6E-5B0F3A7C2E41}
AppName=VideoTranskeeper
AppVersion={#AppVersion}
AppPublisher=VideoTranskeeper
DefaultDirName={localappdata}\VideoTranskeeper
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
OutputBaseFilename=VideoTranskeeper-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=VideoTranskeeper
; сами закрываем программу перед заменой файлов (см. StopKeeper) — стандартный поиск занятых файлов не нужен
CloseApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel1=Установка VideoTranskeeper
ru.WelcomeLabel2=VideoTranskeeper скачивает видео с открытой вкладки браузера (Chrome, Edge, Яндекс Браузер), а ещё звук и текст того, что в видео говорят.%n%nБудет установлено (около 410 МБ):%n%n  •  программа VideoTranskeeper и расширение для браузера;%n  •  свои Python и ffmpeg — внутри папки программы, уже установленные не затрагиваются;%n  •  по желанию — распознавание речи для текста.%n%nВсё ставится в одну папку, права администратора не нужны.%n%nПоследний шаг — добавить расширение в браузер — делается вручную: после установки откроется подсказка.
ru.SelectDirLabel3=VideoTranskeeper будет установлен в эту папку. Сюда же позже скачаются модели распознавания речи (до 660 МБ). Видео сохраняются отдельно — в «Загрузки\Videos», это меняется в окне расширения.
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
; по этим записям Chrome, Edge и Яндекс Браузер находят программу; само значение пишет launcher.py --install, здесь — чтобы записи
; исчезли при удалении
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.kachalka.host"; ValueType: string; ValueData: "{app}\native_host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.kachalka.host"; ValueType: string; ValueData: "{app}\native_host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Yandex\YandexBrowser\NativeMessagingHosts\com.kachalka.host"; ValueType: string; ValueData: "{app}\native_host.json"; Flags: uninsdeletekey

[Run]
Filename: "{app}\python\python.exe"; Parameters: """{app}\launcher.py"" --install"; WorkingDir: "{app}"; StatusMsg: "Подключаю программу к браузерам…"; Flags: runhidden
Filename: "{app}\next-steps.html"; Description: "Показать, как добавить VideoTranskeeper в браузер (последний шаг)"; Flags: postinstall shellexec skipifsilent

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

// Модели распознавания скачиваются прямо в окне установщика: программа-загрузчик работает скрыто и построчно
// сообщает проценты, а мы показываем их на общей полосе. Раньше для этого открывалось отдельное чёрное окно.
procedure ModelProgress(const S: String; const Error, FirstLine: Boolean);
var
  Percent: Integer;
begin
  Percent := StrToIntDef(Trim(S), -1);
  if Percent >= 0 then
  begin
    WizardForm.ProgressGauge.Position := Percent * WizardForm.ProgressGauge.Max div 100;
    WizardForm.FilenameLabel.Caption := Format('Скачано %d%%', [Percent]);
  end;
end;

procedure FetchModel(const Name, Title: String);
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Title;
  WizardForm.FilenameLabel.Caption := '';
  WizardForm.ProgressGauge.Position := 0;
  if not ExecAndLogOutput(ExpandConstant('{app}\python\python.exe'), '-m kachalka.fetch_models --lines ' + Name,
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code, @ModelProgress) or (Code <> 0) then
    SuppressibleMsgBox('Распознавание речи сейчас не скачалось (нет интернета?).' + #13#10 +
      'Ничего страшного: оно скачается само при первом запросе текста.', mbInformation, MB_OK, IDOK);
  WizardForm.FilenameLabel.Caption := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('model_ru') then
      FetchModel('ru', 'Скачиваю распознавание русской речи (170 МБ)…');
    if WizardIsTaskSelected('model_multi') then
      FetchModel('multi', 'Скачиваю распознавание английского и других языков (490 МБ)…');
  end;
end;
