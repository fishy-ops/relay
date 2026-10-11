# ECS Fargate: API behind an ALB, workers that autoscale on backlog per worker, and a dispatcher.
# Deployments use the ECS circuit breaker, so a failing release rolls back automatically.

locals {
  image     = "${aws_ecr_repository.relay.repository_url}:${var.image_tag}"
  log_group = "/relay/${var.environment}"
  common_env = [
    { name = "S3_BUCKET", value = aws_s3_bucket.data.bucket },
    { name = "AWS_REGION", value = var.region },
    { name = "REDIS_URL", value = "redis://${aws_elasticache_cluster.relay.cache_nodes[0].address}:6379/0" },
    { name = "LEASE_SECONDS", value = "30" },
    { name = "MAX_ATTEMPTS", value = "5" },
    { name = "CLOUDWATCH_METRICS", value = "1" },
    { name = "ALLOW_PRIVATE_WEBHOOKS", value = tostring(var.allow_private_webhooks) },
  ]
  database_secret = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
  ]
}

resource "aws_cloudwatch_log_group" "relay" {
  name              = local.log_group
  retention_in_days = 30
}

resource "aws_ecs_cluster" "relay" {
  name = "relay-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "relay-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.image
    essential    = true
    command      = ["uvicorn", "relay.api:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.common_env
    secrets      = local.database_secret
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = local.log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "relay-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.image
    essential   = true
    command     = ["python", "-m", "relay.worker"]
    environment = concat(local.common_env, [{ name = "WORKER_POLL_SECONDS", value = "0.5" }])
    secrets     = local.database_secret
    stopTimeout = 120
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = local.log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "dispatcher" {
  family                   = "relay-dispatcher"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "dispatcher"
    image       = local.image
    essential   = true
    command     = ["python", "-m", "relay.dispatcher"]
    environment = local.common_env
    secrets     = local.database_secret
    stopTimeout = 60
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = local.log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = "dispatcher"
      }
    }
  }])
}

resource "aws_lb" "relay" {
  name               = "relay-${var.environment}"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 60
}

resource "aws_lb_target_group" "api" {
  name        = "relay-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.relay.id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.relay.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.certificate_arn == null ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }

  dynamic "default_action" {
    for_each = var.certificate_arn == null ? [] : [1]
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_arn == null ? 0 : 1
  load_balancer_arn = aws_lb.relay.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "relay-api"
  cluster         = aws_ecs_cluster.relay.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "relay-worker"
  cluster         = aws_ecs_cluster.relay.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.background.id]
    assign_public_ip = false
  }

  # Autoscaling owns desired_count after the first deploy.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_ecs_service" "dispatcher" {
  name            = "relay-dispatcher"
  cluster         = aws_ecs_cluster.relay.id
  task_definition = aws_ecs_task_definition.dispatcher.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.background.id]
    assign_public_ip = false
  }
}

# Scale workers on backlog per running worker: queued jobs divided by RunningTaskCount
# from Container Insights. This is a metric-math target-tracking policy.
resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.relay.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
}

resource "aws_appautoscaling_policy" "worker_backlog" {
  name               = "relay-worker-backlog"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.backlog_per_worker_target
    scale_in_cooldown  = 120
    scale_out_cooldown = 30

    customized_metric_specification {
      metrics {
        id          = "queued"
        label       = "Queued jobs"
        return_data = false
        metric_stat {
          metric {
            namespace   = "Relay"
            metric_name = "QueueDepth"
          }
          stat = "Average"
        }
      }
      metrics {
        id          = "workers"
        label       = "Running worker tasks"
        return_data = false
        metric_stat {
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.relay.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.worker.name
            }
          }
          stat = "Average"
        }
      }
      metrics {
        id          = "backlog_per_worker"
        label       = "Backlog per worker"
        expression  = "queued / MAX(workers)"
        return_data = true
      }
    }
  }
}
