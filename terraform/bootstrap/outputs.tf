# These values are what every other terraform/ directory's backend "s3"
# block should hardcode -- Terraform backend blocks can't reference other
# resources/outputs, so this is documentation of the literal values to
# copy, not something consumed programmatically.

output "state_bucket_name" {
  description = "S3 bucket holding Terraform state for the rest of terraform/."
  value       = aws_s3_bucket.terraform_state.bucket
}

output "lock_table_name" {
  description = "DynamoDB table used for Terraform state locking."
  value       = aws_dynamodb_table.terraform_lock.name
}
