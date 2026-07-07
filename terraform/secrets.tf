# DATABASE_URL lives only in Secrets Manager (and, unavoidably, tfstate —
# which is why the state bucket is private + encrypted). Never a Terraform
# variable, never in GitHub.

resource "random_password" "db" {
  length = 32
  # Alphanumeric only so the password needs no URL-encoding inside DATABASE_URL.
  special = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name = "${local.name}/database-url"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id

  # postgresql+psycopg:// — the app pins SQLAlchemy to the psycopg3 driver.
  secret_string = "postgresql+psycopg://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:5432/${var.db_name}"
}
