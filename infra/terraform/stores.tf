# Object storage, container registry, database, cache, and the database secret.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "data" {
  bucket = "relay-data-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  cors_rule {
    # Browsers upload with a presigned POST and download with a presigned GET.
    allowed_methods = ["GET", "POST"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {
      prefix = "tenants/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_ecr_repository" "relay" {
  name                 = "relay"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "relay" {
  repository = aws_ecr_repository.relay.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 50 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 50
      }
      action = { type = "expire" }
    }]
  })
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "relay" {
  name       = "relay-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "relay" {
  identifier                   = "relay-${var.environment}"
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = 50
  max_allocated_storage        = 500
  storage_encrypted            = true
  db_name                      = "relay"
  username                     = "relay"
  password                     = random_password.db.result
  db_subnet_group_name         = aws_db_subnet_group.relay.name
  vpc_security_group_ids       = [aws_security_group.database.id]
  multi_az                     = var.db_multi_az
  backup_retention_period      = 7
  deletion_protection          = var.db_deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "relay-${var.environment}-final"
  auto_minor_version_upgrade   = true
  performance_insights_enabled = true
  publicly_accessible          = false
}

resource "aws_elasticache_subnet_group" "relay" {
  name       = "relay-cache"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "relay" {
  cluster_id           = "relay-${var.environment}"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  port                 = 6379
  parameter_group_name = "default.redis7"
  subnet_group_name    = aws_elasticache_subnet_group.relay.name
  security_group_ids   = [aws_security_group.redis.id]
}

# The full DATABASE_URL lives in Secrets Manager. Tasks read it at start-up. It never sits in an image.
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "relay/${var.environment}/database-url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://relay:${random_password.db.result}@${aws_db_instance.relay.address}:5432/relay"
}
