@echo off
setlocal
rem ============================================================================
rem  build_clipboard.bat - rebuild pyclipboard.pyd and repackage the exe
rem
rem  Steps: vcvars64(MSVC+SDK) -> maturin build --release -> pip install wheel
rem         -> make sure jietuba_pp.exe is NOT running -> package exe
rem
rem  Usage: double click, or run  build_clipboard.bat  from a terminal
rem ============================================================================
set "ROOT=%~dp0"
set "VCVARS=F:\Work\ZCode_Project\jietuba\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set "RUSTUP_HOME=F:\Work\ZCode_Project\jietuba\toolchain\rustup"
set "CARGO_HOME=F:\Work\ZCode_Project\jietuba\toolchain\cargo"
set "PY=%ROOT%venv311\Scripts\python.exe"
set "MATURIN=%ROOT%venv311\Scripts\maturin.exe"
set "WHEELDIR=%ROOT%rust_libs\target\wheels"

rem cargo lives in a per-project toolchain dir, add it explicitly so this
rem script also works when launched from a shell that predates the PATH edit
set "PATH=%CARGO_HOME%\bin;%PATH%"

rem refuse to run half-way if a required input is missing
if not exist "%VCVARS%" ( echo missing vcvars64: %VCVARS% & exit /b 1 )
if not exist "%PY%" ( echo missing python: %PY% & exit /b 1 )
if not exist "%MATURIN%" ( echo missing maturin: %MATURIN% & exit /b 1 )
if not exist "%ROOT%build_with_ocr_onefile.py" ( echo missing packaging script & exit /b 1 )

echo [1/4] Loading MSVC environment...
call "%VCVARS%" >nul 2>&1

echo [2/4] maturin build --release ...
"%MATURIN%" build --release -i "%PY%" --manifest-path "%ROOT%rust_libs\pyclipboard\Cargo.toml"
if errorlevel 1 (
    echo maturin build FAILED
    exit /b 1
)

echo [3/4] Installing newest wheel into venv311...
set "WHEEL="
for /f "delims=" %%W in ('powershell -NoProfile -Command "Get-ChildItem '%WHEELDIR%\j_clipboard-*.whl' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName"') do set "WHEEL=%%W"
if not defined WHEEL (
    echo no wheel found in %WHEELDIR%
    exit /b 1
)
echo       %WHEEL%
"%PY%" -m pip install --force-reinstall --no-deps "%WHEEL%"
if errorlevel 1 (
    echo wheel install FAILED
    exit /b 1
)

echo [4/4] Packaging exe...
tasklist /FI "IMAGENAME eq jietuba_pp.exe" 2>nul | find /I "jietuba_pp.exe" >nul
if not errorlevel 1 (
    echo jietuba_pp.exe is running - quit the app first.
    exit /b 1
)
"%PY%" "%ROOT%build_with_ocr_onefile.py"
if errorlevel 1 (
    echo package FAILED
    exit /b 1
)

echo.
echo ============ DONE ============
dir /b "%ROOT%dist\jietuba_pp.exe"
exit /b 0
