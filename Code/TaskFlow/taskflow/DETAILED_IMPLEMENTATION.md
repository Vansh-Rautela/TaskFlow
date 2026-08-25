# Detailed Technical Implementation Documentation

## 1. Configuration-Based Thresholds Implementation

### File: `src/taskflow/pipeline/orchestrator.py`

**Before (Hardcoded):**
```python
# In evaluate_gates call
abstain_threshold=0.55,
threshold=0.70,

# In compute function call
threshold=0.70,

# In weights dictionary (hardcoded)
weights = {
    "citation_coverage": 0.35,
    "grounding_entailment": 0.25,
    "retrieval_relevance": 0.20,
    "intent_confidence": 0.10,
    "tone_alignment": 0.10,
}
```

**After (Configuration-Based):**
```python
# Using thresholds_config() function to load from YAML
abstain_threshold=thresholds_config()["gates"]["abstain_threshold"],
threshold=thresholds_config()["thresholds"]["general"],
weights=thresholds_config()["weights"],  # Loads from config file
```

### File: `config/thresholds.yaml`
```yaml
gates:
  abstain_threshold: 0.55
  min_grounding_ratio: 0.80
  max_unsupported_claims: 0

thresholds:                    # per-intent auto-send bar
  general: 0.70
  technical: 0.70
  account: 0.70
  feature_request: 0.75
  billing: 0.80
  refund: 0.90
  complaint: 1.01              # unreachable by construction; also blocked by G4
  unknown: 1.01

weights:                       # sum to 1.0 — quality signals only, no safety signals
  citation_coverage: 0.35
  grounding_entailment: 0.25
  retrieval_relevance: 0.20
  intent_confidence: 0.10
  tone_alignment: 0.10
```

## 2. Database Configuration Implementation

### File: `src/taskflow/api/app.py`
**Before (Hardcoded):**
```python
db_url = "sqlite+aiosqlite:///data/taskflow.db"
```

**After (Configuration-Based):**
```python
from taskflow.config.settings import settings
db_url = settings().database_url
```

This change allows configuration of the database URL through the `.env` file or environment variables, enabling different databases for different environments.

## 3. Provider Router Implementation

### File: `src/taskflow/api/app.py`
**Before (Hardcoded Providers):**
```python
# Direct instantiation
openrouter = OpenRouterProvider()
claude = ClaudeProvider() 
ollama = OllamaProvider()
```

**After (Provider Router):**
```python
# Using configuration-driven approach
llm_router = ProviderRouter()
```

The `ProviderRouter` loads provider configuration from `config/providers.yaml` which specifies priority and availability of different LLM providers.

## 4. Confidence Value Configuration

### File: `src/taskflow/services/classify/service.py`
**Before (Hardcoded):**
```python
return Intent.REFUND, 0.95  # Hardcoded confidence
```

**After (Configurable):**
```python
fallback_thresholds = thresholds_config().get("fallback_confidences", {})
return Intent.REFUND, fallback_thresholds.get("refund", 0.95)
```

### File: `src/taskflow/services/draft/service.py`
**Before (Hardcoded):**
```python
draft_confidence=0.50  # Hardcoded
```

**After (Configurable):**
```python
draft_confidence=thresholds_config()["fallback_confidences"].get("draft", 0.50)
```

### File: `src/taskflow/services/validate/citations.py`
**Before (Hardcoded):**
```python
score=0.70  # Hardcoded validation threshold
```

**After (Configurable):**
```python
score=thresholds_config()["citation_validation_threshold"]
```

## 5. Telegram Channel Adapter Implementation

### File: `src/taskflow/adapters/channels/telegram.py`

This implements the `ChannelConnector` interface with:

**Health Check Method:**
```python
async def health(self) -> bool:
    # Checks if bot token and chat ID are configured
    # Tests bot connectivity via Telegram API
```

**Message Fetching:**
```python
async def fetch(self) -> list[InboundMessage]:
    # Gets updates from Telegram Bot API
    # Filters for messages from configured chat
    # Converts Telegram messages to InboundMessage objects
```

**Message Sending:**
```python
async def send(self, outbound: OutboundMessage) -> DeliveryReceipt:
    # Sends messages via Telegram Bot API
    # Handles error cases appropriately
```

## 6. Dashboard Configuration Fix

### File: `dashboard/app.py`
**Before (Hardcoded):**
```python
db_url = "sqlite+aiosqlite:///data/taskflow.db"
```

**After (Configuration-Based):**
```python
from taskflow.config.settings import settings
db_url = settings().database_url
```

## 7. Configuration Loading System

### File: `src/taskflow/config/settings.py`
The configuration loading system uses:
- `@lru_cache` for efficient loading
- YAML parsing for structured configuration
- Environment variable overrides for secrets
- Modular configuration loading functions:
  - `thresholds_config()` - for confidence thresholds
  - `providers_config()` - for LLM provider settings
  - `policies_config()` - for business rules
  - `app_config()` - for application settings

## 8. Benefits of This Approach

1. **Environment Flexibility**: Different environments can have different configurations
2. **Easy Tuning**: Business thresholds can be adjusted without code changes
3. **Maintainability**: All configuration in central locations
4. **Testing**: Easy to mock/override configurations for testing
5. **Deployment**: No code changes needed for different deployments
6. **Observability**: Configuration can be monitored and audited

This implementation follows the principle that "Everything tunable lives in .env (secrets, paths) or config/*.yaml (behaviour). Nothing tunable lives in Python" as specified in the project's ADR-004.