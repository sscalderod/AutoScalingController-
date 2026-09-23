# SI3016 — Cloud Computing — Challenge Based Learning No. 1
# Build Your Own Auto-Scaling Controller

**Estudiante:** Samuel Calderón
**Curso:** SI3016 Cloud Computing — EAFIT
**Repositorio:** este archivo + `autoscaler.py` + `iam_policy.json` (mínimo privilegio)

Este documento cubre los entregables de la Sección 10 y las respuestas a las
preguntas de la Sección 12 del reto.

---

## 0. Clasificación taxonómica (Sección 5 del reto)

Antes de implementar, se clasifica la solución según la taxonomía de
Al-Dhuraibi et al. (*Elasticity in Cloud Computing: State of the Art and
Research Challenges*, 2018):

| Dimensión | Clasificación | ¿Impuesta por el reto o decisión de diseño? |
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

### 1.5 Permisos (principio de mínimo privilegio — Sección 7, punto 9)

El rol de la instancia de control solo tiene:
- `cloudwatch:GetMetricStatistics`, `cloudwatch:GetMetricData` (lectura de métricas)
- `autoscaling:DescribeAutoScalingGroups`, `autoscaling:SetDesiredCapacity` (lectura y ajuste de capacidad, sin permisos de creación/eliminación de ASGs ni de políticas de scaling)
- `ssm:SendCommand`, `ssm:GetCommandInvocation` (solo para el fallback de carga interna)

No tiene permisos de IAM, de red, ni de otros servicios. Ver `iam_policy.json`.

*(Nota: en el AWS Academy Lab usado para las pruebas, el laboratorio impone
`LabRole`/`LabInstanceProfile` con permisos más amplios, ya que el lab no
permite crear roles IAM personalizados. `iam_policy.json` documenta cuál
sería el rol de mínimo privilegio en una cuenta propia.)*

### 1.6 Supuestos y limitaciones

- Se asume que `stress-ng` está disponible o instalable vía `dnf` en las
  instancias del ASG (Amazon Linux 2023).
- Solo se usa CPU como métrica; no se incorpora memoria, latencia ni tasa de
  error (ver Sección 5 de este documento — Pregunta 1 de la Sección 12).
- El controller es centralizado y de punto único de falla: si el proceso
  muere, no hay decisiones nuevas hasta reiniciarlo manualmente (no se
  implementó supervisión tipo systemd con `Restart=always` en las pruebas,
  aunque el diseño lo permite).
- El fallback de carga interna asume que existe al menos una instancia
  `InService` en el ASG; si el ASG está vacío, no hay forma de generar
  carga interna (caso límite no crítico, dado que `MIN_INSTANCES=1`).

---

## 2. Reproducible Implementation (10.2)

### 2.1 Estructura del repositorio

```
/
├── autoscaler.py          # controller completo (un solo archivo)
├── iam_policy.json         # política IAM de mínimo privilegio
├── requirements.txt        # boto3
├── .gitignore               # excluye state.json, decisions.log, credenciales
├── AWS_SETUP_GUIDE.md       # instrucciones de despliegue de infra (consola AWS)
├── RUNBOOK_PRESENTACION.md  # instrucciones de ejecución y generación de carga
└── DELIVERABLES.md          # este documento
```

### 2.2 Infraestructura (definida vía consola AWS, documentada en `AWS_SETUP_GUIDE.md`)

- VPC default (subnets públicas en al menos 2 AZs)
- Security Groups separados: ALB, instancias del ASG, instancia de control
- Launch Template: Amazon Linux 2023, `nginx` + `stress-ng` vía user data
- Target Group + Application Load Balancer (HTTP:80)
- Auto Scaling Group `asg-asc`: min=1, max=5, **sin políticas de scaling de AWS**
- Instancia de control (EC2 separada), acceso vía Session Manager (SSM), sin puertos abiertos

### 2.3 Ejecución

```bash
export AWS_REGION=us-east-1
export ASG_NAME=asg-asc
pip3 install -r requirements.txt
python3 autoscaler.py
```

### 2.4 Generación de carga (load-generation)

Dos mecanismos, documentados en `RUNBOOK_PRESENTACION.md`:

1. **Manual/orgánica**: `stress-ng --cpu 0 --timeout 300s` ejecutado
   directamente en una instancia del ASG vía Session Manager — simula
   carga real de usuarios sin generar tráfico externo hacia el ALB.
2. **Automática (fallback del controller)**: si tras `IDLE_CYCLES_BEFORE_STRESS`
   ciclos no hay carga orgánica, el propio controller dispara `stress-ng`
   vía SSM Run Command — este es el mecanismo pedido explícitamente por el
   profesor en la especificación de la actividad de clase.

### 2.5 Mecanismo de logging de decisiones

`log_decision()` escribe a `decisions.log` una línea JSON por ciclo con:
timestamp, métrica observada, capacidad existente, decisión, justificación
y resultado de la acción — cumple el requisito de explicabilidad de la
Sección 9.

### 2.6 Credenciales

No se almacena ninguna credencial en el repositorio. La autenticación se
resuelve vía el rol IAM de instancia (`boto3` usa las credenciales
temporales inyectadas por el Instance Metadata Service), sin claves de
acceso hardcodeadas.

---

## 3. Experimental Evidence (10.3)

### 3.1 Escenario de prueba

1. Estado inicial: `desired_capacity=1`, CPU en reposo (~0.2-0.3%).
2. Sin carga orgánica, el fallback interno dispara `stress-ng --cpu 0
   --timeout 240s` en la única instancia InService tras 3 ciclos inactivos.
3. La métrica de CPU sube (55% → 95% → 100%) durante 4 ciclos consecutivos.
4. Al tercer ciclo con CPU ≥70%, se ejecuta `INCREASE_CAPACITY`
   (`desired_capacity`: 1 → 2).
5. Posteriormente, con CPU baja sostenida, se ejecuta `REDUCE_CAPACITY`
   (`desired_capacity`: 2 → 1).

### 3.2 Extracto del log de decisiones — Scale-up

```json
{"timestamp": "2026-09-21T15:56:15", "observed_cpu": 0.266, "current_capacity": 1, "decision": "MAINTAIN_CAPACITY", "reason": "CPU=0.3% dentro de rango normal o sin streak suficiente", "action_result": "sin acción sobre infraestructura"}
{"timestamp": "2026-09-21T15:57:15", "observed_cpu": 55.16, "current_capacity": 1, "decision": "MAINTAIN_CAPACITY", "reason": "CPU=55.2% dentro de rango normal o sin streak suficiente", "action_result": "sin acción sobre infraestructura"}
{"timestamp": "2026-09-21T15:58:15", "observed_cpu": 95.12, "current_capacity": 1, "decision": "MAINTAIN_CAPACITY", "reason": "CPU=95.1% dentro de rango normal o sin streak suficiente", "action_result": "sin acción sobre infraestructura"}
{"timestamp": "2026-09-21T16:12:57", "observed_cpu": 100.0, "current_capacity": 1, "decision": "INCREASE_CAPACITY", "reason": "CPU=100.0% >= 70.0% durante 3 ciclos", "action_result": "OK: desired_capacity -> 2"}
```

### 3.3 Manejo de fallos observado (evidencia de la restricción 8 de la Sección 7)

Durante las pruebas, el ASG tenía `MaxSize=1` mal configurado en la
consola. El controller intentó escalar y AWS rechazó la operación; el
controller **no se cayó**, registró el error y continuó operando:

```json
{"timestamp": "2026-09-21T16:12:58", "observed_cpu": 100.0, "current_capacity": 1, "decision": "INCREASE_CAPACITY", "reason": "CPU=100.0% >= 70.0% durante 3 ciclos", "action_result": "ERROR: An error occurred (ValidationError) when calling the SetDesiredCapacity operation: New SetDesiredCapacity value 2 is above max value 1 for the AutoScalingGroup."}
```

Esto es evidencia directa del fail-safe: el error de AWS quedó capturado y
logueado, sin excepción no controlada, y el ciclo siguiente continuó con
normalidad.

### 3.4 Time series

Comparar en CloudWatch (Metrics → EC2 → By Auto Scaling Group →
`CPUUtilization`, period 1 min) contra los timestamps del log anterior
permite reconstruir visualmente la correlación carga → decisión →
capacidad.

---

## 4. Critical Analysis (10.4)

### 4.1 Fortalezas de la política

- **Explicabilidad total**: cada decisión (incluso las de "mantener") queda
  registrada con su justificación exacta, cumpliendo el requisito 7 de la
  Sección 7.
- **Resiliencia a picos transitorios**: la confirmación por ciclos
  consecutivos evita reaccionar a ruido de corta duración.
- **Fail-safe robusto**: se probó en vivo (ver 3.3) que un fallo de AWS no
  interrumpe el loop de control.
- **Aislamiento de responsabilidades**: AWS solo aprovisiona; toda la
  lógica de decisión es propia (cumple restricción 4 de la Sección 7).

### 4.2 Debilidades encontradas durante las pruebas (con evidencia)

**Bug 1 — Duración insuficiente del estrés interno.** La primera versión
usaba `STRESS_DURATION_SECONDS=90`, insuficiente para sostener 3 ciclos de
60s consecutivos por encima del umbral (180s necesarios). El estrés se
apagaba antes de completar el streak, y el sistema nunca llegaba a
`INCREASE_CAPACITY`. **Corrección**: se subió a 240s.

**Bug 2 — El fallback interrumpía streaks legítimos en construcción.** El
contador `idle_cycles` (que dispara el fallback de carga) se incrementaba
incluso mientras se estaba acumulando un streak alto o bajo real (p. ej.
`low_streak=2` de 5 necesarios). Esto causaba que el fallback disparara
carga artificial a mitad de una tendencia real, reiniciándola antes de
completarse — el sistema quedaba oscilando indefinidamente sin nunca
alcanzar 5 ciclos bajos consecutivos. **Corrección**: el fallback ahora
solo se dispara cuando `high_streak == 0 and low_streak == 0` (verdadera
inactividad, no una tendencia parcial).

**Bug 3 — Camino "ya en el mínimo" sin fallback.** Cuando la capacidad ya
estaba en `MIN_INSTANCES` y el streak bajo llegaba a 5, el código entraba
en una rama de retorno temprano que nunca llamaba al fallback de carga
interna, dejando el sistema permanentemente estancado en capacidad mínima
sin forma de validar si debía escalar. **Corrección**: se añadió la
llamada al fallback también en esa rama — este es, de hecho, el escenario
principal que pidió el profesor (capacidad mínima + sin carga → generar
carga de prueba).

Estos tres bugs y sus correcciones están documentados como parte del
proceso de validación de ingeniería, no como fallas ocultadas.

### 4.3 Cumplimiento del objetivo de servicio (SLO)

El SLO implícito es "mantener CPU promedio del grupo por debajo de 70%
sostenido, sin sobreaprovisionar por debajo de 30% sostenido". Con la
política final (umbrales 70/30, streaks 3/5, cooldown 180s), el sistema
demostró cumplirlo en el escenario de prueba: escaló arriba cuando la
carga fue real y sostenida, y abajo cuando dejó de serlo, sin oscilar
entre ambas decisiones en el mismo cooldown.

### 4.4 Capacidad utilizada

Durante el experimento, la capacidad osciló entre 1 y 2 instancias (dentro
del rango permitido 1-5). No se alcanzó el máximo de 5 porque el escenario
de prueba generó carga en una sola instancia a la vez.

### 4.5 Limitaciones

- Métrica única (CPU); no captura memoria, latencia ni errores de la
  aplicación (ver Pregunta 1, Sección 5 de este documento).
- Arquitectura centralizada: un único proceso es punto único de falla del
  control (aunque no de la aplicación, que sigue sirviendo tráfico si el
  controller se cae — solo deja de escalar).
- Método reactivo, no predictivo: no anticipa demanda, solo responde una
  vez que ya ocurrió.

### 4.6 Mejoras posibles

- Agregar una segunda métrica (p. ej. `RequestCountPerTarget` del ALB) y
  combinarla con CPU para decisiones más robustas.
- Ejecutar el controller como servicio systemd con `Restart=always` para
  eliminar el punto único de falla operativa.
- Modo proactivo (time-series forecasting) para anticipar demanda en vez
  de solo reaccionar.

---

## 5. Demonstration (10.5) — checklist para la demo en vivo

1. Mostrar el ASG en consola con capacidad inicial (1 instancia).
2. Mostrar `decisions.log` con `tail -f` en la instancia de control.
3. Generar carga (manual con `stress-ng` o dejar actuar el fallback
   automático).
4. Mostrar en CloudWatch cómo sube `CPUUtilization` en tiempo real.
5. Mostrar en el log cómo, tras el streak de 3 ciclos, aparece
   `INCREASE_CAPACITY` y en consola AWS la nueva instancia lanzándose
   (pestaña "Activity" del ASG).
6. Esperar a que la carga termine y, tras el streak bajo, mostrar
   `REDUCE_CAPACITY` y la instancia terminándose.

---

## 6. Preguntas para la Presentación Final (Sección 12)

**1. ¿Por qué las métricas seleccionadas representan adecuadamente el
estado del sistema, y qué aspectos relevantes no capturan?**

`CPUUtilization` representa directamente la saturación de cómputo del
grupo, que es la causa más común de degradación en aplicaciones sin
cuellos de botella externos (I/O, red, base de datos). Es la métrica que
el propio paper de referencia usa como ejemplo canónico de umbral estático
("if CPU utilization is greater than 80 percent... for 5 minutes"). Sin
embargo, no captura: latencia percibida por el usuario final, tasa de
errores HTTP, saturación de memoria o de I/O de disco/red, ni profundidad
de cola de requests en el load balancer. Una aplicación podría estar
degradada (alta latencia por locking, memory pressure, etc.) sin que la
CPU lo refleje — en ese caso el controller no lo detectaría.

**2. ¿Cuánto tiempo transcurre entre la aparición de una condición de
sobrecarga y la disponibilidad de capacidad adicional?**

Componentes del retraso total:
- Confirmación del streak: hasta 3 ciclos × 60s = **180s** (se necesita
  ver la condición sostenida antes de decidir).
- Ejecución de `set_desired_capacity`: prácticamente inmediata (llamada API).
- Lanzamiento y arranque de la instancia EC2 (AMI boot + user data
  instalando nginx): observado entre **60-120s** adicionales.
- Registro en el Target Group y health check pasando a "healthy": el
  ALB por defecto revisa cada 30s con un umbral de 2-3 chequeos
  exitosos, agregando **~60-90s** más.

**Total estimado: entre 5 y 6.5 minutos** desde que la sobrecarga aparece
hasta que la nueva instancia recibe tráfico real.

**3. ¿Cómo distingue el controller una variación transitoria de un cambio
sostenido en la demanda?**

Mediante la confirmación por ciclos consecutivos: un solo dato por encima
del umbral no dispara ninguna acción; se requiere que el streak
(`high_streak` o `low_streak`) alcance el umbral de confirmación (3 para
subir, 5 para bajar) antes de actuar. Cualquier lectura que caiga en la
zona neutral (30%-70%) reinicia ambos contadores a cero, de modo que un
pico aislado no se acumula con lecturas anteriores.

**4. ¿Qué evita que el controller aumente y reduzca capacidad
repetidamente?**

Dos mecanismos combinados: (a) el cooldown de 180s tras cada acción, que
bloquea cualquier nueva decisión de escalado inmediatamente después de una
anterior, dando tiempo a que el sistema se estabilice; y (b) los umbrales
asimétricos con zona neutral entre 30% y 70% — el sistema tendría que
cruzar una banda amplia de 40 puntos porcentuales para pasar de un
extremo a otro, lo que en la práctica es poco frecuente salvo con cambios
reales de carga.

**5. ¿Qué sucede cuando falla una medición o una acción de
infraestructura?**

Medición ausente: si CloudWatch no devuelve datapoints, `get_average_cpu`
retorna `None`; el motor de decisión lo trata como un caso fail-safe
explícito — nunca actúa a ciegas, mantiene la capacidad actual y registra
la ausencia de métrica en el log. Acción fallida: si `set_desired_capacity`
lanza una excepción de AWS (ver evidencia en 3.3), el error se captura,
se registra en el log de decisiones como parte del `action_result`, y el
loop principal continúa en el siguiente ciclo sin caerse (el `while True`
en `run_forever` envuelve cada ciclo en `try/except` general adicional).

**6. ¿En qué escenario el controller tomó una decisión incorrecta o
tardía?**

Durante las pruebas se identificaron dos casos concretos (documentados en
la sección 4.2 de este documento): (a) con `STRESS_DURATION_SECONDS=90`,
el sistema nunca llegaba a escalar porque la carga de prueba se apagaba
antes de completar el streak de confirmación — una decisión "tardía" en el
sentido de que nunca llegaba a tomarse a pesar de haber señal real; y (b)
el fallback de carga interna interrumpía streaks de scale-down legítimos
ya en curso, generando oscilación indefinida sin llegar nunca a
`REDUCE_CAPACITY`. Ambos se corrigieron y quedaron documentados con
evidencia de logs antes/después.

**7. ¿Cuál fue el costo de recursos de mantener el objetivo de servicio?**

En el escenario de prueba, la capacidad máxima alcanzada fue 2 instancias
(sobre un máximo permitido de 5), durante el tiempo que duró la carga
sintética (~4 minutos) más el cooldown posterior (180s) antes de poder
reducir. El costo adicional fue equivalente a mantener 1 instancia extra
tipo `t2.micro`/`t3.micro` durante aproximadamente 7-8 minutos en total.

**8. Si dos controllers mantienen el mismo nivel de servicio pero uno usa
más instancias o realiza más cambios de capacidad, ¿cuál es mejor, y cómo
se puede demostrar?**

Es mejor el que logra el mismo SLO con **menos instancias-hora acumuladas
y menos transiciones de capacidad**, porque ambos factores son proxies
directos de costo: instancias-hora es el costo de cómputo directo, y cada
transición de capacidad tiene un costo operacional (riesgo de
inestabilidad, tiempo de arranque desperdiciado si se revierte pronto, y
en algunos proveedores costo de facturación por hora parcial). Esto se
demuestra empíricamente corriendo ambos controllers contra el **mismo
patrón de carga reproducible** y comparando dos métricas del log de
decisiones: (a) el área bajo la curva de `capacidad(t)` a lo largo del
experimento (instancias-hora totales), y (b) el conteo total de eventos
`INCREASE_CAPACITY` + `REDUCE_CAPACITY`. El controller con menor valor en
ambas métricas, para el mismo cumplimiento de SLO (mismo % de tiempo con
CPU dentro del rango objetivo), es el más eficiente.
