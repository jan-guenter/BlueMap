#!/usr/bin/env bash
set -euo pipefail

namespace="${NAMESPACE:-minecraft}"
cluster_name="${CLUSTER_NAME:-bluemap-perf-postgres}"
secret_name="${SECRET_NAME:-bluemap-perf-postgres-tls}"
ca_secret_name="${CA_SECRET_NAME:-bluemap-perf-postgres-ca}"
experiment_id="${EXPERIMENT_ID:-bootstrap}"

work_dir="$(mktemp -d)"
cleanup() {
  rm -f \
    "${work_dir}/ca.key" \
    "${work_dir}/ca.crt" \
    "${work_dir}/server.key" \
    "${work_dir}/server.csr" \
    "${work_dir}/server.crt"
  rmdir "${work_dir}"
}
trap cleanup EXIT

openssl genrsa -out "${work_dir}/ca.key" 3072
openssl req -x509 -new -sha256 \
  -key "${work_dir}/ca.key" \
  -days 7 \
  -subj "/CN=BlueMap performance PostgreSQL CA" \
  -out "${work_dir}/ca.crt"

openssl genrsa -out "${work_dir}/server.key" 3072
openssl req -new -sha256 \
  -key "${work_dir}/server.key" \
  -subj "/CN=${cluster_name}.${namespace}.svc.cluster.local" \
  -out "${work_dir}/server.csr"

openssl x509 -req -sha256 \
  -in "${work_dir}/server.csr" \
  -CA "${work_dir}/ca.crt" \
  -CAkey "${work_dir}/ca.key" \
  -set_serial 1 \
  -days 7 \
  -extfile <(printf '%s\n' \
    "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" \
    "subjectAltName=DNS:${cluster_name},DNS:${cluster_name}.${namespace},DNS:${cluster_name}.${namespace}.svc,DNS:${cluster_name}.${namespace}.svc.cluster.local") \
  -out "${work_dir}/server.crt"

kubectl -n "${namespace}" create secret tls "${secret_name}" \
  --cert="${work_dir}/server.crt" \
  --key="${work_dir}/server.key" \
  --dry-run=client \
  --output=yaml |
  kubectl apply -f -

kubectl -n "${namespace}" create secret generic "${ca_secret_name}" \
  --from-file=ca.crt="${work_dir}/ca.crt" \
  --dry-run=client \
  --output=yaml |
  kubectl apply -f -

kubectl -n "${namespace}" label secret "${secret_name}" "${ca_secret_name}" \
  app.kubernetes.io/part-of=bluemap-web-performance \
  "bluemap.guenter.cloud/experiment-id=${experiment_id}" \
  --overwrite
