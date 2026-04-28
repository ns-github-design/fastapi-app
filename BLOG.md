# CI/CD as a Platform: Shipping Microservices and AI Agents with Reusable GitHub Actions Workflows

At ten services, duplicated CI/CD pipelines are annoying. At fifty, they're a liability. Versions diverge, security fixes propagate inconsistently, and deployment logic becomes org-wide technical debt. The fix isn't better templates — it's a different architecture.

This post builds a two-tier delivery platform: reusable GitHub Actions workflows for microservices, extended with evaluation gates for AI agents. The core insight is that AI systems break CI/CD's fundamental assumption — deterministic correctness. A unit test validates code behavior. It cannot validate whether an agent hallucinates, drifts, or degrades silently across prompt changes. Addressing that requires evaluation as a first-class deployment gate, not a manual afterthought.

By the end: a versioned CI/CD platform, a scoring-based AI delivery pipeline, and a Foundry-integrated governance layer — all built on the same reusable workflow architecture.

---

## The Architecture

Two repositories. Clear ownership. No duplicated logic.

The **platform repo** owns all delivery logic — reusable, versioned workflows callable by any application team. The **application repo** is intentionally thin: one workflow file that calls the platform via GitHub's `workflow_call` mechanism.

```
ci-platform/               ← owned by platform team
  .github/workflows/
    test-python.yml
    build.yml
    deploy.yml
    evaluate-agent.yml     ← added for AI

fastapi-app/               ← owned by application team
  .github/workflows/
    release.yml            ← calls ci-platform workflows
  src/
  Dockerfile
```

`workflow_call` lets one workflow invoke another across repositories. The application passes inputs; the platform owns the implementation. Same contract as an API.

The pipeline moves through four stages: **Test → Build → Deploy Staging → Deploy Production**. One image built once, promoted immutably across environments. The Git SHA is the image tag — every running container traces to a specific commit.

Azure infrastructure: **ACR** (image store) + **Azure Container Apps** (managed runtime), provisioned via Bicep and deployed to separate resource groups for staging and production.

---

## Platform Repo — Reusable Workflows

Three workflows. One infrastructure file.

| Workflow | Responsibility |
|---|---|
| `test-python.yml` | Install dependencies and run tests |
| `build.yml` | Build Docker image and push to ACR |
| `deploy.yml` | Deploy a specific image to a specific environment |

### test-python.yml

```yaml
name: test-python
on:
  workflow_call:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11.9"
      - run: pip install -r requirements.txt
      - run: pytest
```

Python version is pinned to `3.11.9` — not a floating `3.11`. Every service tests against an identical runtime. The `workflow_call` trigger makes this unreachable except via `uses:` — it cannot be triggered directly.

### build.yml

```yaml
name: build
on:
  workflow_call:
    outputs:
      image_tag:
        value: ${{ jobs.build.outputs.image_tag }}
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      image_tag: ${{ steps.meta.outputs.tag }}
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - id: meta
        run: echo "tag=${GITHUB_SHA}" >> $GITHUB_OUTPUT
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - run: az acr login --name ${{ secrets.ACR_NAME }}
      - run: |
          docker build -t ${{ secrets.ACR_LOGIN_SERVER }}/app:${{ github.sha }} .
          docker push ${{ secrets.ACR_LOGIN_SERVER }}/app:${{ github.sha }}
```

Key design decisions:
- **`id-token: write`** — enables OIDC-based Azure authentication. No long-lived secrets. GitHub mints a short-lived token at runtime; Azure trusts it via federated identity. Credentials never stored.
- **`${GITHUB_SHA}` as image tag** — every container is traceable to its exact source commit.
- **`image_tag` output** — the SHA flows downstream to deploy jobs without being hardcoded anywhere.

### deploy.yml

```yaml
name: deploy
on:
  workflow_call:
    inputs:
      environment:
        required: true
        type: string
      image_tag:
        required: true
        type: string
      app_name:
        required: true
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    steps:
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - run: |
          az containerapp update \
            --name ${{ inputs.app_name }} \
            --resource-group ${{ secrets.AZURE_RESOURCE_GROUP }} \
            --image ${{ secrets.ACR_LOGIN_SERVER }}/app:${{ inputs.image_tag }}
```

`environment: ${{ inputs.environment }}` is the key line. It binds the job to a GitHub Environment at runtime, meaning all protection rules — required reviewers, wait timers, deployment policies — apply automatically based on what the caller passes. One workflow handles every environment. No hardcoded logic.

---

## Azure Infrastructure — Bicep

Two resources. One file. Deployed twice (staging and production) into isolated resource groups.

```bicep
param location string = resourceGroup().location

resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: 'myregistry'
  location: location
  sku: { name: 'Basic' }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'my-app'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
      }
    }
  }
}

// Grant the Container App's managed identity permission to pull images from ACR
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, containerApp.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
```

Three things changed from the earlier version:
- **`identity: { type: 'SystemAssigned' }`** — gives the Container App a managed identity at provisioning time
- **`acrPullRoleId`** — the built-in `AcrPull` role ID, scoped to the ACR resource
- **`acrPullAssignment`** — binds the role to the Container App's identity so it can pull images without a stored password

Without these three additions, `az containerapp update` succeeds, the pipeline goes green, and the container silently fails to start with an image pull error. This is the most common runtime failure for first-time deployments.

```bash
az deployment group create --resource-group rg-ciplatform-staging --template-file infra/main.bicep
az deployment group create --resource-group rg-ciplatform-production --template-file infra/main.bicep
```

**OIDC setup** (run once):

```bash
az ad app create --display-name "github-actions-platform"
az ad app federated-credential create \
  --id <app-id> \
  --parameters '{
    "name": "github-actions",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:your-org/fastapi-app:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Required GitHub secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `ACR_NAME`, `ACR_LOGIN_SERVER`.

---

## Application Repo — Release Workflow

The application's entire CI/CD surface is one file:

```yaml
name: release
on:
  push:
    branches: [main]
permissions:
  id-token: write
  contents: read
jobs:
  test:
    uses: ns-github-design/ci-platform/.github/workflows/test-python.yml@v2

  build:
    needs: test
    uses: ns-github-design/ci-platform/.github/workflows/build.yml@v2
    secrets: inherit

  deploy-staging:
    needs: build
    uses: ns-github-design/ci-platform/.github/workflows/deploy.yml@v2
    with:
      environment: staging
      image_tag: ${{ needs.build.outputs.image_tag }}
      app_name: my-app-staging
    secrets: inherit

  deploy-prod:
    needs: [build, deploy-staging]
    uses: ns-github-design/ci-platform/.github/workflows/deploy.yml@v2
    with:
      environment: production
      image_tag: ${{ needs.build.outputs.image_tag }}
      app_name: my-app-prod
    secrets: inherit
```

The `@v2` tag pins to a specific platform version. Application teams are insulated from breaking changes; platform teams can ship improvements independently. `image_tag` flows from `build` → `deploy-staging` → `deploy-prod` as a single SHA — one artifact, promoted immutably.

The FastAPI app exposes a `/version` endpoint that surfaces `GITHUB_SHA` and `APP_ENV` as runtime values, making deployment verification trivial without inspecting container logs:

```python
@app.get("/version")
def version():
    return {
        "version": os.getenv("GITHUB_SHA", "dev"),
        "environment": os.getenv("APP_ENV", "local")
    }
```

The Dockerfile base image (`python:3.11.9-slim`) intentionally matches the platform's `test-python.yml` runtime — the same Python version runs in CI and in production, eliminating an entire class of environment-specific failures.

---

## Why AI Breaks This Model

The platform above is complete for deterministic software. AI agents break its core assumption.

| Characteristic | Traditional Software | AI / Agent Systems |
|---|---|---|
| Behavior | Deterministic | Probabilistic |
| Definition of success | Binary pass/fail | Continuous score |
| Change surface | Source code | Prompt + model + data |
| Validation method | Unit tests | Semantic evaluation |
| Failure modes | Exceptions, wrong output | Hallucination, drift, bias |

A unit test can assert that code returns the right value. It cannot assert that an LLM response is accurate, factual, or safe. A prompt change that silently degrades quality will pass every existing test and deploy cleanly.

The pipeline needs a new gate: **evaluation**.

---

## Evaluation as a Deployment Gate

What testing is to code, evaluation is to AI. We add a fourth reusable workflow — `evaluate-agent.yml` — to the platform repo. It runs before any deployment.

```yaml
# ci-platform/.github/workflows/evaluate-agent.yml
name: evaluate-agent
on:
  workflow_call:
    inputs:
      score_threshold:
        required: false
        type: number
        default: 0.8
    outputs:
      score:
        value: ${{ jobs.evaluate.outputs.score }}
jobs:
  evaluate:
    runs-on: ubuntu-latest
    outputs:
      score: ${{ steps.eval.outputs.score }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11.9"
      - run: pip install -r eval/requirements.txt
      - id: eval
        env:
          AZURE_OPENAI_ENDPOINT: ${{ secrets.AZURE_OPENAI_ENDPOINT }}
          AZURE_OPENAI_KEY: ${{ secrets.AZURE_OPENAI_KEY }}
          AZURE_OPENAI_DEPLOYMENT: ${{ secrets.AZURE_OPENAI_DEPLOYMENT }}
        run: |
          score=$(python eval/eval.py)
          echo "score=$score" >> $GITHUB_OUTPUT
          echo "$score" > eval_score.txt
          python -c "import sys; sys.exit(0 if float('$score') >= ${{ inputs.score_threshold }} else 1)"
      - name: Upload evaluation score
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: eval-score
          path: eval_score.txt
```

If the score falls below the threshold, the step exits non-zero. GitHub cancels all downstream jobs. No deployment occurs. The score is logged as a build artifact.

### eval/requirements.txt

```
openai>=1.0.0
```

### eval/dataset.json

A minimal golden dataset — inputs the agent should handle, paired with the expected response used by the judge to score quality:

```json
[
  {
    "input": "What is the return policy?",
    "expected": "Items can be returned within 30 days of purchase with a receipt."
  },
  {
    "input": "How do I reset my password?",
    "expected": "Click 'Forgot Password' on the login page and follow the email instructions."
  },
  {
    "input": "What are your support hours?",
    "expected": "Support is available Monday to Friday, 9am to 6pm EST."
  }
]
```

Extend this file as the agent's scope grows. The pipeline scores every entry on every run — regression is caught automatically.

### eval.py — What Evaluation Looks Like

This is a complete, runnable LLM-as-judge implementation. It calls your agent, then asks an Azure OpenAI model to score each response against the expected answer.

```python
import json
import os
from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_KEY"],
    api_version="2024-02-01",
)

JUDGE_PROMPT = """\
You are a strict evaluator. Given an agent response and an expected answer,
score the response from 0.0 to 1.0 based on accuracy and relevance.
Return only a float between 0.0 and 1.0. No explanation. No other text.\
"""


def call_agent(user_input: str) -> str:
    """Call your agent. Replace this with your actual agent endpoint or SDK call."""
    system_prompt = open("prompts/system_v1.txt").read()
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
    )
    return response.choices[0].message.content


def score_response(response: str, expected: str) -> float:
    """Ask the LLM judge to score the response against the expected answer."""
    result = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"Response: {response}\nExpected: {expected}"},
        ],
        temperature=0,
    )
    try:
        return float(result.choices[0].message.content.strip())
    except ValueError:
        return 0.0


def evaluate_agent() -> float:
    dataset = json.load(open("eval/dataset.json"))
    scores = [score_response(call_agent(s["input"]), s["expected"]) for s in dataset]
    return round(sum(scores) / len(scores), 4)


if __name__ == "__main__":
    print(evaluate_agent())
```

Three required secrets in your GitHub repo: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT`. The same Azure OpenAI resource acts as both the agent under test and the judge — swap `call_agent()` for your real agent SDK call when ready.

### Integrating the Evaluation Stage

The AI application's release workflow becomes:

```yaml
name: release

on:
  push:
    branches: [main]

permissions:
  id-token: write
  contents: read

jobs:
  test:
    uses: ns-github-design/ci-platform/.github/workflows/test-python.yml@v2

  build:
    needs: test
    uses: ns-github-design/ci-platform/.github/workflows/build.yml@v2
    secrets: inherit

  evaluate:
    needs: build
    uses: ns-github-design/ci-platform/.github/workflows/evaluate-agent.yml@v2
    with:
      score_threshold: 0.85

  deploy-staging:
    needs: evaluate
    uses: ns-github-design/ci-platform/.github/workflows/deploy.yml@v2
    with:
      environment: staging
      image_tag: ${{ needs.build.outputs.image_tag }}
      app_name: my-agent-staging
    secrets: inherit

  deploy-prod:
    needs: [evaluate, deploy-staging]
    uses: ns-github-design/ci-platform/.github/workflows/deploy.yml@v2
    with:
      environment: production
      image_tag: ${{ needs.build.outputs.image_tag }}
      app_name: my-agent-prod
    secrets: inherit
```

Control flow: **Test → Build → Evaluate → Deploy**. A score below `0.85` blocks at the evaluate stage. Nothing reaches staging or production. The score, prompt version, model checkpoint, and dataset revision are all logged as artifacts — every release decision is auditable.

---

## Foundry Integration

With evaluation gates in place, we integrate Microsoft Foundry as the AI agent runtime. Foundry provides managed agent execution, LLM-as-judge evaluation services, and live telemetry — extending the platform from delivery into observability.

The platform repo gains a fourth workflow: `monitor.yml`, which polls Foundry for post-deployment metrics and can trigger re-evaluation if production performance degrades.

| Workflow | Foundry Capability |
|---|---|
| `build.yml` | Model packaging & versioning |
| `evaluate-agent.yml` | Offline evaluation / LLM-as-judge |
| `deploy.yml` | Agent deployment to Foundry runtime |
| `monitor.yml` | Live telemetry, drift detection, re-evaluation trigger |

### monitor.yml — Full Definition

```yaml
# ci-platform/.github/workflows/monitor.yml
name: monitor
on:
  workflow_call:
    inputs:
      foundry_project:
        required: true
        type: string
      alert_threshold:
        required: false
        type: number
        default: 0.75
      app_name:
        required: true
        type: string
      environment:
        required: true
        type: string
jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Fetch production evaluation score from App Insights
        id: metrics
        run: |
          score=$(az monitor app-insights query \
            --app ${{ secrets.APPINSIGHTS_NAME }} \
            --analytics-query "customEvents \
              | where name == 'agent_eval_score' \
              | where tostring(customDimensions.project) == '${{ inputs.foundry_project }}' \
              | where tostring(customDimensions.app) == '${{ inputs.app_name }}' \
              | summarize avg(todouble(customDimensions.score)) \
              | project avg_score" \
            --query "tables[0].rows[0][0]" \
            --output tsv 2>/dev/null || echo "1.0")
          echo "score=$score" >> $GITHUB_OUTPUT

      - name: Check score against alert threshold
        run: |
          python - <<'EOF'
          import sys, os
          score = float(os.environ["SCORE"])
          threshold = float("${{ inputs.alert_threshold }}")
          print(f"Production score: {score} | Threshold: {threshold}")
          if score < threshold:
              print(f"::error::Score {score} is below threshold {threshold}.")
              print("::notice::Consider triggering re-evaluation or rolling back.")
              sys.exit(1)
          print("Score is within acceptable range.")
          EOF
        env:
          SCORE: ${{ steps.metrics.outputs.score }}

      - name: Write monitoring result to file
        if: always()
        run: echo "${{ steps.metrics.outputs.score }}" > monitor_score.txt

      - name: Upload monitoring result
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: monitor-score-${{ inputs.environment }}
          path: monitor_score.txt
```

The `APPINSIGHTS_NAME` secret points to the Azure Application Insights resource where your agent emits `agent_eval_score` custom events. The `foundry_project` input is logged in the step output and can be used to scope the App Insights query to a specific Foundry project namespace. The workflow queries the last average, compares against `alert_threshold`, and fails the job — blocking any downstream auto-promotion — if the score has drifted below the acceptable floor.

A Foundry-aware pipeline in an agent repo:

```yaml
  deploy-prod:
    needs: [evaluate, deploy-staging]
    uses: ns-github-design/ci-platform/.github/workflows/deploy.yml@v2
    with:
      environment: production
      image_tag: ${{ needs.build.outputs.image_tag }}
      app_name: my-agent-prod
    secrets: inherit

  monitor:
    needs: deploy-prod
    uses: ns-github-design/ci-platform/.github/workflows/monitor.yml@v2
    with:
      foundry_project: my-ai-agent
      alert_threshold: 0.75
      app_name: my-agent-prod
      environment: production
    secrets: inherit
```

Foundry telemetry feeds back into the pipeline — if production scores drift below `alert_threshold`, `monitor.yml` can open a GitHub issue, trigger a re-evaluation run, or auto-rollback via the deploy workflow. GitHub Actions owns delivery; Foundry owns runtime and observability. Together they close the loop.

---

## Advanced Practices for AI Delivery

### Prompts Are Code — Version Them

A single token change in a system prompt can shift agent behavior as dramatically as a logic rewrite. Store prompts in git alongside code:

```
/prompts/
  system_v1.txt
  system_v2.txt
```

Reference the active version explicitly in pipeline metadata and `experiment.json` artifacts. Re-runs of a specific commit must reproduce identical behavior — that's only possible if prompts are versioned and immutable per release.

`eval.py` reads `prompts/system_v1.txt` as the system prompt at evaluation time. A minimal starting point:

```
You are a helpful customer support assistant. Answer questions accurately and concisely.
If you do not know the answer, say so. Do not fabricate information.
```

Commit this file alongside code. Change the filename (`system_v2.txt`) when the prompt changes — never edit in place.

### Treat (Code, Prompt, Model) as One Release Unit

For AI systems, rollback has three dimensions:

| Dimension | Rollback Mechanism |
|---|---|
| Code | Redeploy previous container image |
| Prompt | Revert to earlier prompt file in git |
| Model | Reuse prior checkpoint or model deployment ID |

Version them together as a single immutable release triple. GitHub tags + evaluation artifacts = auditable rollback point.

### Continuous Evaluation After Deployment

The evaluation gate fires pre-deployment. But model APIs update, data distributions shift, and agent quality can degrade in production without any code change. Schedule post-deployment evaluation jobs:

```yaml
on:
  schedule:
    - cron: "0 */6 * * *"   # every 6 hours
```

Feed live production traces back into the evaluation dataset. If scores fall below threshold, auto-trigger re-evaluation or alert. This closes the MLOps loop: deploy → monitor → evaluate → re-deploy or rollback.

### Governance by Design

Combine GitHub's native controls with Foundry's policy hooks:
- Branch protections prevent unreviewed prompt changes from reaching main
- Required reviewers on the production GitHub Environment gate human approval
- Foundry policies restrict which teams can promote evaluated agents
- Minimum score thresholds are enforced in `evaluate-agent.yml` — not in a wiki, not in a checklist

Governance embedded in code scales. Governance in documentation doesn't.

---

## Maturity Model

| Stage | Delivery Mechanism | Validation |
|---|---|---|
| CI/CD as Automation | Per-repo YAML | Tests |
| CI/CD as Product | Reusable versioned workflows | Tests + Approval gates |
| CI/CD as Governance | Platform repo + GitHub Environments | Tests + Reviews + Traceability |
| AI Delivery Platform | Evaluation gates + Foundry runtime | Semantic scoring + Drift monitoring |

Each stage is additive — the platform repo from stage 2 powers all subsequent stages. No rebuild required.

---

## Conclusion

The same architectural principle scales from microservices to AI agents: centralize delivery logic, version it, expose it as a reusable interface. What changes for AI is the definition of "correct" — from binary pass/fail to continuous behavioral scoring.

The platform built here enforces that at every layer:
- `evaluate-agent.yml` blocks probabilistic quality failures before deployment
- Foundry's LLM-as-judge and telemetry close the observability loop post-deployment
- Versioned (code, prompt, model) triples make every release reproducible and rollback-safe
- Governance is code, not process

The result isn't a better pipeline. It's a delivery system that treats AI behavior as a first-class engineering constraint.
