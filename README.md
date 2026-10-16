# Monitor P2P USDT/BOB (Binance + Bybit)

App para el celular + alertas por Telegram. Corre **gratis** en GitHub, sin servidor ni computadora encendida.

**Qué hace cada 5 minutos**
- Lee los anuncios P2P de Binance y Bybit (filtra anunciantes con pocas órdenes o baja tasa de cumplimiento).
- Análisis técnico sobre velas de 1 h: RSI, MACD, EMA 20/50, Bollinger, soporte y resistencia.
- Lee noticias de Bolivia (El Deber, Red Uno, Unitel y búsquedas sobre dólar, BCB, reservas, combustible) y puntúa si empujan el dólar arriba o abajo.
- Calcula un **escenario dominante** (ALZA / ESTABLE / BAJA) con sus razones.
- Detecta oportunidades **netas de comisión**: arbitraje Binance↔Bybit, cruce directo y spread como maker.
- Te avisa por Telegram y actualiza el panel web.

## Instalación (≈15 minutos, una sola vez)

### 1. Bot de Telegram
1. En Telegram abre **@BotFather** → `/newbot` → guarda el **token**.
2. Abre tu bot nuevo y envíale `/start`.
3. En el navegador abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y copia el número de `"chat":{"id": ...}`. Ese es tu **chat id**.

### 2. Repositorio en GitHub
1. Crea un repositorio **público** (necesario para que Actions y Pages sean gratis e ilimitados). No hay secretos en el código.
2. Sube todos los archivos de esta carpeta (botón *Add file → Upload files*; incluye la carpeta oculta `.github`).
3. En **Settings → Secrets and variables → Actions → New repository secret** crea:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `WAVESOFT_TOKEN` *(opcional)*: llave gratuita de wavesoft.cloud/developers/p2p. Sirve para cargar 500 horas de historial el primer día (así RSI y MACD funcionan de inmediato) y como respaldo si Binance falla.
4. En **Settings → Pages**: *Deploy from a branch* → rama `main`, carpeta `/docs`.
5. En la pestaña **Actions** habilita los workflows y ejecuta **Monitor P2P USDT-BOB → Run workflow** una vez.

Tu panel quedará en `https://TU_USUARIO.github.io/NOMBRE_REPO/`.
Si lo abres desde otro dominio, agrega `?repo=TU_USUARIO/NOMBRE_REPO` la primera vez.

### 3. Instalar en el celular
- **Android (Chrome):** menú ⋮ → *Instalar app* / *Agregar a pantalla de inicio*.
- **iPhone (Safari):** Compartir → *Agregar a pantalla de inicio*.

Las notificaciones push llegan por **Telegram** (activa el sonido del chat). El panel es para mirar el detalle.

## Comandos en Telegram
Responde en la siguiente ejecución (hasta ~5–10 min):
- `análisis mercado P2P` o `/estado` → lectura completa.
- `/oportunidades` → solo spreads y arbitraje.
- `/noticias` → titulares recientes con su efecto probable.

## Ajustes (`config.json`)
| Campo | Qué hace |
|---|---|
| `fee_binance_pct`, `fee_bybit_pct` | Comisión por operación. **Verifica la tuya** en cada app. |
| `capital_bob`, `capital_usdt` | Para estimar ganancias en Bs. |
| `trade_amount_bob` | Monto típico por operación: solo se consideran anuncios donde cabe ese monto. |
| `usdt_transfer_fee`, `transfer_size_usdt` | Costo de mover USDT entre plataformas (red TRC20/BEP20, etc.). |
| `alerts.arb_net_pct`, `maker_net_pct`, `taker_net_pct` | Spread neto mínimo para avisarte. |
| `alerts.move_pct_30m`, `move_pct_2h` | Movimiento de precio que dispara alerta de subida/bajada. |
| `alerts.price_above`, `price_below` | Niveles de precio propios (ej. avisar si pasa de 12.00). |
| `alerts.cooldown_min` | Minutos de espera para no repetir la misma alerta. |

## Cómo leer las señales
- **Compra/venta de USDT:** "compras USDT a" es el precio más bajo de los anunciantes que venden; "vendes USDT a" es el más alto de los que compran.
- **Arbitraje:** comprar en una plataforma y vender en la otra. Se muestra el neto sin y con costo de transferencia (si mantienes saldo en ambas, evitas ese costo).
- **Maker:** el margen si publicas tus propios anuncios en la posición `maker_rank` (por defecto 3.ª) de cada lado.
- **Escenario:** mezcla RSI, MACD, tendencia, momentum de 3 h, presión de oferta/demanda y noticias. Con poco historial la confianza sale "baja".
- Regla incluida: con escenario BAJA fuerte la app te sugiere **no comprar** hasta que el RSI esté bajo y el precio se estabilice.

## Límites que debes conocer
- GitHub Actions no es tiempo real: el cron de 5 min puede atrasarse varios minutos. Para cruces que duran segundos, úsalo como radar y confirma en la app del exchange.
- Binance y Bybit no publican APIs oficiales para P2P; usan endpoints públicos que pueden cambiar o bloquear IPs de la nube. Si una plataforma falla, el panel lo avisa y sigue con la otra (y con wavesoft si configuraste su token).
- La lectura de noticias es por palabras clave del titular. Es orientativa.
- Esto es una herramienta informativa, no asesoría financiera.

## Probar localmente
```bash
python monitor.py --demo --data /tmp/demo     # datos simulados
python monitor.py --dry-run --data datos      # datos reales, alertas por consola
```
