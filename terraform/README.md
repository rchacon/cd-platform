# Terraform

AWS infrastructure for this repo, provisioned incrementally by component:
`bootstrap/` (state backend, one-time), `networking/` (this issue, #19),
and eventually `rds/` (#20), `mwaa/` (#21), `cd-api/` (#22).

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.15
- AWS credentials for the target account, with permission to create the
  resources each directory defines (via environment variables, an AWS CLI
  profile, or SSO -- anything the AWS provider's standard credential chain
  picks up)

## `bootstrap/` -- one-time state backend setup

Creates the S3 bucket + DynamoDB table that hold every other directory's
Terraform state. Run once per AWS account, with local state (there's
nothing else yet to store *this* config's state in):

```bash
cd terraform/bootstrap
terraform init
terraform apply
terraform output
```

Note the `state_bucket_name` and `lock_table_name` outputs -- every other
`terraform/*` directory's backend needs them (see below). This directory
isn't touched again as part of normal workflow once it's applied.

## `networking/` -- VPC, subnets, security groups

The shared network layer RDS (#20), MWAA (#21), and cd-api's Lambda (#22)
all provision into.

Backend config is intentionally left empty in `versions.tf` (a bucket name
containing your AWS account ID shouldn't be hardcoded into version-controlled
files). Supply it via a gitignored `backend.hcl`:

```bash
cd terraform/networking
cat > backend.hcl <<EOF
bucket         = "<state_bucket_name from bootstrap output>"
key            = "networking/terraform.tfstate"
region         = "us-west-2"
dynamodb_table = "<lock_table_name from bootstrap output>"
encrypt        = true
EOF

terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

Defaults: `us-west-2`, VPC CIDR `10.0.0.0/16`, 2 AZs, one shared NAT gateway
(cheaper than one per AZ, at the cost of a single point of failure if that
AZ has an outage -- see `variables.tf` for how to change this). Future
`rds/`/`mwaa/`/`cd-api/` directories will read this state's outputs
(`vpc_id`, subnet IDs, security group IDs) via `terraform_remote_state`,
using the same `backend.hcl` pattern with a different `key`.

## Validating without AWS credentials

`terraform fmt -check -recursive` and `terraform validate` (after
`terraform init -backend=false`) don't need real AWS credentials -- they
only check formatting and internal config consistency. `plan`/`apply` do
need credentials, since they call the AWS API.
