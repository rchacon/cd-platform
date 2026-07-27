output "vpc_id" {
  description = "VPC ID. Consumed by #20 (RDS), #21 (Airflow on EC2), #22 (cd-api Lambda)."
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs -- RDS, the Airflow EC2 instance, and cd-api's Lambda all live here."
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "Public subnet IDs -- only the NAT gateway(s) live here."
  value       = module.vpc.public_subnets
}

output "rds_security_group_id" {
  description = "Security group to attach to the RDS instance (#20)."
  value       = aws_security_group.rds.id
}

output "airflow_security_group_id" {
  description = "Security group to attach to the Airflow EC2 instance (#21)."
  value       = aws_security_group.airflow.id
}

output "lambda_security_group_id" {
  description = "Security group to attach to cd-api's Lambda (#22)."
  value       = aws_security_group.lambda.id
}
