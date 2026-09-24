# Build Your Own Auto Scaling Controller

Samuel Calderón

---

## 0. Clasificación taxonómica

| Dimensión | Clasificación | Impuesta o Decisión |
|---|---|---|
| Tipo y dirección de elasticidad | Horizontal, bidireccional (incrementar y reducir) | **Impuesta** — el reto exige scaling horizontal y respuesta a subidas y bajadas de demanda (Sección 7, puntos 1 y 6) |
| Recurso escalado | Instancias EC2 (VMs) | **Impuesta** — el reto especifica EC2 detrás de un load balancer (Sección 7, punto 1) |
| Scope | Infraestructura (IaaS) | **Decisión de diseño** — se pudo optar por elasticidad embebida en la aplicación (`code embedded`), pero se eligió a nivel de infraestructura porque el reto prohíbe modificar internamente la app y exige actuar sobre el ASG |
| Purpose | Performance + costo | **Decisión de diseño** — el diseño balancea evitar degradación (umbral alto) contra evitar sobreaprovisionamiento (umbral bajo + reducción) |
| Modo de operación | Automático, reactivo por umbrales (threshold-based) | **Parcialmente impuesta / decisión** — el reto exige "sin intervención humana" (automático); reactivo por umbrales fue la decisión de diseño frente a alternativas proactivas (time-series, ML) descritas en el paper |
| Método de decisión | Umbrales estáticos con confirmación por ciclos consecutivos + cooldown | **Decisión de diseño** — el reto no prescribe algoritmo (Sección 4) |
| Arquitectura del controller | Centralizada | **Decisión de diseño** — un único proceso decide por todo el ASG, frente a alternativas descentralizadas |
| Alcance del proveedor cloud | Single-provider (AWS) | **Impuesta** — el reto exige EC2/CloudWatch específicamente |

---

## 1. Solution Design (10.1)

### 1.1 Arquitectura

```
                    ┌─────────────────────────┐
                    │  Instancia de control    │
                    │  (EC2, fuera del ASG)    │
                    │                          │
                    │   autoscaler.py (loop)   │
                    └───────────┬──────────────┘
                                │
         ┌──────────────────────┼───────────────────────┐
         │                      │                        │
         ▼                      ▼                        ▼
 ┌───────────────┐   ┌────────────────────┐   ┌──────────────────┐
 │  CloudWatch    │   │  Auto Scaling      │   │  SSM Run Command  │
 │  GetMetric     │   │  Group (asg-asc)   │   │  (fallback de     │
 │  Statistics    │   │  set_desired_      │   │  carga interna)   │
 │  (CPUUtiliz.)  │   │  capacity          │   │                    │
 └───────────────┘   └─────────┬──────────┘   └──────────┬─────────┘
                                │                          │
                                ▼                          ▼
                     ┌─────────────────────┐    ┌────────────────────┐
                     │ Instancias EC2       │◄───┤ stress-ng (CPU)     │
                     │ (nginx + stress-ng)  │    │ vía SSM, sin        │
                     │ tras el ALB          │    │ peticiones externas │
                     └─────────────────────┘    └────────────────────┘
```

### 1.2 Componentes

- **Observador** (`get_average_cpu`): consulta `CPUUtilization` en CloudWatch,
  namespace `AWS/EC2`, agregada automáticamente por la dimensión
  `AutoScalingGroupName` (promedio de todas las instancias del ASG en una
  sola llamada).
- **Motor de decisión** (`evaluate_and_act`): política de umbrales con
  confirmación por ciclos consecutivos (ver 1.4).
- **Actuador** (`set_capacity`, `get_current_capacity`): ajusta
  `desired_capacity` del ASG directamente. El ASG de AWS se usa únicamente
  como mecanismo de aprovisionamiento (lanzar/terminar instancias,
  registrarlas en el load balancer, health checks) — **nunca decide él
  mismo cuánto ni cuándo escalar** (cumple restricción 4 de la Sección 7).
- **Fallback de validación** (`trigger_internal_stress`): genera carga real
  de CPU dentro de una instancia del ASG vía AWS Systems Manager (SSM) Run
  Command, cuando no hay carga orgánica tras varios ciclos. No genera
  ninguna petición HTTP externa.
- **Persistencia de estado** (`load_state`/`save_state`): contadores de
  streaks y cooldowns en `state.json`, para sobrevivir reinicios del
  proceso.
- **Log de decisiones** (`log_decision`): registro JSON-lines con cada
  decisión tomada, incluso las de "mantener".

### 1.3 Flujo de información y de acción

```
observe (CloudWatch) → analyze (umbrales + streaks) → decide (MAINTAIN /
INCREASE / REDUCE) → act (ASG.set_desired_capacity o SSM.send_command) →
log (decisions.log) → sleep(60s) → observe again
```

### 1.4 Política de control

| Parámetro | Valor | Justificación |
|---|---|---|
| `CPU_UPPER_THRESHOLD` | 70% | umbral de sobrecarga |
| `CPU_LOWER_THRESHOLD` | 30% | umbral de subutilización |
| `CONSECUTIVE_HIGH_FOR_SCALE_UP` | 3 ciclos (~3 min) | evita reaccionar a picos transitorios |
| `CONSECUTIVE_LOW_FOR_SCALE_DOWN` | 5 ciclos (~5 min) | más conservador para bajar — evita liberar capacidad sobre una caída pasajera |
| `COOLDOWN_SECONDS` | 180s tras cada acción | evita oscilación (flapping) entre subir y bajar |
| `MIN_INSTANCES` / `MAX_INSTANCES` | 1 / 5 | límites duros impuestos por el reto (Sección 7, punto 2) |
| `EVALUATION_INTERVAL_SECONDS` | 60s | balance entre reactividad y no saturar CloudWatch/logs |
| `SCALE_STEP` | 1 instancia por decisión | cambios incrementales, más predecibles que saltos grandes |

### 1.5 Permisos (Sección 7, punto 9)
 
El rol de la instancia de control solo tiene:
- `cloudwatch:GetMetricStatistics`, `cloudwatch:GetMetricData` (lectura de métricas)
- `autoscaling:DescribeAutoScalingGroups`, `autoscaling:SetDesiredCapacity` (lectura y ajuste de capacidad, sin permisos de creación/eliminación de ASGs ni de políticas de scaling)
- `ssm:SendCommand`, `ssm:GetCommandInvocation` (solo para el fallback de carga interna)
No tiene permisos de IAM, de red, ni de otros servicios. La política JSON
completa de mínimo privilegio está documentada en `AWS_SETUP_GUIDE.md`.
 

### 1.6 Supuestos y limitaciones

- Solo se usa CPU como métrica; no se incorpora memoria, latencia ni tasa de
  error.
- El controller es centralizado y de punto único de falla: si el proceso
  muere, no hay decisiones nuevas hasta reiniciarlo manualmente.
- El fallback de carga interna asume que existe al menos una instancia
  `InService` en el ASG; si el ASG está vacío, no hay forma de generar
  carga interna.

---

## 2. Reproducible Implementation (10.2)

### 2.1 Estructura del repositorio
 
```
/
├── autoscaler.py          # controller completo
├── requirements.txt        # boto3
├── .gitignore              
├── AWS_SETUP_GUIDE.md       # instrucciones de despliegue de infra
├── Deliverables.md          
└── Evidencias
```
 
### 2.2 Infraestructura (definida vía consola AWS, documentada en `AWS_SETUP_GUIDE.md`)
 
- VPC default (subnets públicas en al menos 2 AZs)
- Security Groups separados: ALB, instancias del ASG, instancia de control
- Launch Template: Amazon Linux 2023, `nginx` + `stress-ng` vía user data
- Target Group + Application Load Balancer (HTTP:80)
- Auto Scaling Group `asg-asc`: min=1, max=5, **sin políticas de scaling de AWS**
- Instancia de control (EC2 separada), acceso vía Session Manager (SSM), sin puertos abiertos
---

## 3. Experimental Evidence (10.3)
 
Toda la evidencia fue cruzada contra el **Activity History** del propio Auto Scaling Group
en la consola de AWS, que es un registro independiente del log del
controller esto permite confirmar que la decisión no solo quedó anotada
en el log, sino que produjo un cambio real y verificable en la
infraestructura.
 
### 3.1 Corrida de scale-up
 
Antes de esta corrida se restauraron explícitamente los valores de diseño
para producción: `IDLE_CYCLES_BEFORE_STRESS=3` y
`CONSECUTIVE_LOW_FOR_SCALE_DOWN=5`. Es decir, este
escenario corrió con la política tal como queda documentada en la Sección
1.4, sin ningún ajuste temporal para hacer la demo más rápida.
 
**Secuencia observada en el log (`decisions.log`), 2026-09-21:**
 
| Hora (UTC) | CPU observado | Decisión | Razón |
|---|---|---|---|
| 16:48:35 | 0.18% | MAINTAIN_CAPACITY | dentro de rango normal, sin streak |
| 16:49:35 | 0.30% | MAINTAIN_CAPACITY | dentro de rango normal, sin streak |
| 16:50:36 | ~0.2% | MAINTAIN_CAPACITY | fallback disparado: *"Estrés interno enviado ... tras 3 ciclos inactivos"* |
| 16:51:36 | ~2.2% | MAINTAIN_CAPACITY | ya en el mínimo (1); estrés en cooldown |
| 16:52:36 | 99.99% | MAINTAIN_CAPACITY | dentro de rango normal o sin streak suficiente (streak alto: 1/3) |
| 16:53:37 | 100.00% | MAINTAIN_CAPACITY | *"Estrés interno en cooldown, se omite este ciclo"* (streak alto: 2/3) |
| **16:54:37** | **99.99%** | **INCREASE_CAPACITY** | **CPU=100.0% >= 70.0% durante 3 ciclos → `desired_capacity: 1 → 2`** |
| 16:55:37 | 98.27% | MAINTAIN_CAPACITY | cooldown activo tras la última acción |
| 16:56:37 | 0.10% | MAINTAIN_CAPACITY | cooldown activo tras la última acción |
 
El fallback de carga interna (SSM Run Command) se disparó tras exactamente
3 ciclos sin carga orgánica, tal como especifica `IDLE_CYCLES_BEFORE_STRESS
=3`, y la decisión de escalar se tomó al tercer ciclo consecutivo con CPU
≥70% (`CONSECUTIVE_HIGH_FOR_SCALE_UP=3`) — comportamiento exactamente igual
al diseñado, sin ajustes.
 
**Cross-validación con AWS Auto Scaling Group Activity History:**
 
> **Launching a new EC2 instance: i-0e8f46d62d5c176b**
> *"At 2026-09-21T16:54:37Z a user request explicitly set group desired
> capacity changing the desired capacity from 1 to 2. At
> 2026-09-21T16:54:49Z an instance was started in response to a difference
> between desired and actual capacity, increasing the capacity from 1 to
> 2."*
> Estado: **Correcto**. Hora de inicio: 21 sept 2026, 11:54:51 AM (UTC-05:00).
 
El timestamp que AWS registra por su cuenta para el cambio de capacidad
(**16:54:37Z**) coincide, al segundo, con el timestamp de la decisión
`INCREASE_CAPACITY` en el log del controller. Esto confirma que la acción
observada en la infraestructura fue efectivamente causada por esta
decisión del controller y no por otro mecanismo.
 
**Evidencia visual de infraestructura tras el scale-up:**
- Target Group `tg-asc`: 2 destinos registrados, **2 en buen estado, 0 en
  mal estado** — confirma que la nueva instancia pasó el health check del
  ALB y ya recibe tráfico.
- Consola EC2 → Instancias: 3 instancias en ejecución (2 `t3.micro` del
  ASG + 1 `t2.micro` = instancia de control), consistente con
  `desired_capacity=2`.
### 3.2 Corrida de scale-down
 
**Secuencia observada en el log (`decisions.log`), 2026-09-21:**
 
| Hora (UTC) | CPU observado | Decisión | Razón |
|---|---|---|---|
| 17:25:40 | 50.13% | MAINTAIN_CAPACITY | dentro de rango normal o sin streak suficiente |
| 17:26:40 | 50.10% | MAINTAIN_CAPACITY | dentro de rango normal o sin streak suficiente |
| 17:27:40 | 6.82% | MAINTAIN_CAPACITY | dentro de rango normal o sin streak suficiente (streak bajo: 1/2) |
| **17:28:41** | **~0.18%** | **REDUCE_CAPACITY** | **CPU=0.2% <= 30.0% durante 2 ciclos → `desired_capacity: 2 → 1`** |
| 17:29:41 | 0.29% | MAINTAIN_CAPACITY | cooldown activo tras la última acción |
| 17:30:41 | ~0.18% | MAINTAIN_CAPACITY | cooldown activo tras la última acción |
 
**Cross-validación con AWS Auto Scaling Group Activity History:**
 
> **Terminating EC2 instance: i-00d04c7553058745d — Waiting For ELB
> Connection Draining**
> *"At 2026-09-21T17:28:41Z a user request explicitly set group desired
> capacity changing the desired capacity from 2 to 1. At
> 2026-09-21T17:28:47Z an instance was taken out of service in response to
> a difference between desired and actual capacity, shrinking the capacity
> from 2 to 1. At 2026-09-21T17:28:47Z instance i-00d04c7553058745d was
> selected for termination."*
> Estado: Drenaje de conexiones en curso. Hora de inicio: 21 sept 2026,
> 12:28:47 PM (UTC-05:00).
 
De nuevo, el timestamp de AWS (**17:28:41Z**) coincide exactamente con el
segundo en que el controller registró `REDUCE_CAPACITY`, confirmando la
relación causa-efecto entre la decisión y el cambio real en el ASG. El
proceso de terminación respetó el *connection draining* del load
balancer (no se cortó de golpe ninguna conexión en curso).
 
### 3.3 Manejo de fallos (evidencia de la restricción 8 de la Sección 7)
 
En una iteración previa de las pruebas (antes de la corrida final
documentada arriba), el ASG tenía `MaxSize=1` mal configurado en la
consola. El controller intentó escalar y AWS rechazó la operación; el
controller **no se cayó**, registró el error tal cual lo devolvió boto3 y
continuó operando con normalidad en el siguiente ciclo:
 
```
razón=CPU=100.0% >= 70.0% durante 3 ciclos | resultado=ERROR: An error
occurred (ValidationError) when calling the SetDesiredCapacity operation:
New SetDesiredCapacity value 2 is above max value 1 for the AutoScalingGroup.
```
 
Esto es evidencia directa del fail-safe ante fallos de infraestructura: el
error de AWS quedó capturado y logueado como parte del `action_result`,
sin excepción no controlada, y el ciclo siguiente continuó con
normalidad. Una vez corregido `MaxSize=5` en el ASG, las corridas
documentadas en 3.1 y 3.2 se completaron sin este error.
 
### 3.5 Series de tiempo
 
Comparar en CloudWatch (Metrics → EC2 → By Auto Scaling Group →
`CPUUtilization`, period 1 min) contra los timestamps de las tablas
anteriores permite reconstruir visualmente la correlación carga → decisión
→ capacidad para ambas corridas.
 
---

## 4. Critical Analysis (10.4)
 
### 4.1 Fortalezas de la política
 
- **Explicabilidad total**: cada decisión (incluso las de "mantener") queda
  registrada con su justificación exacta.
- **Resiliencia a picos transitorios**: la confirmación por ciclos
  consecutivos evita reaccionar a ruido de corta duración.
- **Fail-safe robusto**: se probó en vivo que un fallo de AWS no
  interrumpe el loop de control.
- **Aislamiento de responsabilidades**: AWS solo aprovisiona; toda la
  lógica de decisión es propia.
### 4.2 Debilidades encontradas durante las pruebas (con evidencia)
 
**Bug 1 — Duración insuficiente del estrés interno.** La primera versión
usaba `STRESS_DURATION_SECONDS=90`, insuficiente para sostener 3 ciclos de
60s consecutivos por encima del umbral (180s necesarios). El estrés se
apagaba antes de completar el streak, y el sistema nunca llegaba a
`INCREASE_CAPACITY`. **Corrección**: se subió a 240s.
 
**Bug 2 — El fallback interrumpía streaks legítimos en construcción.** El
contador `idle_cycles` se incrementaba
incluso mientras se estaba acumulando un streak alto o bajo real. 
Esto causaba que el fallback disparara
carga artificial a mitad de una tendencia real, reiniciándola antes de
completarse — el sistema quedaba oscilando indefinidamente sin nunca
alcanzar 5 ciclos bajos consecutivos. **Corrección**: el fallback ahora
solo se dispara cuando `high_streak == 0 and low_streak == 0`.
 
**Bug 3 — Camino "ya en el mínimo" sin fallback.** Cuando la capacidad ya
estaba en `MIN_INSTANCES` y el streak bajo llegaba a 5, el código entraba
en una rama de retorno temprano que nunca llamaba al fallback de carga
interna, dejando el sistema permanentemente estancado en capacidad mínima
sin forma de validar si debía escalar. **Corrección**: se añadió la
llamada al fallback también en esa rama — este es, de hecho, el escenario
principal que pidió el profesor (capacidad mínima + sin carga → generar
carga de prueba).
 
### 4.3 Cumplimiento del objetivo de servicio (SLO)
 
El SLO implícito es "mantener CPU promedio del grupo por debajo de 70%
sostenido, sin sobreaprovisionar por debajo de 30% sostenido". Con la
política final (umbrales 70/30, streaks 3/5, cooldown 180s), el sistema
demostró cumplirlo en el escenario de prueba: escaló arriba cuando la
carga fue real y sostenida, y abajo cuando dejó de serlo, sin oscilar
entre ambas decisiones en el mismo cooldown.
 
### 4.4 Capacidad utilizada
 
Durante el experimento, la capacidad osciló entre 1 y 4 instancias (dentro
del rango permitido 1-5).
 
### 4.5 Limitaciones
 
- Métrica única (CPU); no captura memoria, latencia ni errores de la
  aplicación.
- Arquitectura centralizada: un único proceso es punto único de falla del
  control.
- Método reactivo, no predictivo: no anticipa demanda, solo responde una
  vez que ya ocurrió.
### 4.6 Mejoras posibles
 
- Agregar una segunda métrica y
  combinarla con CPU para decisiones más robustas.
- Ejecutar el controller como servicio systemd con `Restart=always` para
  eliminar el punto único de falla operativa.
- Modo proactivo para anticipar demanda en vez
  de solo reaccionar.

---
