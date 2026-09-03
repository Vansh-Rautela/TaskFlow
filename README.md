# TaskFlow

**An autonomous email support agent that knows when *not* to trust itself.**

TaskFlow reads a customer support email, classifies it, retrieves the relevant policy from a knowledge base, and drafts a cited reply. Before that reply can be sent automatically it has to pass eight deterministic safety gates and clear a quality score. Anything that fails either goes to a human, with the reasoning attached.

## → The project lives in [`Code/TaskFlow/taskflow/`](Code/TaskFlow/taskflow/)

**Start there.** That directory has the full README: architecture, getting started, the decision engine, measured results, and the complete documentation set.

```bash
git clone https://github.com/Vansh-Rautela/TaskFlow.git
cd TaskFlow/Code/TaskFlow/taskflow
cat README.md
```

## What's in this repository

| Path | What it is |
|---|---|
| [`Code/TaskFlow/taskflow/`](Code/TaskFlow/taskflow/) | **The system.** Source, tests, configuration, and all documentation. |
| `Code/Data/Documents/synthetic-saas-kb/` | The synthetic knowledge base and evaluation queries the system is built and measured against. 42 documents, 478 labelled evaluation queries. No real customer data. |
| `Code/IMPLEMENTATION_PLAN.md`, `Code/Design/` | Planning notes written during development. |
| `TASKFLOW_ARCHITECTURE_REVIEW_AND_IMPLEMENTATION_PLAN.md.pdf` | An early architecture review, kept for reference. |

## The short version

TaskFlow reads a support email, classifies it, retrieves the relevant policy, and drafts a cited reply. Before that reply can go out on its own it has to pass eight deterministic safety gates — a policy violation, a PII leak, an unresolvable citation, or a restricted topic each block it outright — and only then clear a quality score whose bar rises with the stakes of the topic. Anything that fails either layer goes to a human with the reasoning attached.

It runs on one machine. No Docker, no hosted services: the vector store and database run in-process, and the whole pipeline can run against a local model with no internet at all.

It is a proof of concept. The knowledge base covers one domain, every dataset is synthetic, and it has never been pointed at a real customer inbox. The documentation is explicit about what was measured, on how small a sample, and what has not been built.

**Key documents** — [BRD](Code/TaskFlow/taskflow/docs/BRD.md) · [Product Specification](Code/TaskFlow/taskflow/docs/PRODUCT_SPECIFICATION.md) · [Architecture Decisions](Code/TaskFlow/taskflow/docs/02_ARCHITECTURE_DECISIONS.md) · [Production Readiness Gap](Code/TaskFlow/taskflow/docs/13_PRODUCTION_READINESS_GAP.md)
