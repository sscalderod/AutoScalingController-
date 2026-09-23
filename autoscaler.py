"""
(config + cloudwatch + estado + actuador + 
estrés interno + motor de decisión + loop principal)

Ciclo: observe -> analyze -> decide -> act -> log -> sleep.

Requiere: pip3 install boto3
Ejecutar:
    export AWS_REGION=us-east-1
    export ASG_NAME=asg-asc
    python3 autoscaler.py
"""

import os
import json
import time
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError


#configuracion parametros 

REGION = os.getenv("AWS_REGION", "us-east-1")
ASG_NAME = os.getenv("ASG_NAME", "si3016-asg")

MIN_INSTANCES = 1
MAX_INSTANCES = 5

#metricas observadas
METRIC_NAMESPACE = "AWS/EC2"
METRIC_NAME = "CPUUtilization"
METRIC_STAT = "Average"
METRIC_PERIOD_SECONDS = 60
METRIC_LOOKBACK_MINUTES = 5

#decision
CPU_UPPER_THRESHOLD = 70.0
CPU_LOWER_THRESHOLD = 30.0
CONSECUTIVE_HIGH_FOR_SCALE_UP = 3
CONSECUTIVE_LOW_FOR_SCALE_DOWN = 5
SCALE_STEP = 1

#loop
EVALUATION_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 180

MAX_CONSECUTIVE_MISSING_METRICS = 3

#carga interna
IDLE_CYCLES_BEFORE_STRESS = 3
#240s cubre CONSECUTIVE_HIGH_FOR_SCALE_UP ciclos de evaluación
#para que el estrés interno alcance a sostener el streak alto necesario.
STRESS_DURATION_SECONDS = 240
STRESS_COOLDOWN_SECONDS = 240

#persistencia y logs
STATE_FILE = os.getenv("STATE_FILE", "/var/lib/autoscaler/state.json")
DECISION_LOG_FILE = os.getenv("DECISION_LOG_FILE", "/var/log/autoscaler/decisions.log")

MAINTAIN = "MAINTAIN_CAPACITY"
INCREASE = "INCREASE_CAPACITY"
REDUCE = "REDUCE_CAPACITY"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("autoscaler")

_cw_client = boto3.client("cloudwatch", region_name=REGION)
_asg_client = boto3.client("autoscaling", region_name=REGION)
_ssm_client = boto3.client("ssm", region_name=REGION)

#cloudwatch metricas

def get_average_cpu(asg_name: str = ASG_NAME):
    """Último valor de CPUUtilization promedio del ASG, o None si no hay datos."""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=METRIC_LOOKBACK_MINUTES)

    try:
        response = _cw_client.get_metric_statistics(
            Namespace=METRIC_NAMESPACE,
            MetricName=METRIC_NAME,
            Dimensions=[{"Name": "AutoScalingGroupName", "Value": asg_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=METRIC_PERIOD_SECONDS,
            Statistics=[METRIC_STAT],
        )
    except Exception as exc:  
        logger.error("Fallo consultando CloudWatch: %s", exc)
        return None

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        logger.warning("Sin datapoints de %s para %s", METRIC_NAME, asg_name)
        return None

    latest = max(datapoints, key=lambda dp: dp["Timestamp"])
    value = latest[METRIC_STAT]
    logger.info("CPUUtilization promedio (%s) = %.2f%%", latest["Timestamp"], value)
    return value


#ajustar capacidad del asg

def get_current_capacity(asg_name: str = ASG_NAME) -> int:
    response = _asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        raise RuntimeError(f"ASG no encontrado: {asg_name}")
    return groups[0]["DesiredCapacity"]


def set_capacity(new_capacity: int, asg_name: str = ASG_NAME) -> str:
    new_capacity = max(MIN_INSTANCES, min(MAX_INSTANCES, new_capacity))
    try:
        _asg_client.set_desired_capacity(
            AutoScalingGroupName=asg_name,
            DesiredCapacity=new_capacity,
            HonorCooldown=False, 
        )
        return f"OK: desired_capacity -> {new_capacity}"
    except ClientError as exc:
        logger.error("Fallo al ajustar capacidad del ASG: %s", exc)
        return f"ERROR: {exc}"


def list_running_instance_ids(asg_name: str = ASG_NAME) -> list:
    response = _asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        return []
    return [
        inst["InstanceId"]
        for inst in groups[0]["Instances"]
        if inst["LifecycleState"] == "InService"
    ]


#carga ssm 

_STRESS_COMMAND = (
    f"which stress-ng || sudo yum install -y stress-ng; "
    f"stress-ng --cpu 0 --timeout {STRESS_DURATION_SECONDS}s"
)


def trigger_internal_stress() -> str:
    #envia estres ssm Run Command
    instance_ids = list_running_instance_ids()
    if not instance_ids:
        return "SKIPPED: no hay instancias InService para aplicar estrés"

    target_instance = instance_ids[0]
    try:
        response = _ssm_client.send_command(
            InstanceIds=[target_instance],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [_STRESS_COMMAND]},
            TimeoutSeconds=STRESS_DURATION_SECONDS + 30,
        )
        command_id = response["Command"]["CommandId"]
        logger.info("Estrés interno enviado a %s (CommandId=%s)", target_instance, command_id)
        return f"OK: stress enviado a {target_instance} (CommandId={command_id})"
    except ClientError as exc:
        logger.error("Fallo enviando comando SSM: %s", exc)
        return f"ERROR: {exc}"


#persistencia json

_DEFAULT_STATE = {
    "high_streak": 0,
    "low_streak": 0,
    "idle_cycles": 0,
    "missing_metric_streak": 0,
    "last_action_timestamp": None,
    "last_stress_timestamp": None,
}


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(_DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception as exc:  
        logger.error("No se pudo leer el estado (%s), se reinicia en limpio", exc)
        return dict(_DEFAULT_STATE)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_FILE)


#log decisiones

def log_decision(cpu_value, current_capacity, decision, reason, action_result) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "observed_cpu": cpu_value,
        "current_capacity": current_capacity,
        "decision": decision,
        "reason": reason,
        "action_result": action_result,
    }
    os.makedirs(os.path.dirname(DECISION_LOG_FILE), exist_ok=True)
    with open(DECISION_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Decisión=%s | CPU=%s | capacidad=%s | razón=%s | resultado=%s",
                decision, cpu_value, current_capacity, reason, action_result)


#decisiones

def _in_cooldown(state: dict) -> bool:
    last_action = state.get("last_action_timestamp")
    if last_action is None:
        return False
    return (time.time() - last_action) < COOLDOWN_SECONDS


def _maybe_trigger_internal_stress(state: dict) -> None:
    if state["idle_cycles"] < IDLE_CYCLES_BEFORE_STRESS:
        return
    last_stress = state.get("last_stress_timestamp")
    if last_stress is not None and (time.time() - last_stress) < STRESS_COOLDOWN_SECONDS:
        logger.info("Estrés interno en cooldown, se omite este ciclo")
        return
    result = trigger_internal_stress()
    logger.info("Fallback de carga interna disparado tras %s ciclos inactivos: %s",
                state["idle_cycles"], result)
    state["last_stress_timestamp"] = time.time()
    state["idle_cycles"] = 0


def evaluate_and_act(cpu_value, state: dict) -> tuple:
    current_capacity = get_current_capacity()

    if cpu_value is None:
        state["missing_metric_streak"] += 1
        reason = (f"Métrica no disponible ({state['missing_metric_streak']} "
                  f"ciclos seguidos); se mantiene capacidad por seguridad")
        state["idle_cycles"] += 1
        _maybe_trigger_internal_stress(state)
        return MAINTAIN, reason, "sin acción sobre infraestructura", state

    state["missing_metric_streak"] = 0

    if _in_cooldown(state):
        reason = "Cooldown activo tras la última acción; se mantiene capacidad"
        state["idle_cycles"] += 1
        return MAINTAIN, reason, "sin acción sobre infraestructura", state

    #actualiza streaks
    if cpu_value >= CPU_UPPER_THRESHOLD:
        state["high_streak"] += 1
        state["low_streak"] = 0
    elif cpu_value <= CPU_LOWER_THRESHOLD:
        state["low_streak"] += 1
        state["high_streak"] = 0
    else:
        state["high_streak"] = 0
        state["low_streak"] = 0

    if state["high_streak"] >= CONSECUTIVE_HIGH_FOR_SCALE_UP:
        if current_capacity >= MAX_INSTANCES:
            reason = f"CPU={cpu_value:.1f}% sostenido, pero ya en el máximo ({MAX_INSTANCES})"
            state["idle_cycles"] += 1
            return MAINTAIN, reason, "sin acción sobre infraestructura", state

        action_result = set_capacity(current_capacity + SCALE_STEP)
        reason = f"CPU={cpu_value:.1f}% >= {CPU_UPPER_THRESHOLD}% durante {state['high_streak']} ciclos"
        state["high_streak"] = 0
        state["idle_cycles"] = 0
        state["last_action_timestamp"] = time.time()
        return INCREASE, reason, action_result, state

    if state["low_streak"] >= CONSECUTIVE_LOW_FOR_SCALE_DOWN:
        if current_capacity <= MIN_INSTANCES:
            reason = f"CPU={cpu_value:.1f}% bajo, pero ya en el mínimo ({MIN_INSTANCES})"
            state["idle_cycles"] += 1
            _maybe_trigger_internal_stress(state)
            return MAINTAIN, reason, "sin acción sobre infraestructura", state

        action_result = set_capacity(current_capacity - SCALE_STEP)
        reason = f"CPU={cpu_value:.1f}% <= {CPU_LOWER_THRESHOLD}% durante {state['low_streak']} ciclos"
        state["low_streak"] = 0
        state["idle_cycles"] = 0
        state["last_action_timestamp"] = time.time()
        return REDUCE, reason, action_result, state

    reason = f"CPU={cpu_value:.1f}% dentro de rango normal o sin streak suficiente"
    if state["high_streak"] == 0 and state["low_streak"] == 0:
        state["idle_cycles"] += 1
        _maybe_trigger_internal_stress(state)
    return MAINTAIN, reason, "sin acción sobre infraestructura", state


#loop principal

def run_single_cycle():
    state = load_state()
    cpu_value = get_average_cpu()

    try:
        current_capacity_before = get_current_capacity()
    except Exception as exc:  
        logger.error("No se pudo leer capacidad actual: %s", exc)
        current_capacity_before = None

    decision, reason, action_result, state = evaluate_and_act(cpu_value, state)

    log_decision(cpu_value, current_capacity_before, decision, reason, action_result)
    save_state(state)


def run_forever():
    logger.info("Iniciando Auto-Scaling Controller para ASG=%s", ASG_NAME)
    while True:
        try:
            run_single_cycle()
        except Exception as exc:  
            logger.exception("Ciclo falló inesperadamente: %s", exc)
        time.sleep(EVALUATION_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        logger.info("Controller detenido manualmente")
        sys.exit(0)