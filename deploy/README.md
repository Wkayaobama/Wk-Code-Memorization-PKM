# Recurser for a team, on Kubernetes

One deployment serves several people. Each person signs in with a personal link, sees only
the projects listed for them, keeps a review history of their own, and can ask questions
that are answered from the decks through the Anthropic SDK.

## The five layers

| Layer | What it is here | File |
|---|---|---|
| 1. Container | The web server, an API service on port `8000` | `Dockerfile` |
| 2. ConfigMap | `config.yaml` (model, retrieval, limits), mounted at `/app/config/config.yaml` | `k8s/03-config.yaml` |
| 3. Secret | The Anthropic credential, injected as `ANTHROPIC_API_KEY` with `secretKeyRef`. A second Secret holds `users.json`, mounted as a file | `k8s/04-secrets.example.yaml` |
| 4. Service | Port `80` inside the cluster to the container's `8000`. `kubectl port-forward` for local access, the API described at `/docs` and `/openapi.json` | generated |
| 5. Custom resource | A `RecurserApp` names the image, the `projects` it serves, `configRef`, `secretRef` and `usersRef`. The controller checks them and generates the Deployment and the Service | `k8s/01-crd.yaml`, `k8s/02-controller.yaml`, `k8s/05-recurserapp.yaml` |

The controller also generates a volume claim for the review histories, a NetworkPolicy, and
an Ingress when `spec.ingress.host` is set. Every generated object is owned by the
`RecurserApp`: delete it and they go with it.

## Step by step

```bash
# 0. The image. The decks under data/ are copied in, so build from the checkout that holds
#    the decks this deployment may serve, and push to a registry as private as those decks.
docker build -t ghcr.io/<you>/recurser:0.2.0 .
docker push ghcr.io/<you>/recurser:0.2.0

# 1. Your own copy of the files, with your image in them (deploy/k8s/ holds the same with a placeholder).
python3 -m recurser k8s write --image ghcr.io/<you>/recurser:0.2.0 --namespace recurser --name team --out my-k8s

# 2. Namespace, custom resource definition, controller, settings.
kubectl apply -f my-k8s/00-namespace.yaml -f my-k8s/01-crd.yaml
kubectl apply -f my-k8s/02-controller.yaml -f my-k8s/03-config.yaml

# 3. Secrets, created with kubectl so that no credential is ever written to a file in git.
kubectl -n recurser create secret generic team-anthropic --from-literal=ANTHROPIC_API_KEY=<your key>
python3 -m recurser users add wk  --all                              --file users.json
python3 -m recurser users add sam --projects tide-loader,harbor-notes --file users.json
kubectl -n recurser create secret generic team-users --from-file=users.json=users.json

# 4. The application: one object.
kubectl apply -f my-k8s/05-recurserapp.yaml
kubectl -n recurser get recurserapps        # READY and REASON say what is missing, if anything

# 5. Reach it.
kubectl -n recurser port-forward svc/team 8000:80
#    then open http://127.0.0.1:8000/?token=<your token>     (the API is described at /docs)
```

`users add` prints each person's link. Send it over a private channel: the token in it is
that person's sign-in. `users.json` must stay out of git.

Without the controller, skip `01` and `02` and apply `my-k8s/plain/recurser.yaml` instead of
`05`: it holds the same Deployment, Service, volume claim and NetworkPolicy as static files.

## Who sees what

`spec.projects` on the `RecurserApp` limits what the whole deployment serves. Leave it out to
serve every project in the image. Inside that, each person has a list of their own.

`users.json` lists the people. `"projects": "*"` gives every project in the image. A list
gives only those projects: the others are absent from that person's page, from their
search, from their stored history, and from what is sent to the API on their behalf. Names
of private repositories outside the list are replaced in the remaining text, quoted code,
paths and branch names included.

To change access, edit the file with `recurser users add` or `remove`, then replace the
Secret. The controller notices the new version and restarts the pod.

```bash
kubectl -n recurser create secret generic team-users --from-file=users.json=users.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

`usersRef` is required. A shared deployment never runs on one link for everybody, and no
sign-in token is ever written to the pod's log.

Behind an authenticating proxy that sets a header with the person's name, set
`access.trust_user_header` and `access.trusted_proxies` in `config.yaml`. The header is
believed only when the request comes from one of those addresses, and names are matched
against `users.json`. From anywhere else the header is ignored and the personal link decides.

## Network

- **In:** the NetworkPolicy admits pods of the same namespace, the ingress controller's
  namespace when an Ingress is requested, and the namespaces in
  `spec.networkPolicy.ingressNamespaces`. `kubectl port-forward` keeps working.
- **Out:** DNS, and HTTPS to public addresses only. Private ranges and the node metadata
  address are excluded, so the pod can call the Anthropic API and nothing inside the
  cluster. A plain NetworkPolicy cannot filter by host name. With a network plugin that
  can, narrow the HTTPS rule to `api.anthropic.com`. If your cluster reaches the internet
  through a proxy on a private address, that address must be added to the policy.
- **Outside the cluster:** set `spec.ingress.host`, and `tlsSecretName` for HTTPS. The
  sign-in cookie is marked `Secure` when the request arrived over HTTPS.

## The assistant

Retrieval runs in the pod: every card and every fact of every project note is a passage,
scored with BM25 against the question, inside the projects the person may see. The best
passages go to the Messages API as `search_result` blocks with citations enabled, and the
answer comes back with the passage each sentence rests on.

| Setting | Default | Meaning |
|---|---|---|
| `llm.model` | `claude-opus-5-5` | the model |
| `llm.effort` | `medium` | `low`, `medium`, `high`, `xhigh` or `max` |
| `llm.fallbacks` | `true` | when a safety classifier declines a request, the API re-runs it on a fallback model in the same call |
| `llm.timeout_seconds` | `60` | the longest silence tolerated from the API |
| `llm.answer_seconds` | `180` | the longest one answer may take in all |
| `llm.enabled` | `true` | `false` turns the assistant off |
| `retrieval.top_k` | `8` | passages sent with a question |
| `server.ask_per_10_minutes` | `30` | questions per person |

Without a `secretRef` the page works and the assistant is off.

## If something is off

- `kubectl -n recurser get recurserapps` shows `READY` and `REASON`: `ConfigMissing`,
  `ConfigInvalid`, `SecretKeyMissing`, `UsersMissing`, `ApplyFailed` and the like name the
  cause, and `kubectl describe` has the sentence.
- `Progressing` for long: `kubectl -n recurser logs deploy/team`. The server says what stops
  it, for example a project name in `users.json` that is not in the image.
- History is not saved: the volume must be writable by user and group `10001`. Most
  storage classes honour `fsGroup`; a class that does not needs its own fix.

## What was tested, and what was not

Tested here: the server started with these exact arguments and this `config.yaml`; sign-in,
per-person decks and histories; the SDK calls against a local stand-in that speaks the
streaming protocol; every generated object against the typed models of the official
Kubernetes Python client (`tests/check_manifests.py`); the controller's decisions against a
simulated cluster.

Not tested: the image build, the objects on a real cluster, the controller's calls to a real
API server, and a request to the live Anthropic API. Run steps 0 to 5 once on a test cluster
before relying on it.
