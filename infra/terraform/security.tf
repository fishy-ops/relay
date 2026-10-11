# Security groups, least privilege: each tier only accepts traffic from the tier in front.

resource "aws_security_group" "alb" {
  name        = "relay-alb"
  description = "Public HTTP(S) to the load balancer"
  vpc_id      = aws_vpc.relay.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "api" {
  name        = "relay-api"
  description = "API tasks accept traffic from the ALB only"
  vpc_id      = aws_vpc.relay.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Workers and dispatchers make outbound calls only (webhooks, S3, CloudWatch).
resource "aws_security_group" "background" {
  name        = "relay-background"
  description = "Worker and dispatcher tasks. No inbound traffic."
  vpc_id      = aws_vpc.relay.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name        = "relay-database"
  description = "Postgres reachable from the application tiers only"
  vpc_id      = aws_vpc.relay.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id, aws_security_group.background.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "relay-redis"
  description = "Redis reachable from the application tiers only"
  vpc_id      = aws_vpc.relay.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id, aws_security_group.background.id]
  }
}
