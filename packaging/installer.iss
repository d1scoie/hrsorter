; HR Sorter — Windows ინსტალერი (Inno Setup 6). აწყობა: .venv\Scripts\python.exe packaging\build.py
; DistDir და AppVersion build.py გადმოსცემს (/D...). ფაილი UTF-8 BOM-ითაა (ქართული ტექსტისთვის).

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef DistDir
  #define DistDir "..\build_out\dist\HRSorter"
#endif

[Setup]
AppId={{7C2E4B6A-9F31-4D5C-8B0E-3A1F6D2C9E57}
AppName=HR გადამრჩევი
AppVersion={#AppVersion}
AppVerName=HR გადამრჩევი {#AppVersion}
AppPublisher=HR Sorter
DefaultDirName={autopf}\HRSorter
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\build_out
OutputBaseFilename=HRSorter-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=HR გადამრჩევი
UninstallDisplayIcon={app}\hrsorter-bg.exe
CloseApplications=yes
RestartApplications=no
LicenseFile=THIRD_PARTY_NOTICES.txt

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "სამუშაო მაგიდაზე მალსახმობი"; Flags: unchecked

[Dirs]
; კონფიგურაცია და ჟურნალები — ყველა მომხმარებლისთვის საერთო; ფონური პროცესი ჩვეულებრივი მომხმარებლით მუშაობს
Name: "{commonappdata}\HRSorter"; Permissions: users-modify
Name: "{code:GetDataDir}"; Permissions: users-modify

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "\_internal\VCRUNTIME140*.dll"; \
  Flags: recursesubdirs createallsubdirs ignoreversion
; Microsoft-ის საერთო runtime: მას სხვა პროგრამაც შეიძლება იყენებდეს (ასე ჩაიშალა 0.1.16-ის განახლება) — იგივე
; ვერსია არ იცვლება (ignoreversion-ის გარეშე), ახალი თუ დაკავებულია — Windows-ის გადატვირთვისას ჩანაცვლდება
Source: "{#DistDir}\_internal\VCRUNTIME140*.dll"; DestDir: "{app}\_internal"; Flags: restartreplace uninsrestartdelete
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\HR გადამრჩევი"; Filename: "{app}\hrsorter-bg.exe"; Parameters: "open"
Name: "{autodesktop}\HR გადამრჩევი"; Filename: "{app}\hrsorter-bg.exe"; Parameters: "open"; Tasks: desktopicon
; ავტოგაშვება Windows-ში შესვლისას (ყველა მომხმარებელი); მეორე worker-ს InstanceLock არ უშვებს
Name: "{commonstartup}\HR Sorter worker"; Filename: "{app}\hrsorter-bg.exe"; Parameters: "worker"
Name: "{commonstartup}\HR Sorter UI"; Filename: "{app}\hrsorter-bg.exe"; Parameters: "ui"
; ხატულა ეკრანის ქვედა მარჯვენა კუთხეში (თითო მომხმარებელზე ერთი)
Name: "{commonstartup}\HR Sorter tray"; Filename: "{app}\hrsorter-bg.exe"; Parameters: "tray"

[Run]
Filename: "{app}\hrsorter.exe"; Parameters: "setup --root ""{code:GetDataDir}"" --tesseract-dir ""{app}\tesseract"""; \
  StatusMsg: "კონფიგურაციის მომზადება..."; Flags: runhidden waituntilterminated
; ავტოგანახლება: HRSorter\Update (SYSTEM) და HRSorter\Start (პროგრამის გაშვება შესულ მომხმარებლებთან)
Filename: "{app}\hrsorter.exe"; Parameters: "update install-tasks"; Flags: runhidden waituntilterminated
; ჩუმი განახლების (/VERYSILENT) შემდეგ — პროგრამის თავიდან გაშვება მომხმარებლების სესიებში
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""HRSorter\Start"""; Flags: runhidden nowait; Check: WizardSilent
Filename: "{app}\hrsorter-bg.exe"; Parameters: "open"; Description: "HR გადამრჩევის გაშვება"; \
  Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""HRSorter\Update"""; Flags: runhidden; RunOnceId: "DelUpdateTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""HRSorter\Start"""; Flags: runhidden; RunOnceId: "DelStartTask"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM hrsorter-bg.exe"; Flags: runhidden; RunOnceId: "StopHRSorter"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  DataDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir, 'სამუშაო საქაღალდე',
    'სად შეინახოს სკანები და დალაგებული დოკუმენტები?',
    'აქ შეიქმნება Inbox\Batch და Inbox\Single (სკანერმა PDF აქ უნდა შეინახოს), Sorted, NeedsReview და არქივი.' + #13#10 +
    'გამოიყენე მხოლოდ ლათინური ასოები ბილიკში. განახლებისას არსებული პარამეტრები არ იცვლება.',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetPreviousData('DataDir', 'C:\HRScan');   // განახლებისას (ჩუმადაც) — წინა არჩევანი
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataDirPage.Values[0]);
end;

function GetDataDir(Param: String): String;
begin
  Result := DataDirPage.Values[0];
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  I: Integer;
  S: String;
begin
  Result := True;
  if CurPageID = DataDirPage.ID then
  begin
    S := DataDirPage.Values[0];
    for I := 1 to Length(S) do
      if Ord(S[I]) > 127 then
      begin
        MsgBox('ბილიკში მხოლოდ ლათინური ასოები გამოიყენე (OCR ქართულ ბილიკს ვერ კითხულობს).', mbError, MB_OK);
        Result := False;
        Exit;
      end;
  end;
end;

var
  AppStopped, Installed: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM hrsorter-bg.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
    AppStopped := True;
  end;
  if CurStep = ssPostInstall then
  begin
    // ავტოგანახლება (updater.installed_version) ამით ხედავს, რომ ეს ვერსია ბოლომდე დადგა
    SaveStringToFile(ExpandConstant('{app}\installed-version.txt'), '{#AppVersion}', False);
    Installed := True;
  end;
end;

procedure DeinitializeSetup;
var
  Code: Integer;
begin
  // ინსტალაცია ჩაიშალა, პროგრამა კი უკვე გავაჩერეთ — ძველი ვერსია თავიდან ჩაირთოს
  if AppStopped and not Installed then
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "HRSorter\Start"', '', SW_HIDE, ewNoWait, Code);
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + 'სამუშაო საქაღალდე:' + NewLine + Space + DataDirPage.Values[0] +
    NewLine + NewLine + 'სკანერის საქაღალდეები:' + NewLine + Space + DataDirPage.Values[0] + '\Inbox\Batch' +
    NewLine + Space + DataDirPage.Values[0] + '\Inbox\Single';
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;
