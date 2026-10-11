output "api_url" {
  description = "Base URL of the API (HTTP, or HTTPS when certificate_arn is set)"
  value       = var.certificate_arn == null ? "http://${aws_lb.relay.dns_name}" : "https://${aws_lb.relay.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.relay.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.relay.name
}

output "services" {
  value = {
    api        = aws_ecs_service.api.name
    worker     = aws_ecs_service.worker.name
    dispatcher = aws_ecs_service.dispatcher.name
  }
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "github_deploy_role_arn" {
  description = "Put this in the deploy workflow as role-to-assume"
  value       = aws_iam_role.github_deploy.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
