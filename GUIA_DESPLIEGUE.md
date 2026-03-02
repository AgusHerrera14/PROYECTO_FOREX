# Guia de Despliegue - FundedNext EURUSD Bot

## Requisitos

- Windows 10/11
- MetaTrader 5 instalado y con cuenta activa
- Python 3.10+ (https://www.python.org/downloads/) - marcar **"Add Python to PATH"** al instalar
- Git (https://git-scm.com/downloads)

---

## Paso 1: Clonar el repositorio

```cmd
git clone https://github.com/AgusHerrera14/PROYECTO_FOREX.git
cd PROYECTO_FOREX
git checkout claude/setup-mt5-trading-bot-rV6Zv
```

## Paso 2: Configurar credenciales

Hay dos opciones para manejar los secretos (password MT5, tokens Telegram):

### Opcion A: Variables de entorno (recomendado)

Crear un archivo `set_env.bat` en la carpeta del proyecto (NO subirlo a git):

```cmd
set MT5_PASSWORD=tu_password_de_mt5
set TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
set TELEGRAM_CHAT_ID=123456789
```

### Opcion B: Editar config.yaml localmente

Editar `config.yaml` y completar los campos directamente:

```yaml
mt5:
  password: "tu_password_de_mt5"

telegram:
  bot_token: "tu_token_real"
  chat_id: "tu_chat_id_real"
```

**IMPORTANTE: Si editas config.yaml, NO hagas git push con tus credenciales.**

## Paso 3: Configurar Telegram (para alertas al celular)

1. Abrir Telegram en tu celular
2. Buscar **@BotFather** y enviar `/newbot`
3. Elegir un nombre (ej: "FN Trading Bot")
4. Elegir un username (ej: `fn_trading_12345_bot`) - debe terminar en "bot"
5. BotFather te devuelve el **bot_token** - copialo
6. Buscar **@userinfobot** en Telegram y enviar `/start`
7. Te devuelve tu **chat_id** (un numero) - copialo
8. Enviar cualquier mensaje a tu bot nuevo (para activar el chat)
9. Usar estos valores en el Paso 2

## Paso 4: Preparar MetaTrader 5

1. Abrir MetaTrader 5
2. Verificar que estas conectado (barrita verde abajo a la derecha)
3. Verificar que tu cuenta esta logueada (Archivo > Login)
4. Agregar EURUSD al Market Watch:
   - Click derecho en Market Watch > "Show All"
   - O buscar EURUSD y hacer click en "Show"
5. Dejar MT5 abierto (el bot se conecta a traves de MT5)

## Paso 5: Descargar datos historicos (opcional, para backtest)

```cmd
python data_export.py --source dukascopy --start 2020-01-01 --end 2026-03-01
```

O simplemente hacer doble click en `DESCARGAR_DATOS.bat`.

Despues correr el backtest:

```cmd
python main.py --mode backtest
```

## Paso 6: Iniciar el bot

Hacer doble click en **INICIAR_BOT.bat** o ejecutar:

```cmd
python main.py --mode live
```

El bot deberia mostrar:
```
FundedNext EURUSD Bot v2.0 | Mode: LIVE
[MT5] Connected. Account: 103720564 | Balance: $100000.00
```

Y recibir un mensaje en Telegram: "Bot started in live mode"

---

## Horarios de operacion (Argentina UTC-3)

El bot usa la estrategia London Breakout. La laptop debe estar encendida durante:

| Evento | Hora UTC | Hora Argentina |
|--------|----------|----------------|
| Rango Asiatico (recoleccion) | 00:00 - 06:00 | 21:00 - 03:00 |
| Entrada London Breakout | 07:00 - 10:00 | 04:00 - 07:00 |
| Monitoreo posiciones | 10:00 - 24:00 | 07:00 - 21:00 |

**Minimo requerido**: Laptop encendida de **21:00 a 10:00** (hora Argentina).

**Importante**: Desactivar suspension/hibernacion de Windows:
- Panel de Control > Opciones de energia > Cambiar plan de energia
- Poner "Suspender" en **Nunca** (al menos mientras opera)

---

## Monitoreo

### Telegram
- Recibes alerta cuando se abre/cierra un trade
- Heartbeat cada hora con balance, equity y drawdown
- Alertas de reglas de compliance (pause, kill switch)

### Logs
- `logs/system_YYYYMMDD.log` - Log del sistema
- `logs/trades_YYYYMM.csv` - Historial de trades

### MT5
- Pestaña "Trade" en MT5 muestra posiciones abiertas por el bot
- Magic number del bot: **202503** (para identificar sus trades)

---

## Problemas comunes

| Error | Solucion |
|-------|----------|
| "Cannot connect to broker" | Abrir MT5 y verificar conexion |
| "Symbol EURUSD not available" | Agregar EURUSD al Market Watch |
| "Spread too high" | Normal fuera de London session, el bot espera |
| "FAILSAFE_BLOCK" (news) | No puede leer calendario; bloquea trading temporalmente |
| "KILL_SWITCH" | DD supero 8%, bot cierra todo. Reiniciar manualmente |
| Bot se cierra solo | Revisar logs, reiniciar con INICIAR_BOT.bat |

---

## Reglas de riesgo activas

| Regla | Valor | Accion |
|-------|-------|--------|
| Riesgo por trade | 2.25% ($2,250) | Tamanio de posicion |
| Riesgo reducido | 1.0% ($1,000) | Despues de 3 perdidas seguidas |
| Max perdida diaria | 3.5% ($3,500) | Pausa hasta dia siguiente |
| Max drawdown total | 8.0% ($8,000) | KILL SWITCH - cierra todo |
| Max trades por dia | 3 | No opera mas ese dia |
| Max spread | 2.5 pips | No opera si spread es mayor |
