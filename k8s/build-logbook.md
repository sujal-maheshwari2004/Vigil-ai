# AI Health Guardian Build Logbook

This file is the running historical log for the project build. It records the major commands used, what they were intended to do, the issues encountered, and how those issues were resolved.

It is meant to grow across all build days, not just Day 2.

## How to Use This Logbook

Each day should capture:

- the goal for the day
- the commands that were run
- why those commands were used
- the outputs or behavior that mattered
- any errors or blockers
- the fix that resolved the issue

## Day 1: Foundations

### Goal

Set up the initial local development foundation:

- FastAPI gateway
- inference service
- Dockerfiles for both services
- Docker Compose wiring
- end-to-end local verification

### Relevant Project State

The repository included:

- `gateway/`
- `inference_service/`
- `docker-compose.yml`
- workspace configuration in the root `pyproject.toml`

### Docker Compose Verification

### Commands

```powershell
curl.exe http://localhost:8001/health
curl.exe http://localhost:8000/health
```

### Why

These commands verified that both services were up and reachable locally:

- `inference_service` on port `8001`
- `gateway` on port `8000`

### Result

Both health endpoints returned a healthy response.

### Query Path Verification

### Initial Attempt

```powershell
curl.exe -X POST http://localhost:8000/query -H "Content-Type: application/json" -d "{\"text\":\"What is hypertension?\",\"top_k\":3}"
```

### Why

This was used to validate the Day 1 exit condition from the PRD: the gateway should return a valid response for `POST /query`.

### Error Observed

PowerShell quoting caused malformed JSON and argument parsing problems. The terminal returned errors including:

```text
JSON decode error
URL rejected
Could not resolve host
unmatched close brace/bracket
```

### Interpretation

The application was not the issue. The failure came from PowerShell escaping behavior when passing inline JSON to `curl.exe`.

### Fix

The command was retried using PowerShell-native HTTP invocation:

```powershell
Invoke-RestMethod -Method POST `
  -Uri "http://localhost:8000/query" `
  -ContentType "application/json" `
  -Body '{"text":"What is hypertension?","top_k":3}'
```

### Result

The request succeeded and returned:

- the original query
- a stub RAG answer
- source identifiers
- an embedding vector

### Day 1 Outcome

Day 1 was completed successfully because:

- both services were healthy
- the gateway could reach the inference service
- `POST /query` returned a valid end-to-end response

## Day 2: Kubernetes

### Goal

Move the working Docker Compose setup into Kubernetes using Minikube and complete the PRD Day 2 scope:

- Deployments
- Services
- HPA
- Ingress

## Cluster Readiness

### Commands

```powershell
minikube status
kubectl version --client
kubectl config current-context
```

### Why

This checked whether Minikube was running, whether `kubectl` was installed, and whether the active Kubernetes context pointed to the local cluster.

### What Happened

Minikube was only partially running:

- host was running
- kubelet was stopped
- apiserver was stopped

### Fix

```powershell
minikube start
minikube addons enable metrics-server
kubectl get nodes
kubectl get pods -A
```

This fully started the cluster and enabled the metrics server needed for HPA.

## Building Images for Minikube

### Commands

```powershell
minikube -p minikube docker-env --shell powershell | Invoke-Expression
docker build -t vigil-inference-service:latest .\inference_service
docker build -t vigil-gateway:latest .\gateway
docker images | Select-String "vigil-"
```

### Why

The Kubernetes manifests used:

- `vigil-inference-service:latest`
- `vigil-gateway:latest`

These images needed to exist inside Minikube's Docker environment so the cluster could run them without pushing to an external registry.

### Result

Both images were built successfully and matched the tags used in the manifests.

## Applying Kubernetes Manifests

### Commands

```powershell
kubectl apply -f .\k8s\deployment.yaml
kubectl apply -f .\k8s\service.yaml
kubectl get deployments
kubectl get pods
kubectl get svc
```

### Why

This created:

- a Deployment for `gateway`
- a Deployment for `inference-service`
- a `NodePort` Service for `gateway`
- a `ClusterIP` Service for `inference-service`

### Result

The `gateway` pod became healthy quickly, but the `inference-service` pod stayed `Running` with `READY 0/1`.

## Investigating the Inference Pod

### Commands

```powershell
kubectl describe pod inference-service-74bd68fc7c-4t2k2
kubectl logs inference-service-74bd68fc7c-4t2k2
```

### Why

This was used to understand why the inference pod was not becoming ready.

### Error Observed

The pod events showed:

```text
Readiness probe failed: connect: connection refused
Liveness probe failed: connect: connection refused
Container inference-service failed liveness probe, will be restarted
```

### Interpretation

Kubernetes was checking `/health` before the app had finished starting.

## First Fix: Startup Probe

### Change Made

The inference Deployment was updated to include a `startupProbe` and more forgiving readiness/liveness timing.

### Why

The inference service loads a sentence-transformer model, which takes much longer to start than a simple FastAPI app.

### Commands

```powershell
kubectl apply -f .\k8s\deployment.yaml
kubectl get pods -w
```

### Result

The pod still took too long to become ready.

## Root Cause Discovery

### Commands

```powershell
kubectl describe pod inference-service-66cdc887ff-crmf5
kubectl logs inference-service-66cdc887ff-crmf5
```

### Error Observed

The logs showed:

```text
Warning: You are sending unauthenticated requests to the HF Hub.
```

The startup probe continued failing with:

```text
connect: connection refused
```

### Interpretation

The service was trying to download the Hugging Face model at runtime before the app started listening on port `8001`.

## Final Fix: Bake the Model into the Image

### Change Made

The inference Dockerfile was updated so the model download happens during image build instead of container startup.

### Why

This avoids slow cold starts in Kubernetes and makes the pod ready much faster.

### Commands

```powershell
docker build -t vigil-inference-service:latest .\inference_service
kubectl rollout restart deployment/inference-service
kubectl get pods -w
```

### Result

The new inference pod eventually became `1/1 Ready`, and the old pod was replaced.

## Verifying Deployments and Services

### Commands

```powershell
kubectl get deployments
kubectl get pods
kubectl get svc
minikube service gateway --url
```

### Why

This verified:

- both Deployments were available
- both pods were healthy
- Services were created properly
- the gateway could be reached externally through Minikube

### Result

The gateway was exposed at a local URL similar to:

```text
http://127.0.0.1:54804
```

## End-to-End Kubernetes Test

### Commands

```powershell
curl.exe http://127.0.0.1:54804/health
```

```powershell
Invoke-RestMethod -Method POST `
  -Uri "http://127.0.0.1:54804/query" `
  -ContentType "application/json" `
  -Body '{"text":"What is hypertension?","top_k":3}'
```

### Why

This confirmed:

- ingress into the gateway through Kubernetes
- gateway to inference-service communication through Kubernetes DNS
- end-to-end behavior matched the Docker Compose setup

### Result

Both requests succeeded.

## Horizontal Pod Autoscaler

### Commands

```powershell
kubectl apply -f .\k8s\hpa.yaml
kubectl get hpa
kubectl top pods
```

### Why

This created the HPA resources and checked whether the metrics server was returning CPU metrics.

### Initial Issue

Immediately after creation, `kubectl get hpa` showed:

```text
cpu: <unknown>/60%
```

### Interpretation

The HPA had been created, but metrics had not propagated yet.

### Confirmation

`kubectl top pods` returned live CPU and memory usage, proving metrics-server was working.

### Final Check

```powershell
kubectl get hpa
```

This later showed valid targets such as:

```text
gateway             cpu: 2%/60%
inference-service   cpu: 0%/60%
```

That confirmed HPA was working correctly.

## Ingress Setup

### Commands

```powershell
minikube addons enable ingress
kubectl apply -f .\k8s\ingress.yaml
kubectl get ingress
kubectl describe ingress gateway-ingress
```

### Why

This enabled an NGINX ingress controller in Minikube and created an Ingress resource for the `gateway` Service using the host `vigil-ai.local`.

### Result

The ingress resource was created successfully, and the rule mapped:

- host: `vigil-ai.local`
- path: `/`
- backend: `gateway:8000`

### Note

On Windows with Minikube's Docker driver, `minikube tunnel` needs to stay running for ingress access through `127.0.0.1`.

### Test Commands

```powershell
minikube tunnel
```

```powershell
curl.exe http://127.0.0.1/health -H "Host: vigil-ai.local"
```

Optional local hostname mapping:

```text
127.0.0.1 vigil-ai.local
```

Then:

```powershell
curl.exe http://vigil-ai.local/health
```

## Files Created or Updated During Days 1 and 2

- `k8s/deployment.yaml`
- `k8s/service.yaml`
- `k8s/hpa.yaml`
- `k8s/ingress.yaml`
- `inference_service/Dockerfile`

## Summary of Issues and Fixes

### Issue 1

PowerShell quoting broke inline JSON when testing Day 1 with `curl.exe`.

### Fix

Used `Invoke-RestMethod` for reliable JSON request submission.

### Issue 2

Minikube control plane was not fully running.

### Fix

Started Minikube and enabled `metrics-server`.

### Issue 3

Inference pod failed readiness and liveness checks.

### Fix

Added a `startupProbe` and relaxed health probe timing.

### Issue 4

Inference service was downloading the Hugging Face model at runtime, delaying startup.

### Fix

Pre-downloaded the model during Docker image build.

### Issue 5

HPA initially showed `<unknown>` CPU metrics.

### Fix

Waited for metrics propagation and confirmed metrics-server with `kubectl top pods`.

### Issue 6

Ingress requires additional routing support on Windows with Minikube's Docker driver.

### Fix

Enabled ingress addon and used `minikube tunnel`, with optional hosts-file mapping for `vigil-ai.local`.

## Current Outcome

At this stage:

- Day 1 is complete
- Day 2 is complete
- the application works locally with Docker Compose
- the application works on Minikube with Kubernetes
- HPA is active
- ingress is configured

Future days can append new sections below this point.

## Day 3: Observability

### Goal

Add observability to both FastAPI services and stand up a basic monitoring stack with:

- Prometheus scraping
- Grafana dashboards
- live visibility into latency, error rate, CPU, and requests per second

### Instrumenting the Services

### Changes Made

Prometheus instrumentation was added to both services using `prometheus-fastapi-instrumentator`.

### Files Updated

- `gateway/pyproject.toml`
- `inference_service/pyproject.toml`
- `gateway/main.py`
- `inference_service/main.py`

### Why

This exposed a `/metrics` endpoint on both FastAPI services so Prometheus could scrape request and process metrics.

## Monitoring Stack Setup

### Changes Made

A local monitoring stack was added through Docker Compose:

- Prometheus
- Grafana

### Files Created

- `monitoring/prometheus.yml`
- `monitoring/grafana/provisioning/datasources/prometheus.yml`
- `monitoring/grafana/provisioning/dashboards/dashboards.yml`
- `monitoring/grafana/dashboards/grafana-dashboard.json`

### File Updated

- `docker-compose.yml`

### Why

This allowed Day 3 observability to be validated locally before deciding whether to extend the same setup into Kubernetes.

## Metrics Verification

### Commands

```powershell
curl.exe http://localhost:8000/metrics
curl.exe http://localhost:8001/metrics
curl.exe http://localhost:9090/-/ready
```

### Why

These commands checked:

- the gateway metrics endpoint
- the inference service metrics endpoint
- Prometheus readiness

### Result

All three endpoints responded successfully.

The `/metrics` output included:

- Python process metrics
- request counters
- request duration histograms
- handler-level request labels

## Dashboard and Target Verification

### Checks Performed

- Prometheus targets page was checked and confirmed `UP`
- Grafana was opened and confirmed to auto-load the provisioned dashboard

### Result

Both Prometheus and Grafana were functioning as expected.

## Traffic Generation for Dashboard Validation

### Command

```powershell
1..10 | ForEach-Object {
  Invoke-RestMethod -Method POST `
    -Uri "http://localhost:8000/query" `
    -ContentType "application/json" `
    -Body '{"text":"What is hypertension?","top_k":3}' | Out-Null
}
```

### Why

This generated enough application traffic for Prometheus to scrape useful request data and for the Grafana panels to visibly update.

### Result

After the looped requests:

- latency showed live values
- error rate remained visible
- CPU usage showed activity
- requests per second populated correctly

## Day 3 Outcome

Day 3 was completed successfully because:

- both services exposed `/metrics`
- Prometheus scraped both services successfully
- Grafana auto-loaded the dashboard
- dashboard panels populated with live data after traffic generation

## Files Created or Updated During Day 3

- `gateway/pyproject.toml`
- `inference_service/pyproject.toml`
- `gateway/main.py`
- `inference_service/main.py`
- `docker-compose.yml`
- `monitoring/prometheus.yml`
- `monitoring/grafana/provisioning/datasources/prometheus.yml`
- `monitoring/grafana/provisioning/dashboards/dashboards.yml`
- `monitoring/grafana/dashboards/grafana-dashboard.json`
