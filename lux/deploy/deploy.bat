@echo off
rem ---------------------------------------------------------------------------
rem  Despliega Drive en un servidor remoto por SSH, desde Windows.
rem
rem    deploy\deploy.bat usuario@servidor
rem    deploy\deploy.bat -p 2222 -i %USERPROFILE%\.ssh\id_drive root@192.168.1.10
rem    deploy\deploy.bat --update usuario@servidor
rem    deploy\deploy.bat --lux-bin C:\ruta\lux usuario@servidor
rem
rem  Usa el ssh, scp y tar que incluye Windows 10 (1803) y posteriores.
rem  Si ya hay una instalacion, actualiza solo el codigo: base de datos,
rem  ficheros subidos, config.json y secreto de firma se conservan siempre.
rem ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

rem La ruta del script se guarda antes de tocar los argumentos: cada 'shift'
rem desplaza tambien %0, y despues %~dp0 ya no apunta aqui.
set "SCRIPT_DIR=%~dp0"

set "SSH_PORT=22"
set "SSH_KEY="
set "REMOTE_STAGING=/tmp/drive-deploy"
set "HEALTH_PORT=8000"
set "MODE=auto"
set "LUX_BIN="
set "ASSUME_YES=0"
set "DRY_RUN=0"
set "TARGET="

rem --------------------------------------------------------------- argumentos
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-p"            ( set "SSH_PORT=%~2" & shift & shift & goto parse )
if /i "%~1"=="--port"        ( set "SSH_PORT=%~2" & shift & shift & goto parse )
if /i "%~1"=="-i"            ( set "SSH_KEY=%~2" & shift & shift & goto parse )
if /i "%~1"=="--key"         ( set "SSH_KEY=%~2" & shift & shift & goto parse )
if /i "%~1"=="-d"            ( set "REMOTE_STAGING=%~2" & shift & shift & goto parse )
if /i "%~1"=="--staging"     ( set "REMOTE_STAGING=%~2" & shift & shift & goto parse )
if /i "%~1"=="--health-port" ( set "HEALTH_PORT=%~2" & shift & shift & goto parse )
if /i "%~1"=="--lux-bin"     ( set "LUX_BIN=%~2" & shift & shift & goto parse )
if /i "%~1"=="--install"     ( set "MODE=install" & shift & goto parse )
if /i "%~1"=="--update"      ( set "MODE=update" & shift & goto parse )
if /i "%~1"=="-y"            ( set "ASSUME_YES=1" & shift & goto parse )
if /i "%~1"=="--yes"         ( set "ASSUME_YES=1" & shift & goto parse )
if /i "%~1"=="-n"            ( set "DRY_RUN=1" & shift & goto parse )
if /i "%~1"=="--dry-run"     ( set "DRY_RUN=1" & shift & goto parse )
if /i "%~1"=="-h"            goto ayuda
if /i "%~1"=="--help"        goto ayuda
if /i "%~1"=="/?"            goto ayuda
set "ARG=%~1"
if "!ARG:~0,1!"=="-" (
    echo [ERROR] Opcion desconocida: %~1
    goto uso_error
)
if defined TARGET (
    echo [ERROR] Solo se admite un servidor de destino.
    exit /b 1
)
set "TARGET=%~1"
shift
goto parse

:ayuda
call :mostrar_uso
exit /b 0

:uso_error
call :mostrar_uso
exit /b 1

:mostrar_uso
echo.
echo Uso: deploy.bat [opciones] [usuario@]servidor
echo.
echo Opciones:
echo   -p, --port PUERTO     Puerto SSH (por defecto 22^)
echo   -i, --key FICHERO     Clave privada SSH
echo   -d, --staging RUTA    Directorio temporal en el servidor (/tmp/drive-deploy^)
echo       --health-port N   Puerto donde comprobar /healthz (8000^)
echo       --lux-bin FICHERO Binario de Lux a instalar en el servidor (si no, el
echo                         servidor debe tener ya "lux" en el PATH^)
echo       --install         Forzar instalacion completa
echo       --update          Forzar actualizacion de codigo solamente
echo   -y, --yes             No pedir confirmacion
echo   -n, --dry-run         Mostrar lo que se haria, sin tocar nada
echo   -h, --help            Esta ayuda
echo.
echo Requisitos en el servidor: Ubuntu con acceso sudo para el usuario indicado.
echo.
exit /b 0

:parsed
if not defined TARGET (
    echo [ERROR] Falta el servidor de destino.
    goto uso_error
)

rem ------------------------------------------------------------- herramientas
where ssh >nul 2>&1 || ( echo [ERROR] No se encuentra 'ssh'. Instala OpenSSH desde Configuracion ^> Aplicaciones ^> Caracteristicas opcionales. & exit /b 1 )
where scp >nul 2>&1 || ( echo [ERROR] No se encuentra 'scp'. & exit /b 1 )
where tar >nul 2>&1 || ( echo [ERROR] No se encuentra 'tar'. Hace falta Windows 10 1803 o posterior. & exit /b 1 )

rem La raiz del proyecto es el directorio padre de deploy\
pushd "%SCRIPT_DIR%.." || exit /b 1
set "RAIZ=%CD%"

for %%F in (app\app.lux config\config.json deploy\install.sh) do (
    if not exist "%%F" (
        echo [ERROR] No parece la raiz del proyecto: falta %%F
        popd
        exit /b 1
    )
)

set "SSH_OPTS=-p %SSH_PORT% -o ConnectTimeout=10"
set "SCP_OPTS=-P %SSH_PORT% -o ConnectTimeout=10"
if defined SSH_KEY (
    set "SSH_OPTS=%SSH_OPTS% -i "%SSH_KEY%""
    set "SCP_OPTS=%SCP_OPTS% -i "%SSH_KEY%""
)

rem ------------------------------------------------------- comprobacion previa
echo.
echo Conectando con %TARGET% (puerto %SSH_PORT%^)...
ssh %SSH_OPTS% %TARGET% "true" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se puede conectar por SSH con %TARGET%.
    echo         Comprueba el host, el usuario, el puerto y tu clave.
    popd
    exit /b 1
)

set "YA_INSTALADO=0"
ssh %SSH_OPTS% %TARGET% "test -f /opt/drive/app/app.lux" >nul 2>&1
if not errorlevel 1 set "YA_INSTALADO=1"

ssh %SSH_OPTS% %TARGET% "test -d /opt/drive/venv" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] Hay una instalacion de la version Python en /opt/drive ^(venv/^).
    echo         Esta version no migra sus datos ^(otro esquema y otros hashes de
    echo         contrasena^): instala en otra maquina o desinstala antes la anterior.
    popd
    exit /b 1
)

if /i "%MODE%"=="auto" (
    if "%YA_INSTALADO%"=="1" ( set "MODE=update" ) else ( set "MODE=install" )
)
if /i "%MODE%"=="update" if "%YA_INSTALADO%"=="0" (
    echo [ERROR] Se ha pedido --update pero no hay ninguna instalacion en /opt/drive.
    popd
    exit /b 1
)

rem ------------------------------------------------------------ confirmacion
echo.
echo   Destino     : %TARGET%:%SSH_PORT%
if /i "%MODE%"=="install" (
    echo   Operacion   : instalacion completa
    echo                 paquetes del sistema, usuario 'drive', /opt/drive,
    echo                 /var/lib/drive, secreto de firma y servicio systemd
) else (
    echo   Operacion   : actualizacion de codigo
    echo                 se conservan base de datos, ficheros, config.json y secreto
)
if defined LUX_BIN echo   Binario Lux : %LUX_BIN% ^(se envia y se instala^)
echo   Temporal    : %TARGET%:%REMOTE_STAGING%
echo.

if "%DRY_RUN%"=="1" (
    echo Simulacion: no se ha modificado nada.
    popd
    exit /b 0
)

if "%ASSUME_YES%"=="0" (
    set "RESPUESTA="
    set /p "RESPUESTA=Continuar? [s/N] "
    if /i not "!RESPUESTA!"=="s" if /i not "!RESPUESTA!"=="si" (
        echo Cancelado.
        popd
        exit /b 0
    )
)

rem -------------------------------------------------------------- empaquetado
set "PAQUETE=%TEMP%\drive-deploy-%RANDOM%.tar.gz"

echo.
echo ==^> Empaquetando el proyecto
if defined LUX_BIN (
    copy /y "%LUX_BIN%" "%TEMP%\lux" >nul
    tar -czf "%PAQUETE%" --exclude=data app config deploy README.md needed.md -C "%TEMP%" lux
    del /q "%TEMP%\lux" >nul 2>&1
) else (
    tar -czf "%PAQUETE%" --exclude=data app config deploy README.md needed.md
)
if errorlevel 1 (
    echo [ERROR] Fallo al empaquetar.
    popd
    exit /b 1
)

echo ==^> Enviando a %TARGET%
ssh %SSH_OPTS% %TARGET% "rm -rf '%REMOTE_STAGING%' && mkdir -p '%REMOTE_STAGING%'"
if errorlevel 1 goto fallo_envio

scp %SCP_OPTS% -q "%PAQUETE%" "%TARGET%:%REMOTE_STAGING%/drive.tar.gz"
if errorlevel 1 goto fallo_envio

ssh %SSH_OPTS% %TARGET% "cd '%REMOTE_STAGING%' && tar xzf drive.tar.gz && rm drive.tar.gz"
if errorlevel 1 goto fallo_envio

del /q "%PAQUETE%" >nul 2>&1

rem ---------------------------------------------------------- ejecucion remota
echo.
if /i "%MODE%"=="install" (
    echo ==^> Instalando ^(puede pedirte la contrasena de sudo^)
    ssh -t %SSH_OPTS% %TARGET% "cd '%REMOTE_STAGING%' && sudo bash deploy/install.sh"
) else (
    echo ==^> Actualizando ^(puede pedirte la contrasena de sudo^)
    ssh -t %SSH_OPTS% %TARGET% "cd '%REMOTE_STAGING%' && sudo bash deploy/remote-update.sh %HEALTH_PORT%"
)
set "RESULTADO=%ERRORLEVEL%"

ssh %SSH_OPTS% %TARGET% "rm -rf '%REMOTE_STAGING%'" >nul 2>&1

if not "%RESULTADO%"=="0" (
    echo.
    echo [ERROR] El despliegue ha fallado en el servidor. Revisa la salida de arriba.
    popd
    exit /b %RESULTADO%
)

echo.
echo Despliegue terminado.
if /i "%MODE%"=="install" (
    echo.
    echo Siguientes pasos en el servidor:
    echo.
    echo   REM Primer administrador
    echo   sudo drive-cli createuser TUNOMBRE --role admin
    echo.
    echo   REM Revisar la configuracion
    echo   sudo drive-cli check
    echo.
    echo Ahora escucha en 127.0.0.1:8000, sin cifrar y sin acceso desde fuera.
    echo Lux no sirve TLS: para exponerlo se pone nginx delante ^(lo instala el script^):
    echo.
    echo   sudo bash /opt/drive/deploy/setup-tls.sh letsencrypt tu.dominio tu@correo 443
    echo   sudo bash /opt/drive/deploy/setup-tls.sh self-signed 192.168.1.20 4023
)

popd
exit /b 0

:fallo_envio
echo [ERROR] Fallo al enviar el proyecto al servidor.
del /q "%PAQUETE%" >nul 2>&1
popd
exit /b 1
