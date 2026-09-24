# AWS Setup Guide

Guía completa: infraestructura desde cero en la consola de AWS, ejecución
del controller.

## 0. Prerrequisito: VPC

Crea una VPC personalizada con dos zonas de disponiblidad y subredes públicas.

## 1. Security Groups (VPC → Security Groups)

**alb-sg**
- Inbound: HTTP (80) desde `0.0.0.0/0`
- Outbound: todo (default)

**instance-sg**
- Inbound: HTTP (80) desde el Security Group `alb-sg` (no desde `0.0.0.0/0`)
- Outbound: todo (default)

**controller-sg**
- Sin reglas inbound (no se necesita acceso remoto por IP; la conexión es
  vía SSM Session Manager, sin abrir puertos)
- Outbound: todo (default)

## 2. IAM Role / Instance Profile

Usar **`LabInstanceProfile`**, tiene permisos suficientes.

## 3. Launch Template (EC2 → Launch Templates → Create launch template)

- Nombre: `launch-template`
- AMI: Amazon Linux 2023 (viene con el agente SSM preinstalado)
- Instance type: `t2.micro`
- Key pair: no es necesario si vas a usar Session Manager
- Security group: `instance-sg`
- IAM instance profile: `LabInstanceProfile` (o el rol de mínimo privilegio si no estás en un lab)
- **Monitoring**: activa "Detailed CloudWatch monitoring".
- User data (sección "Detalles avanzados" → "Datos de usuario", al final
  del formulario):

```bash
#!/bin/bash
dnf install -y nginx stress-ng
systemctl enable nginx
systemctl start nginx
echo "OK $(hostname)" > /usr/share/nginx/html/index.html
```

## 4. Target Group (EC2 → Target Groups → Create target group)

- Tipo: Instances
- Protocolo: HTTP, puerto 80
- Health check path: `/`
- VPC: la misma que usarás para el ALB y el ASG

## 5. Application Load Balancer (EC2 → Load Balancers → Create)

- Tipo: Application Load Balancer, Internet-facing
- Listener: HTTP 80 → forward al Target Group del paso 4
- Security group: `alb-sg`
- Selecciona al menos 2 subnets en AZs distintas

## 6. Auto Scaling Group (EC2 → Auto Scaling Groups → Create)

- Nombre: `asg-asc` 
- Launch template: `launch-template`
- VPC/subnets: mismas que el ALB
- Attach to an existing load balancer → selecciona el Target Group del paso 4
- Group size: Desired=1, **Min=1, Max=5** 
- **Scaling policies: selecciona "None"** 
- Health checks: activa "ELB health checks" además del de EC2

## 7. Instancia de control (EC2 → Launch Instance)

- Nombre: `controller`
- AMI: Amazon Linux 2023
- Instance type: `t2.micro`
- Security group: `controller-sg`
- IAM instance profile: `LabInstanceProfile`
- Sin key pair necesario (se usa Session Manager)

## 8. Conectarte y correr el controller

EC2 → selecciona `controller` → **Connect** → **Session Manager** →
Connect 

Dentro de la sesión:

```bash
cd ~
sudo dnf install -y python3 python3-pip
pip3 install boto3 --user
sudo mkdir -p /var/lib/autoscaler /var/log/autoscaler
sudo chown $(whoami):$(whoami) /var/lib/autoscaler /var/log/autoscaler
```

Sube `autoscaler.py` a la instancia pegando su contenido completo con un
heredoc:

```bash
cat > autoscaler.py << 'PYEOF'
# ... pegar aquí el contenido completo de autoscaler.py ...
PYEOF
wc -l autoscaler.py 
```

Ejecutar:

```bash
export AWS_REGION=us-east-1
export ASG_NAME=asg-asc
python3 autoscaler.py
```

Verificar el log de decisiones:

```bash
tail -f /var/log/autoscaler/decisions.log
```

## 9. Dejar que el controller escale por sí solo

No se genera carga manual. El controller ya trae su propio mecanismo para
validar el escalado sin tráfico externo: si no hay carga orgánica durante
`IDLE_CYCLES_BEFORE_STRESS` ciclos (3 por defecto, ~3 min), dispara
internamente `stress-ng` vía SSM Run Command en una instancia del ASG —
esto sube la CPU real, y si se sostiene 3 ciclos ≥70%, el controller
decide `INCREASE_CAPACITY` de forma normal.

**Para ver el scale-up**: simplemente déjalo corriendo y espera. No hace
falta ningún comando adicional — con `python3 autoscaler.py` (paso 8) es
suficiente. En unos 6-8 minutos debería aparecer `INCREASE_CAPACITY` en
`decisions.log`.

**Para ver el scale-down después**: una vez que el estrés interno termina
y la CPU deja de tener cambios (vuelve a valores bajos y estables), el
controller necesita 5 ciclos bajos consecutivos seguidos para reducir
capacidad. Baja temporalmente ese umbral:

```bash
# Ctrl+C para detener el controller si sigue corriendo en primer plano
sed -i 's/CONSECUTIVE_LOW_FOR_SCALE_DOWN = 5/CONSECUTIVE_LOW_FOR_SCALE_DOWN = 2/' autoscaler.py
python3 autoscaler.py
```

Con esto, 2 ciclos seguidos de CPU baja (~2 min) bastan para ver
`REDUCE_CAPACITY`. Es un ajuste de parámetro de la propia política, no
carga externa ni manipulación de datos:

```bash
sed -i 's/CONSECUTIVE_LOW_FOR_SCALE_DOWN = 2/CONSECUTIVE_LOW_FOR_SCALE_DOWN = 5/' autoscaler.py
```
