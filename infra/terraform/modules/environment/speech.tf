# Secret values are populated by the operator, never passed through Terraform state.
variable "enable_speech" {
  type        = bool
  default     = false
  description = "Inject an operator-populated Deepgram key into the API only."
}

resource "aws_secretsmanager_secret" "speech" {
  count                   = var.enable_speech ? 1 : 0
  name                    = "${local.name}/speech/deepgram"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 7
}

resource "aws_iam_role_policy" "speech_secret" {
  count = var.enable_speech ? 1 : 0
  role  = aws_iam_role.execution["api"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.speech[0].arn },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.data.arn,
    Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" } } }
  ] })
}
