mock_provider "aws" {
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:us-east-2:402365884764:cluster/mock"
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-2.amazonaws.com/id/MOCK" }] }]
      certificate_authority = [{ data = "bW9jaw==" }]
      endpoint              = "https://mock.eks.amazonaws.com"
    }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-2:402365884764:listener/app/x/1/2" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_sns_topic" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_lb" }
  }
  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_wafv2_web_acl" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_iam_role" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_iam_policy" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_lb_target_group" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_cognito_user_pool" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_dynamodb_table" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_secretsmanager_secret" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:iam::402365884764:mock/aws_kms_key" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "arn:aws:acm:us-east-2:402365884764:certificate/mock", insecure_value = "arn:aws:acm:us-east-2:402365884764:certificate/mock" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "402365884764", arn = "arn:aws:iam::402365884764:user/x" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-2a", "us-east-2b", "us-east-2c"] }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-2", region = "us-east-2" }
  }
}
mock_provider "tls" {}
mock_provider "time" {}
mock_provider "null" {}
mock_provider "cloudinit" {}

run "staging_api_key" {
  command = plan
  variables {
    environment                  = "staging"
    vpc_cidr                     = "10.0.0.0/16"
    nat_per_az                   = false
    alb_deletion_protection      = false
    dynamodb_deletion_protection = false
    auth_mode                    = "api_key"
  }
  assert {
    condition     = length(aws_cognito_user_pool.api) == 0
    error_message = "staging no debe crear Cognito"
  }
  assert {
    condition     = length([for a in aws_lb_listener_rule.api.action : a if a.type == "jwt-validation"]) == 0
    error_message = "staging no valida JWT"
  }
}

run "slo_alarms" {
  command = plan
  variables {
    environment                  = "staging"
    vpc_cidr                     = "10.0.0.0/16"
    nat_per_az                   = false
    alb_deletion_protection      = false
    dynamodb_deletion_protection = false
    auth_mode                    = "api_key"
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.slo) == 15 && length(aws_cloudwatch_composite_alarm.slo_page) == 6
    error_message = "3 SLIs x (2 niveles x 2 ventanas + ticket) y 6 compuestas"
  }
  assert {
    condition     = abs(aws_cloudwatch_metric_alarm.slo["availability-fast-long"].threshold - 1.44) < 0.0001 && alltrue([for q in aws_cloudwatch_metric_alarm.slo["availability-fast-long"].metric_query : q.expression != null || q.metric[0].period == 3600])
    error_message = "disponibilidad rápida: 14,4 x 0,1 % en 1 h"
  }
  assert {
    condition     = abs(aws_cloudwatch_metric_alarm.slo["latency-slow-short"].threshold - 6) < 0.0001
    error_message = "latencia lenta: 6 x 1 %"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.slo["freshness-ticket"].evaluation_periods == 3 && aws_cloudwatch_metric_alarm.slo["freshness-ticket"].treat_missing_data == "breaching"
    error_message = "frescura: ticket a 3 días y la falta de datos cuenta como falla"
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.slo["availability-fast-short"].alarm_actions) == 0
    error_message = "las ventanas sueltas no avisan; avisa la compuesta"
  }
}

run "prod_jwt" {
  command = plan
  variables {
    environment   = "prod"
    vpc_cidr      = "10.1.0.0/16"
    auth_mode     = "jwt"
    api_consumers = ["tablero-plataforma"]
  }
  assert {
    condition     = length(aws_cognito_user_pool.api) == 1
    error_message = "prod debe crear el pool"
  }
  assert {
    condition     = toset(keys(aws_cognito_user_pool_client.consumer)) == toset(["tablero-plataforma", "pipeline-verify"])
    error_message = "clientes esperados"
  }
  assert {
    condition     = [for a in aws_lb_listener_rule.api.action : a.type] == ["jwt-validation", "forward"]
    error_message = "la regla debe validar el JWT antes de enviar a los pods"
  }
  assert {
    condition     = aws_ssm_parameter.contract["auth/mode"].value == "jwt"
    error_message = "contrato auth/mode"
  }
}
